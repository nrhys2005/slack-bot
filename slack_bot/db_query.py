from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import dotenv_values

from slack_bot.runner import MAX_OUTPUT_LENGTH

logger = logging.getLogger(__name__)

# Claude CLI stdout 누적 상한 (bytes). 초과 시 프로세스 강제 종료하여 OOM/슬랙 장애 방지.
# Slack 출력 자체는 MAX_OUTPUT_LENGTH 로 한 번 더 자르지만, psql 결과가 폭주할 때
# 메모리에 전부 쌓이는 일을 막기 위한 안전장치.
_MAX_STDOUT_BYTES = 256 * 1024  # 256KB
DB_QUERY_TIMEOUT = 120  # 2분. DB 조회는 충분한 시간


# ra_backend/app/.env 에서 읽어올 키 목록
_REQUIRED_RA_KEYS = (
    "POSTGRESQL_RA_USERNAME",
    "POSTGRESQL_RA_PASSWORD",
    "POSTGRESQL_RA_READ_HOST",
    "POSTGRESQL_RA_PORT",
    "POSTGRESQL_RA_DB_NAME",
)
_REQUIRED_CORE_KEYS = (
    "POSTGRESQL_CORE_USERNAME",
    "POSTGRESQL_CORE_PASSWORD",
    "POSTGRESQL_CORE_READ_HOST",
    "POSTGRESQL_CORE_PORT",
    "POSTGRESQL_CORE_DB_NAME",
)


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


def _load_db_env(db_backend_path: str) -> dict[str, str]:
    """ra_backend/app/.env 에서 DB 자격증명만 추출해 dict로 반환."""
    env_path = Path(db_backend_path) / "app" / ".env"
    if not env_path.exists():
        raise DBEnvError(f".env 파일을 찾을 수 없습니다: {env_path}")

    values = dotenv_values(env_path)
    required = (*_REQUIRED_RA_KEYS, *_REQUIRED_CORE_KEYS)
    missing = [k for k in required if not values.get(k)]
    if missing:
        raise DBEnvError(f".env에 다음 키가 누락됨: {', '.join(missing)}")

    return {k: values[k] for k in required}


def build_db_instructions(db_env: dict[str, str]) -> str:
    """DB 접속 정보 + SQL 규칙을 텍스트로 반환. chat.py에서도 재사용."""
    ra_user = db_env["POSTGRESQL_RA_USERNAME"]
    ra_host = db_env["POSTGRESQL_RA_READ_HOST"]
    ra_port = db_env["POSTGRESQL_RA_PORT"]
    ra_db = db_env["POSTGRESQL_RA_DB_NAME"]
    core_user = db_env["POSTGRESQL_CORE_USERNAME"]
    core_host = db_env["POSTGRESQL_CORE_READ_HOST"]
    core_port = db_env["POSTGRESQL_CORE_PORT"]
    core_db = db_env["POSTGRESQL_CORE_DB_NAME"]

    return f"""DB 접속 정보:
- ra DB (운영 데이터): host={ra_host}, port={ra_port}, user={ra_user}, db={ra_db}, password는 환경변수 PGPASSWORD_RA
  주요 스키마: ra_v2, out_kr, ra_v2_english, out_kr_english
- core DB (마스터/참조 데이터): host={core_host}, port={core_port}, user={core_user}, db={core_db}, password는 환경변수 PGPASSWORD_CORE
  주요 스키마: ra_v2, external_data, r3, manage, mart_data

스키마 파악: app/models/ra/*.py, app/models/core/*.py 의 SQLAlchemy 모델을 Read/Grep해 확인

SQL 규칙 (엄수):
1. SELECT만 허용. INSERT/UPDATE/DELETE/DDL 절대 금지
2. BEGIN; SET TRANSACTION READ ONLY; <SELECT ...>; ROLLBACK; 으로 래핑
3. 모든 SELECT에 LIMIT 100 적용
4. psql 호출:
   PGPASSWORD="$PGPASSWORD_RA" psql -h {ra_host} -p {ra_port} -U {ra_user} -d {ra_db} -v ON_ERROR_STOP=1 -c "BEGIN; SET TRANSACTION READ ONLY; SELECT ... LIMIT 100; ROLLBACK;"
   core DB: PGPASSWORD_CORE + core 접속정보 사용"""


