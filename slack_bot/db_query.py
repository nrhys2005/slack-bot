from __future__ import annotations

import asyncio
import csv as csv_mod
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from slack_bot.config import ProjectConfig
from slack_bot.runner import MAX_OUTPUT_LENGTH
from slack_bot.security import make_safe_env

logger = logging.getLogger(__name__)

# Claude CLI stdout 누적 상한 (bytes). 초과 시 프로세스 강제 종료하여 OOM/슬랙 장애 방지.
_MAX_STDOUT_BYTES = 256 * 1024  # 256KB
# 내보내기용 상한은 더 넉넉하게 (CSV 파일로 저장하므로 stdout은 요약만)
_MAX_EXPORT_STDOUT_BYTES = 64 * 1024  # 64KB
DB_QUERY_TIMEOUT = 120  # 2분. DB 조회는 충분한 시간
DB_EXPORT_TIMEOUT = 180  # 3분. 내보내기는 대량 데이터 가능

_CREDENTIAL_SUFFIXES = ("USERNAME", "PASSWORD", "READ_HOST", "PORT", "DB_NAME")


class DBEnvError(Exception):
    """DB 자격증명 로드 실패."""


def _convert_md_tables_to_code_blocks(text: str) -> str:
    """마크다운 테이블(|...|)을 Slack에서 보기 좋은 코드 블록으로 변환."""
    lines = text.split("\n")
    result: list[str] = []
    table_lines: list[str] = []
    in_table = False

    for line in lines:
        stripped = line.strip()
        is_table_line = stripped.startswith("|") and stripped.endswith("|")

        if is_table_line:
            if not in_table:
                in_table = True
                table_lines = []
            table_lines.append(stripped)
        else:
            if in_table:
                result.append(_format_table(table_lines))
                in_table = False
                table_lines = []
            result.append(line)

    if in_table:
        result.append(_format_table(table_lines))

    return "\n".join(result)


def _format_table(table_lines: list[str]) -> str:
    """파이프 테이블 행들을 정렬된 코드 블록 텍스트로 변환."""
    # 구분선(|---|---|) 제거
    rows: list[list[str]] = []
    for line in table_lines:
        cells = [c.strip() for c in line.strip("|").split("|")]
        # 구분선 판별: 모든 셀이 ---만으로 구성
        if all(set(c) <= {"-", ":"} and len(c) > 0 for c in cells):
            continue
        rows.append(cells)

    if not rows:
        return ""

    # 각 컬럼 최대 너비 계산
    col_count = max(len(r) for r in rows)
    widths = [0] * col_count
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # 정렬된 텍스트 생성
    formatted: list[str] = []
    for idx, row in enumerate(rows):
        parts = []
        for i in range(col_count):
            cell = row[i] if i < len(row) else ""
            parts.append(cell.ljust(widths[i]))
        formatted.append("  ".join(parts))
        # 헤더 아래 구분선
        if idx == 0:
            formatted.append("  ".join("-" * w for w in widths))

    return "```\n" + "\n".join(formatted) + "\n```"


def _load_db_env(project: ProjectConfig) -> dict[str, dict[str, str]]:
    """프로젝트의 DBConfig를 기반으로 DB 자격증명을 로드.

    Returns:
        {논리명: {username, password, host, port, db_name}}
    """
    db_config = project.db
    if db_config is None:
        raise DBEnvError("프로젝트에 DB 설정이 없습니다")

    env_path = Path(project.path) / db_config.env_file
    if not env_path.exists():
        raise DBEnvError(f".env 파일을 찾을 수 없습니다: {env_path}")

    values = dotenv_values(env_path)
    result: dict[str, dict[str, str]] = {}

    for logical_name, prefix in db_config.env_prefix.items():
        creds: dict[str, str] = {}
        missing: list[str] = []
        for suffix in _CREDENTIAL_SUFFIXES:
            key = f"{prefix}_{suffix}"
            val = values.get(key)
            if not val:
                missing.append(key)
            else:
                creds[suffix.lower()] = val
        if missing:
            raise DBEnvError(f".env에 다음 키가 누락됨: {', '.join(missing)}")
        result[logical_name] = creds

    return result


