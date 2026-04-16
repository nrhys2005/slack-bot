from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import dotenv_values

from slack_bot.runner import MAX_OUTPUT_LENGTH

logger = logging.getLogger(__name__)


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


def _load_db_env(ra_backend_path: str) -> dict[str, str]:
    """ra_backend/app/.env 에서 DB 자격증명만 추출해 dict로 반환."""
    env_path = Path(ra_backend_path) / "app" / ".env"
    if not env_path.exists():
        raise DBEnvError(f".env 파일을 찾을 수 없습니다: {env_path}")

    values = dotenv_values(env_path)
    required = (*_REQUIRED_RA_KEYS, *_REQUIRED_CORE_KEYS)
    missing = [k for k in required if not values.get(k)]
    if missing:
        raise DBEnvError(f".env에 다음 키가 누락됨: {', '.join(missing)}")

    return {k: values[k] for k in required}


def _build_system_prompt(db_env: dict[str, str], wiki_path: str | None) -> str:
    ra_user = db_env["POSTGRESQL_RA_USERNAME"]
    ra_host = db_env["POSTGRESQL_RA_READ_HOST"]
    ra_port = db_env["POSTGRESQL_RA_PORT"]
    ra_db = db_env["POSTGRESQL_RA_DB_NAME"]
    core_user = db_env["POSTGRESQL_CORE_USERNAME"]
    core_host = db_env["POSTGRESQL_CORE_READ_HOST"]
    core_port = db_env["POSTGRESQL_CORE_PORT"]
    core_db = db_env["POSTGRESQL_CORE_DB_NAME"]

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

## 답변 형식 (Slack 마크다운)
- 먼저 1-2 문장으로 어떤 DB/테이블을 조회하는지 설명
- 실행한 SQL을 ```sql 블록으로 표시 (LIMIT 100 포함)
- 결과가 소수 행이면 표/리스트로, 많으면 상위 몇 건 + 요약 통계로 정리
- 반환 행이 100행이면 "최대 100행으로 제한됨 — 더 필요하면 필터 조건을 좁혀 다시 질문해주세요" 문구를 꼭 덧붙인다
- 결과가 없으면 그렇게 말할 것
- 실행 실패 또는 SELECT 외 요청 거부 시 이유를 명확히 설명
"""


async def run_db_query(
    question: str,
    ra_backend_path: str,
    wiki_path: str | None = None,
) -> str:
    """자연어 질문을 받아 Claude CLI로 SQL 생성·실행 후 결과 문자열 반환."""
    try:
        db_env = _load_db_env(ra_backend_path)
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
        "claude", "-p", prompt,
        "--output-format", "text",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", "Read,Glob,Grep,Bash(psql:*)",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=ra_backend_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(
                "Claude CLI 실패 (exit %d)\nstdout: %s\nstderr: %s",
                proc.returncode,
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"),
            )
            return ":warning: DB 조회 중 오류가 발생했습니다. 로그를 확인해주세요."

        output = stdout.decode(errors="replace").strip()
        if len(output) > MAX_OUTPUT_LENGTH:
            output = output[:MAX_OUTPUT_LENGTH] + "\n\n... (truncated)"
        return output
    except Exception:
        logger.exception("Claude CLI 호출 실패 (DB 조회)")
        return ":warning: DB 조회 중 오류가 발생했습니다. 로그를 확인해주세요."