def _build_system_prompt(db_env: dict[str, str], wiki_path: str | None) -> str:
    db_instructions = build_db_instructions(db_env)

    wiki_line = (
        f"- 도메인 용어가 모호하면 위키 디렉토리({wiki_path})에서 Glob/Grep/Read로 찾아본다.\n"
        if wiki_path
        else ""
    )

    return f"""너는 Slack DB 조회 봇이다. 자연어 질문을 받아 SQL로 변환하고 psql로 실행해 결과를 돌려준다.

## DB 접속 정보
- **ra DB** (운영 데이터: 유저, 리포트, 문의, 공지, 관심빌딩 등)
  - host={ra_host}, port={ra_port}, user={ra_user}, db={ra_db}
  - password는 환경변수 `PGPASSWORD_RA` 로 접근 가능
  - 주요 스키마: ra_v2, out_kr, ra_v2_english, out_kr_english
- **core DB** (마스터/참조 데이터: 건축인허가, 기업통계, 주택공급 등)
  - host={core_host}, port={core_port}, user={core_user}, db={core_db}
  - password는 환경변수 `PGPASSWORD_CORE` 로 접근 가능
  - 주요 스키마: ra_v2, external_data, r3, manage, mart_data

## 스키마 파악 방법
- `app/models/ra/*.py` 와 `app/models/core/*.py` 의 SQLAlchemy 모델을 Read/Grep해 테이블·컬럼 확인
- 질문 주제가 운영성 vs 참조성인지에 따라 ra/core 중 적절한 DB 선택
- 양쪽 모두 필요하면 각각 쿼리 후 결합 설명
{wiki_line}
## SQL 실행 규칙 (엄수)
> 이 규칙은 어플리케이션 레벨 방어선이다. 근본 방어는 **DB 유저 자체가 read-only 권한만 갖는 것**이며, 운영자는 가능한 한 `app/.env` 의 계정을 DB 레벨 read-only로 설정해야 한다. 아래 규칙은 계정이 쓰기 권한을 가진 경우에도 피해를 최소화하기 위한 2차 방어선이다.

1. **SELECT만 허용**. INSERT/UPDATE/DELETE/DDL(CREATE/DROP/ALTER/TRUNCATE)/GRANT/REVOKE 절대 금지. 사용자가 요청해도 거부한다.
2. 실행은 반드시 read-only 트랜잭션으로 감싼다:
   ```
   BEGIN; SET TRANSACTION READ ONLY; <SELECT ...>; ROLLBACK;
   ```
3. **모든 SELECT 쿼리에 `LIMIT 100` 을 반드시 적용한다. 예외 없음.**
   - 집계만 수행하는 쿼리(COUNT/SUM/AVG/MIN/MAX 등 단일 행 결과)에도 `LIMIT 100` 을 붙인다 — 결과는 어차피 1행이므로 무해하고, 실수로 GROUP BY가 섞여도 방어가 된다.
   - 사용자가 "전부 보여줘", "모든 데이터" 같이 요청해도 거부하지 말고 상위 100개만 반환하며, 답변에 "결과는 최대 100행으로 제한됨" 을 명시한다.
   - 서브쿼리 안쪽이 아니라 **최종 결과를 반환하는 가장 바깥 SELECT** 에 `LIMIT 100` 을 적용한다.
4. psql 호출 예시:
   ```bash
   PGPASSWORD="$PGPASSWORD_RA" psql -h {ra_host} -p {ra_port} -U {ra_user} -d {ra_db} \\
     -v ON_ERROR_STOP=1 -c "BEGIN; SET TRANSACTION READ ONLY; SELECT ... LIMIT 100; ROLLBACK;"
   ```
   core DB도 동일한 형태, 다만 `PGPASSWORD_CORE`, 각 core 접속 정보 사용.
5. 쿼리 실패 시 에러 메시지와 원인을 그대로 전달하고, 재시도 전에 모델/스키마를 다시 확인한다.

## 답변 형식 (Slack mrkdwn — 일반 Markdown과 다르다!)
Slack은 일반 Markdown을 지원하지 않는다. 반드시 아래 규칙을 따라라:
- *bold*는 `*텍스트*` (별표 1개). `**텍스트**` 는 Slack에서 안 먹는다.
- 헤더 대신 *bold* 텍스트로 섹션을 구분한다. `##` `###` 사용 금지.
- 표(|---|)는 Slack에서 렌더링되지 않는다. 대신:
  - 소수 행(5건 이하): 각 행을 블릿(•) 리스트로 표시, 주요 필드를 `key: value` 형태로 나열
  - 다수 행(6건 이상): ``` 코드 블록 안에 정렬된 텍스트 테이블로 표시
- SQL은 ``` 블록으로 표시 (언어 태그 없이, ```sql 금지)
- 링크: `<URL|텍스트>`
- 이모지: :emoji_name: 형식 사용 가능

*예시 (소수 행):*
```
*기각 상태 경공매 조회 결과 (2건)*

• *안동지원* | 2025타경100008 | 부동산강제경매
  접수: 2025-01-03 → 종국: 2026-04-07
  용도: 토지 | 감정가: 72,362,320원
  주소: 경상북도 안동시 풍천면 도양리 1050

• *의정부지방법원* | 2025타경4643 | 부동산강제경매
  접수: 2025-09-29 → 종국: 2026-03-18
  용도: 토지 | 감정가: 121,875,000원
  주소: 경기도 포천시 이동면 연곡리 746-1
```

- 먼저 1-2문장으로 어떤 DB/테이블을 조회하는지 설명
- *반드시* 실행한 SQL 전문을 ``` 블록으로 포함할 것 (생략 금지)
- 결과가 소수 행이면 블릿 리스트, 많으면 코드 블록 테이블로 정리
- 반환 행이 100행이면 "최대 100행으로 제한됨 — 더 필요하면 필터 조건을 좁혀 다시 질문해주세요" 문구를 꼭 덧붙인다
- 결과가 없으면 그렇게 말할 것
- 실행 실패 또는 SELECT 외 요청 거부 시 이유를 명확히 설명
"""


async def run_db_query(
    question: str,
    db_backend_path: str,
    wiki_path: str | None = None,
) -> str:
    """자연어 질문을 받아 Claude CLI로 SQL 생성·실행 후 결과 문자열 반환."""
    try:
        db_env = _load_db_env(db_backend_path)
    except DBEnvError as exc:
        logger.error("DB 자격증명 로드 실패: %s", exc)
        return f":warning: DB 자격증명 로드 실패: {exc}"

    system_prompt = _build_system_prompt(db_env, wiki_path)
    prompt = f"{system_prompt}\n\n## 질문\n{question}"

    # ANTHROPIC_API_KEY 제거 → Claude Code OAuth 인증 사용
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    # psql용 비밀번호를 전용 환경변수로 전달 (프롬프트에서 이 이름을 안내함)
    env["PGPASSWORD_RA"] = db_env["POSTGRESQL_RA_PASSWORD"]
    env["PGPASSWORD_CORE"] = db_env["POSTGRESQL_CORE_PASSWORD"]

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
            cwd=db_backend_path,
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