def build_db_instructions(
    db_envs: dict[str, dict[str, str]],
    model_paths: list[str] | None = None,
) -> str:
    """DB 접속 정보 + SQL 규칙을 텍스트로 반환. chat.py에서도 재사용."""
    db_lines: list[str] = []
    psql_examples: list[str] = []

    for name, creds in db_envs.items():
        pw_var = f"PGPASSWORD_{name.upper()}"
        db_lines.append(
            f"- {name} DB: host={creds['read_host']}, port={creds['port']}, "
            f"user={creds['username']}, db={creds['db_name']}, "
            f"password는 환경변수 {pw_var}"
        )
        psql_examples.append(
            f'   PGPASSWORD="${pw_var}" psql -h {creds["read_host"]} '
            f'-p {creds["port"]} -U {creds["username"]} -d {creds["db_name"]} '
            f'-v ON_ERROR_STOP=1 -c "BEGIN; SET TRANSACTION READ ONLY; '
            f'SELECT ... LIMIT 100; ROLLBACK;"'
        )

    model_line = ""
    if model_paths:
        paths = ", ".join(f"{p}/*.py" for p in model_paths)
        model_line = f"\n스키마 파악: {paths} 의 SQLAlchemy 모델을 Read/Grep해 확인"

    return (
        "DB 접속 정보:\n"
        + "\n".join(db_lines)
        + model_line
        + "\n\npsql 호출 예시:\n"
        + "\n".join(psql_examples)
    )


def _build_sqlite_system_prompt(
    project: ProjectConfig,
    wiki_path: str | None = None,
) -> str:
    """SQLite 프로젝트용 시스템 프롬프트."""
    db_path = str(Path(project.path) / project.db.db_path)

    model_section = ""
    if project.db and project.db.model_paths:
        paths = ", ".join(f"`{p}/*.py`" for p in project.db.model_paths)
        model_section = (
            f"\n## 스키마 파악 방법\n"
            f"- {paths} 의 모델을 Read/Grep해 테이블·컬럼 확인\n"
        )

    wiki_line = (
        f"- 도메인 용어가 모호하면 위키 디렉토리({wiki_path})에서 Glob/Grep/Read로 찾아본다.\n"
        if wiki_path
        else ""
    )

    return f"""너는 Slack DB 봇이다. 자연어 질문을 받아 SQL로 변환하고 sqlite3로 실행해 결과를 돌려준다.

## DB 정보
- SQLite DB 경로: `{db_path}`
{model_section}
{wiki_line}
## sqlite3 호출 예시
```bash
   sqlite3 "file:{db_path}?mode=ro" "SELECT ... LIMIT 100"
```

쿼리 실패 시 에러 메시지와 원인을 그대로 전달하고, 재시도 전에 스키마를 다시 확인한다.
SELECT 외의 DML/DDL(INSERT, UPDATE, DELETE, DROP, ALTER 등)은 절대 실행하지 않는다.

## 답변 형식 (Slack mrkdwn)
- *bold*는 `*텍스트*` (별표 1개). `**텍스트**` 사용 금지.
- 헤더 대신 *bold* 텍스트로 섹션 구분. `##` `###` 사용 금지.
- 표(|---|)는 Slack에서 안 됨. 소수 행은 블릿(•) 리스트, 다수 행은 ``` 코드 블록.
- SQL은 ``` 블록으로 표시 (언어 태그 없이)
- 먼저 1-2문장으로 조회 대상 설명, 실행한 SQL 전문을 반드시 포함
"""


