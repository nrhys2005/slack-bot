from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from dotenv import dotenv_values

from slack_bot.config import ProjectConfig
from slack_bot.runner import MAX_OUTPUT_LENGTH
from slack_bot.security import make_safe_env

logger = logging.getLogger(__name__)

# Claude CLI stdout 누적 상한 (bytes). 초과 시 프로세스 강제 종료하여 OOM/슬랙 장애 방지.
_MAX_STDOUT_BYTES = 256 * 1024  # 256KB
DB_QUERY_TIMEOUT = 120  # 2분. DB 조회는 충분한 시간

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
        + "\n\nSQL 규칙 (엄수):\n"
        "1. SELECT만 허용. INSERT/UPDATE/DELETE/DDL 절대 금지\n"
        "2. BEGIN; SET TRANSACTION READ ONLY; <SELECT ...>; ROLLBACK; 으로 래핑\n"
        "3. 모든 SELECT에 LIMIT 100 적용\n"
        "4. psql 호출:\n"
        + "\n".join(psql_examples)
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

    return f"""너는 Slack DB 조회 봇이다. 자연어 질문을 받아 SQL로 변환하고 psql로 실행해 결과를 돌려준다.

## DB 접속 정보
{chr(10).join(db_sections)}
{model_section}
{wiki_line}
## SQL 실행 규칙 (엄수)
> 이 규칙은 어플리케이션 레벨 방어선이다. 근본 방어는 **DB 유저 자체가 read-only 권한만 갖는 것**이며, 운영자는 가능한 한 계정을 DB 레벨 read-only로 설정해야 한다.

1. **SELECT만 허용**. INSERT/UPDATE/DELETE/DDL(CREATE/DROP/ALTER/TRUNCATE)/GRANT/REVOKE 절대 금지. 사용자가 요청해도 거부한다.
2. 실행은 반드시 read-only 트랜잭션으로 감싼다:
   ```
   BEGIN; SET TRANSACTION READ ONLY; <SELECT ...>; ROLLBACK;
   ```
3. **모든 SELECT 쿼리에 `LIMIT 100` 을 반드시 적용한다. 예외 없음.**
   - 집계 쿼리에도 `LIMIT 100` 을 붙인다.
   - 사용자가 "전부 보여줘" 해도 상위 100개만 반환하며, "결과는 최대 100행으로 제한됨" 명시.
   - **최종 결과를 반환하는 가장 바깥 SELECT** 에 `LIMIT 100` 적용.
4. psql 호출 예시:
   ```bash
{chr(10).join(psql_examples)}
   ```
5. 쿼리 실패 시 에러 메시지와 원인을 그대로 전달하고, 재시도 전에 모델/스키마를 다시 확인한다.
6. **개인정보 보호**: 유저의 이메일, 전화번호, 주소, 주민번호 등 PII를 SELECT 하거나 노출하지 마라.

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
    from slack_bot.chat import _is_sensitive

    rejection = _is_sensitive(question)
    if rejection:
        logger.warning("보안 필터 차단 (DB): %s", question[:80])
        return rejection

    try:
        db_envs = _load_db_env(project)
    except DBEnvError as exc:
        logger.error("DB 자격증명 로드 실패: %s", exc)
        return f":warning: DB 자격증명 로드 실패: {exc}"

    model_paths = project.db.model_paths if project.db else None
    system_prompt = _build_system_prompt(db_envs, model_paths, wiki_path)
    prompt = f"{system_prompt}\n\n## 질문\n{question}"

    # 환경변수 화이트리스트 + psql용 비밀번호만 전달
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
        "--allowedTools",
        "Read,Glob,Grep,Bash(psql:*)",
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