def build_sqlite_db_instructions(project: ProjectConfig) -> str:
    """SQLite DB 접속 정보를 텍스트로 반환. chat.py에서 재사용."""
    db_path = str(Path(project.path) / project.db.db_path)

    model_line = ""
    if project.db and project.db.model_paths:
        paths = ", ".join(f"{p}/*.py" for p in project.db.model_paths)
        model_line = f"\n스키마 파악: {paths} 의 모델을 Read/Grep해 확인"

    return (
        "DB 접속 정보:\n"
        f"- SQLite DB 경로: {db_path}"
        f"{model_line}\n\n"
        f"sqlite3 호출 예시:\n"
        f'   sqlite3 "file:{db_path}?mode=ro" "SELECT ... LIMIT 100"'
    )


def _build_system_prompt(
    db_envs: dict[str, dict[str, str]],
    model_paths: list[str] | None = None,
    wiki_path: str | None = None,
) -> str:
    # DB별 접속정보 섹션
    db_sections: list[str] = []
    psql_examples: list[str] = []
    for name, creds in db_envs.items():
        pw_var = f"PGPASSWORD_{name.upper()}"
        db_sections.append(
            f"- *{name} DB*: host={creds['read_host']}, port={creds['port']}, "
            f"user={creds['username']}, db={creds['db_name']}, "
            f"password는 환경변수 `{pw_var}`"
        )
        psql_examples.append(
            f'   PGPASSWORD="${pw_var}" psql -h {creds["read_host"]} '
            f'-p {creds["port"]} -U {creds["username"]} -d {creds["db_name"]} \\\n'
            f'     -v ON_ERROR_STOP=1 -c "BEGIN; SET TRANSACTION READ ONLY; '
            f'SELECT ... LIMIT 100; ROLLBACK;"'
        )

    model_section = ""
    if model_paths:
        paths = ", ".join(f"`{p}/*.py`" for p in model_paths)
        model_section = (
            f"\n## 스키마 파악 방법\n"
            f"- {paths} 의 SQLAlchemy 모델을 Read/Grep해 테이블·컬럼 확인\n"
            f"- 질문 주제에 따라 적절한 DB 선택\n"
        )

    wiki_line = (
        f"- 도메인 용어가 모호하면 위키 디렉토리({wiki_path})에서 Glob/Grep/Read로 찾아본다.\n"
        if wiki_path
        else ""
    )

    return f"""너는 Slack DB 봇이다. 자연어 질문을 받아 SQL로 변환하고 psql로 실행해 결과를 돌려준다.

## DB 접속 정보
{chr(10).join(db_sections)}
{model_section}
{wiki_line}
## psql 호출 예시
```bash
{chr(10).join(psql_examples)}
```

쿼리 실패 시 에러 메시지와 원인을 그대로 전달하고, 재시도 전에 모델/스키마를 다시 확인한다.

## 답변 형식 (Slack mrkdwn)
- *bold*는 `*텍스트*` (별표 1개). `**텍스트**` 사용 금지.
- 헤더 대신 *bold* 텍스트로 섹션 구분. `##` `###` 사용 금지.
- 표(|---|)는 Slack에서 안 됨. 소수 행은 블릿(•) 리스트, 다수 행은 ``` 코드 블록.
- SQL은 ``` 블록으로 표시 (언어 태그 없이)
- 먼저 1-2문장으로 조회 대상 설명, 실행한 SQL 전문을 반드시 포함
"""


async def run_db_query(
    question: str,
    project: ProjectConfig,
    wiki_path: str | None = None,
) -> str:
    """자연어 질문을 받아 Claude CLI로 SQL 생성·실행 후 결과 문자열 반환."""
    if project.db and project.db.db_type == "sqlite":
        system_prompt = _build_sqlite_system_prompt(project, wiki_path)
        prompt = f"{system_prompt}\n\n## 질문\n{question}"
        env = make_safe_env()
    else:
        try:
            db_envs = _load_db_env(project)
        except DBEnvError as exc:
            logger.error("DB 자격증명 로드 실패: %s", exc)
            return f":warning: DB 자격증명 로드 실패: {exc}"

        model_paths = project.db.model_paths if project.db else None
        system_prompt = _build_system_prompt(db_envs, model_paths, wiki_path)
        prompt = f"{system_prompt}\n\n## 질문\n{question}"

        extra_env: dict[str, str] = {}
        for name, creds in db_envs.items():
            extra_env[f"PGPASSWORD_{name.upper()}"] = creds["password"]
        env = make_safe_env(extra=extra_env)

    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "text",
        "--permission-mode",
        "bypassPermissions",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=project.path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # stdout을 라인 단위로 스트리밍하며 누적 바이트를 제한한다.
        # 한계 초과 시 프로세스를 kill 해 메모리 폭주/행 차단.
        if proc.stdout is None:
            raise RuntimeError("stdout pipe not created")
        chunks: list[bytes] = []
        total_bytes = 0
        stdout_truncated = False
        async for line in proc.stdout:
            if total_bytes + len(line) > _MAX_STDOUT_BYTES:
                stdout_truncated = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                break
            chunks.append(line)
            total_bytes += len(line)
        try:
            await asyncio.wait_for(proc.wait(), timeout=DB_QUERY_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.error(
                "Claude CLI DB 조회 시간 초과 (%ds), 강제 종료",
                DB_QUERY_TIMEOUT,
            )
            return (
                ":warning: DB 조회 시간이 초과되었습니다. "
                "질문을 더 구체적으로 해주세요."
            )

        # 에러 메시지 용도로 stderr도 읽되 과도하게 큰 경우 일부만 저장.
        if proc.stderr is None:
            raise RuntimeError("stderr pipe not created")
        stderr_bytes = await proc.stderr.read(_MAX_STDOUT_BYTES)

        if stdout_truncated:
            logger.warning(
                "Claude CLI stdout이 %d bytes 를 초과해 프로세스를 종료했습니다.",
                _MAX_STDOUT_BYTES,
            )

        if proc.returncode != 0 and not stdout_truncated:
            logger.error(
                "Claude CLI 실패 (exit %d)\nstdout: %s\nstderr: %s",
                proc.returncode,
                b"".join(chunks).decode(errors="replace"),
                stderr_bytes.decode(errors="replace"),
            )
            return ":warning: DB 조회 중 오류가 발생했습니다. 로그를 확인해주세요."

        output = b"".join(chunks).decode(errors="replace").strip()
        if stdout_truncated:
            output += (
                f"\n\n:warning: 결과가 {_MAX_STDOUT_BYTES // 1024}KB 를 초과해 "
                "프로세스를 중단했습니다. 필터 조건을 좁혀 다시 질문해주세요."
            )
        # 마크다운 테이블 → 코드 블록 변환 (Slack 호환)
        output = _convert_md_tables_to_code_blocks(output)
        if len(output) > MAX_OUTPUT_LENGTH:
            output = output[:MAX_OUTPUT_LENGTH] + "\n\n... (truncated)"
        return output
    except Exception:
        logger.exception("Claude CLI 호출 실패 (DB 조회)")
        return ":warning: DB 조회 중 오류가 발생했습니다. 로그를 확인해주세요."


# ----------------------------------------------------------------
# CSV/Excel 내보내기
# ----------------------------------------------------------------


@dataclass
class ExportResult:
    """DB 조회 내보내기 결과."""

    summary: str  # Claude가 출력한 요약 텍스트
    excel_path: Path | None = None  # 생성된 Excel 파일 경로
    error: str | None = None  # 에러 메시지


def _csv_to_excel(csv_path: Path) -> Path:
    """CSV 파일을 Excel(.xlsx)로 변환."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv_mod.reader(f)
        for row in reader:
            ws.append(row)

    excel_path = csv_path.with_suffix(".xlsx")
    wb.save(excel_path)
    return excel_path


def _build_export_prompt_pg(
    db_envs: dict[str, dict[str, str]],
    model_paths: list[str] | None,
    csv_path: Path,
    wiki_path: str | None = None,
) -> str:
    """PostgreSQL 내보내기용 시스템 프롬프트."""
    db_sections: list[str] = []
    copy_examples: list[str] = []
    for name, creds in db_envs.items():
        pw_var = f"PGPASSWORD_{name.upper()}"
        db_sections.append(
            f"- *{name} DB*: host={creds['read_host']}, port={creds['port']}, "
            f"user={creds['username']}, db={creds['db_name']}, "
            f"password는 환경변수 `{pw_var}`"
        )
        copy_examples.append(
            f'   PGPASSWORD="${pw_var}" psql -h {creds["read_host"]} '
            f'-p {creds["port"]} -U {creds["username"]} -d {creds["db_name"]} \\\n'
            f'     -c "COPY (SELECT ... LIMIT 10000) TO STDOUT WITH CSV HEADER" '
            f"> {csv_path}"
        )

    model_section = ""
    if model_paths:
        paths = ", ".join(f"`{p}/*.py`" for p in model_paths)
        model_section = (
            f"\n## 스키마 파악 방법\n"
            f"- {paths} 의 SQLAlchemy 모델을 Read/Grep해 테이블·컬럼 확인\n"
        )

    wiki_line = (
        f"- 도메인 용어가 모호하면 위키 디렉토리({wiki_path})에서 Glob/Grep/Read로 찾아본다.\n"
        if wiki_path
        else ""
    )

    return f"""너는 Slack DB 봇이다. 자연어 질문을 받아 SQL로 변환하고, 결과를 CSV 파일로 저장한다.

## DB 접속 정보
{chr(10).join(db_sections)}
{model_section}
{wiki_line}

## CSV 저장 규칙 (필수)
- 결과를 반드시 `{csv_path}` 에 CSV 파일로 저장해야 한다.
- psql의 COPY 명령을 사용해 stdout으로 CSV를 출력하고 파일로 리다이렉트:
```bash
{chr(10).join(copy_examples)}
```
- 데이터 건수 제한: LIMIT 10000 적용
- SELECT 외의 DML/DDL은 절대 실행하지 않는다.

## stdout 출력 형식
- 조회 대상 설명 (1-2문장)
- 실행한 SQL 전문
- 결과 건수
- CSV 파일에 대한 설명은 불필요 (봇이 자동 처리)
"""


def _build_export_prompt_sqlite(
    project: ProjectConfig,
    csv_path: Path,
    wiki_path: str | None = None,
) -> str:
    """SQLite 내보내기용 시스템 프롬프트."""
    db_path = str(Path(project.path) / project.db.db_path)

    model_section = ""
    if project.db and project.db.model_paths:
        paths = ", ".join(f"`{p}/*.py`" for p in project.db.model_paths)
        model_section = (
            f"\n## 스키마 파악 방법\n"
            f"- {paths} 의 모델을 Read/Grep해 테이블·컬럼 확인\n"
            f"- 또는 sqlite3로 `.schema` 명령 실행\n"
        )

    wiki_line = (
        f"- 도메인 용어가 모호하면 위키 디렉토리({wiki_path})에서 Glob/Grep/Read로 찾아본다.\n"
        if wiki_path
        else ""
    )

    return f"""너는 Slack DB 봇이다. 자연어 질문을 받아 SQL로 변환하고, 결과를 CSV 파일로 저장한다.

## DB 정보
- SQLite DB 경로: `{db_path}`
{model_section}
{wiki_line}

## CSV 저장 규칙 (필수)
- 결과를 반드시 `{csv_path}` 에 CSV 파일로 저장해야 한다.
- sqlite3의 -header -csv 옵션을 사용해 파일로 리다이렉트:
```bash
   sqlite3 -header -csv "file:{db_path}?mode=ro" "SELECT ... LIMIT 10000" > {csv_path}
```
- 데이터 건수 제한: LIMIT 10000 적용
- SELECT 외의 DML/DDL은 절대 실행하지 않는다.

## stdout 출력 형식
- 조회 대상 설명 (1-2문장)
- 실행한 SQL 전문
- 결과 건수
- CSV 파일에 대한 설명은 불필요 (봇이 자동 처리)
"""


async def run_db_query_export(
    question: str,
    project: ProjectConfig,
    wiki_path: str | None = None,
) -> ExportResult:
    """자연어 질문 → Claude CLI → CSV → Excel 변환. ExportResult 반환."""
    fd, csv_path_str = tempfile.mkstemp(suffix=".csv", prefix="slack_export_")
    os.close(fd)  # Claude CLI가 파일을 덮어쓰므로 fd는 닫는다
    csv_path = Path(csv_path_str)

    try:
        if project.db and project.db.db_type == "sqlite":
            system_prompt = _build_export_prompt_sqlite(project, csv_path, wiki_path)
            env = make_safe_env()
        else:
            try:
                db_envs = _load_db_env(project)
            except DBEnvError as exc:
                logger.error("DB 자격증명 로드 실패: %s", exc)
                return ExportResult(
                    summary="", error=f"DB 자격증명 로드 실패: {exc}"
                )

            model_paths = project.db.model_paths if project.db else None
            system_prompt = _build_export_prompt_pg(
                db_envs, model_paths, csv_path, wiki_path
            )
            extra_env: dict[str, str] = {}
            for name, creds in db_envs.items():
                extra_env[f"PGPASSWORD_{name.upper()}"] = creds["password"]
            env = make_safe_env(extra=extra_env)

        prompt = f"{system_prompt}\n\n## 질문\n{question}"
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "text",
            "--permission-mode",
            "bypassPermissions",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=project.path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        if proc.stdout is None:
            raise RuntimeError("stdout pipe not created")

        chunks: list[bytes] = []
        total_bytes = 0
        stdout_truncated = False
        async for line in proc.stdout:
            if total_bytes + len(line) <= _MAX_EXPORT_STDOUT_BYTES:
                chunks.append(line)
                total_bytes += len(line)
            else:
                stdout_truncated = True

        try:
            await asyncio.wait_for(proc.wait(), timeout=DB_EXPORT_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.error("Claude CLI 내보내기 시간 초과 (%ds)", DB_EXPORT_TIMEOUT)
            return ExportResult(
                summary="",
                error="내보내기 시간이 초과되었습니다. 조건을 좁혀 다시 시도해주세요.",
            )

        summary = b"".join(chunks).decode(errors="replace").strip()
        if stdout_truncated:
            summary += f"\n\n:warning: 요약 결과가 {_MAX_EXPORT_STDOUT_BYTES // 1024}KB를 초과하여 일부 생략되었습니다."
        if len(summary) > MAX_OUTPUT_LENGTH:
            summary = summary[:MAX_OUTPUT_LENGTH] + "\n\n... (truncated)"

        if proc.returncode != 0:
            if proc.stderr:
                stderr_bytes = await proc.stderr.read(_MAX_EXPORT_STDOUT_BYTES)
                logger.error(
                    "Claude CLI 내보내기 실패 (exit %d)\nstderr: %s",
                    proc.returncode,
                    stderr_bytes.decode(errors="replace"),
                )
            return ExportResult(
                summary=summary,
                error="DB 조회 중 오류가 발생했습니다.",
            )

        # CSV 파일 확인
        if not csv_path.exists() or csv_path.stat().st_size == 0:
            logger.warning("CSV 파일이 생성되지 않음: %s", csv_path)
            return ExportResult(
                summary=summary,
                error="CSV 파일이 생성되지 않았습니다. 조회 결과가 없을 수 있습니다.",
            )

        # CSV → Excel 변환
        excel_path = _csv_to_excel(csv_path)
        return ExportResult(summary=summary, excel_path=excel_path)

    except Exception:
        logger.exception("Claude CLI 호출 실패 (DB 내보내기)")
        return ExportResult(
            summary="", error="DB 조회 내보내기 중 오류가 발생했습니다."
        )
    finally:
        csv_path.unlink(missing_ok=True)
