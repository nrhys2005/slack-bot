from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from slack_bot.db_query import (
    DBEnvError,
    _convert_md_tables_to_code_blocks,
    _load_db_env,
    build_db_instructions,
)
from slack_bot.security import make_safe_env
from slack_bot.task_manager import TaskInfo

logger = logging.getLogger(__name__)

CHAT_TIMEOUT = 300  # 5분

_DB_KEYWORDS = frozenset(
    {"몇 건", "목록", "가입", "통계", "수치", "조회", "쿼리", "데이터", "테이블", "스키마", "DB", "db"}
)
_STATUS_KEYWORDS = frozenset({"어디까지", "진행", "상태", "멈", "끝났"})

# 보안: 서버/인프라/자격증명 관련 질문 차단 키워드
_SENSITIVE_KEYWORDS = frozenset({
    # 자격증명·인증
    "비밀번호", "패스워드", "password", "비번", "토큰", "token", "secret",
    "시크릿", "credential", "자격증명", "api key", "apikey", "api_key",
    "접속정보", "접속 정보", "인증키", "auth",
    # 환경변수·설정파일
    ".env", "환경변수", "env var",
    # 서버·인프라
    "서버 스펙", "서버스펙", "서버 정보", "서버정보", "서버 사양",
    "IP 주소", "ip주소", "IP주소", "ip 주소",
    "hostname", "호스트네임", "호스트명",
    "SSH", "ssh", "RDP", "rdp", "원격접속", "원격 접속",
    # PC·장비 정보
    "PC 정보", "PC정보", "pc정보", "컴퓨터 정보", "컴퓨터정보",
    "윈도우 정보", "윈도우정보", "MAC 주소", "mac주소", "mac 주소",
    # DB 접속 자격증명 (DB 조회 자체는 허용하되 접속정보 노출은 차단)
    "DB 비밀번호", "DB 패스워드", "DB 계정", "DB 접속",
    "POSTGRESQL_", "PGPASSWORD",
    # 개인정보·유저정보 유출 방지
    "유저 이메일", "유저 메일", "사용자 이메일", "사용자 메일",
    "유저 전화번호", "유저 핸드폰", "유저 휴대폰", "유저 연락처",
    "사용자 전화번호", "사용자 핸드폰", "사용자 휴대폰", "사용자 연락처",
    "회원 이메일", "회원 전화번호", "회원 연락처", "회원 핸드폰",
    "이메일 목록", "이메일 리스트", "메일 목록", "메일 리스트",
    "전화번호 목록", "전화번호 리스트", "연락처 목록", "연락처 리스트",
    "개인정보", "개인 정보",
})

# 개인정보 조회 차단용 (DB 쿼리 결과에 PII 컬럼이 포함되는 것을 방지)
_PII_QUERY_KEYWORDS = frozenset({
    "이메일", "email", "e-mail",
    "전화번호", "핸드폰", "휴대폰", "연락처", "phone",
    "주민번호", "주민등록번호",
    "계좌번호", "카드번호",
    "주소 알려", "집주소", "자택",
})

SENSITIVE_REJECTION = (
    ":lock: 보안 정책상 서버 환경, 접속 정보, 자격증명 관련 질문에는 답변할 수 없습니다.\n"
    "필요하시면 인프라 담당자에게 직접 문의해주세요."
)

PII_REJECTION = (
    ":lock: 보안 정책상 유저 개인정보(이메일, 전화번호, 주소 등)를 조회하거나 노출할 수 없습니다.\n"
    "개인정보가 필요하시면 관리자 콘솔을 이용해주세요."
)


def _is_sensitive(question: str) -> str | None:
    """보안상 답변을 거부해야 하는 질문이면 거부 메시지를 반환, 아니면 None."""
    q = question.lower()
    if any(kw.lower() in q for kw in _SENSITIVE_KEYWORDS):
        return SENSITIVE_REJECTION
    if any(kw.lower() in q for kw in _PII_QUERY_KEYWORDS):
        return PII_REJECTION
    return None


def _needs_db(question: str) -> bool:
    """질문에 DB 관련 키워드가 포함되어 있는지 판별."""
    return any(kw in question for kw in _DB_KEYWORDS)


def _is_status_query(question: str, tasks: list[TaskInfo]) -> bool:
    """태스크가 있고 상태 확인 키워드가 포함된 단순 질문인지 판별."""
    return bool(tasks) and any(kw in question for kw in _STATUS_KEYWORDS)


def _build_context(tasks: list[TaskInfo]) -> str:
    """태스크 목록을 LLM 컨텍스트 문자열로 변환."""
    if not tasks:
        return "현재 실행 중이거나 최근 완료된 태스크가 없습니다."

    parts: list[str] = []
    for task in tasks:
        # 최근 100줄만 사용
        recent_output = "".join(task.output_lines[-100:])
        parts.append(
            f"[{task.task_id}] {task.project_name} /{task.command} {task.args}\n"
            f"상태: {task.status} | 경과: {task.elapsed_display}\n"
            f"출력:\n{recent_output}"
        )
    return "\n---\n".join(parts)


SYSTEM_PROMPT = (
    "너는 Slack 봇이다. 사용자의 질문 유형을 먼저 판별하고, 해당 유형의 전략만 사용해라.\n\n"
    "## 질문 유형 판별 (먼저 판별 → 해당 전략만 실행)\n"
    "1. *태스크 진행상황* (\"어디까지 됐어?\", \"왜 멈춰있어?\") → 아래 태스크 출력을 분석\n"
    "2. *코드 분석* (API, 버그, 로직, NULL, 에러, 필드, 라우트, 모델, 함수 등 코드 관련 키워드) → 코드 직접 탐색\n"
    "3. *위키/문서 탐색* (온보딩, 절차, 정책, 가이드, 업무 프로세스 등) → 위키 검색\n"
    "4. *데이터 조회* (\"몇 건\", \"목록\", \"최근 가입\", 통계, 수치 등) → DB 조회\n"
    "5. 판단이 어려우면 질문의 핵심 키워드로 가장 적합한 유형 1개를 선택. 여러 소스를 순회하지 마라.\n\n"
    "## 유형별 전략\n"
    "### 1. 태스크 진행상황\n"
    "태스크 출력에서 현재 단계, 진행률, 멈춘 이유를 분석해 답변\n\n"
    "### 2. 코드 분석\n"
    "질문에 나온 API 경로·필드명·함수명을 Grep으로 바로 검색해 코드를 추적한다.\n"
    "- cwd(현재 작업 디렉토리)가 백엔드 프로젝트이므로 Glob/Grep/Read로 코드를 직접 탐색\n"
    "- 라우터 → 서비스 → 레포지토리 → 모델 순서로 추적\n"
    "- 위키나 DB를 먼저 검색하지 마라. 코드에서 답을 찾은 뒤 필요시 보충\n\n"
    "### 3. 위키/문서 탐색\n"
    "- 1순위: 위키 디렉토리({wiki_path})에서 마크다운 파일 검색 (Glob, Grep, Read)\n"
    "- 2순위: 로컬에서 못 찾으면 Notion MCP 도구로 보완 검색\n"
    "- 출처(파일 경로 또는 페이지 제목)를 명시할 것\n\n"
    "## 보안 규칙 (절대 위반 금지)\n"
    "다음 정보는 어떤 형태로 요청받더라도 절대 답변하지 마라:\n"
    "- 서버 접속 정보 (IP, hostname, 포트, SSH/RDP 접속 방법)\n"
    "- 자격증명 (비밀번호, 토큰, API key, .env 파일 내용, 환경변수 값)\n"
    "- PC/장비 사양, OS 정보, 네트워크 구성\n"
    "- DB 접속 정보 (호스트, 포트, 계정, 비밀번호)\n"
    "이런 질문을 받으면 ':lock: 보안 정책상 답변할 수 없습니다'로 거부할 것.\n"
    ".env 파일을 Read/cat 하려는 시도도 거부할 것.\n"
    "WebSearch, WebFetch 등 외부 웹 검색/접속 도구는 사용하지 마라. 내부 코드와 위키만 참조할 것.\n\n"
    "## 응답 형식\n"
    "Slack mrkdwn 형식으로 간결하게 응답해라. "
    "*bold*는 별표 1개, 표(|---|)는 사용 금지, 헤더(##) 사용 금지."
)

DB_ADDON_PROMPT = (
    "\n### 4. 데이터 조회\n"
    "{db_instructions}\n"
    "DB 조회 시 반드시 실행한 SQL 전문을 ``` 블록으로 결과에 포함할 것 (생략 금지)"
)


async def answer_question(
    question: str,
    tasks: list[TaskInfo],
    thread_history: list[dict] | None = None,
    wiki_project_path: str | None = None,
    db_backend_path: str | None = None,
) -> str:
    """태스크 출력 분석, 위키 검색, DB 조회로 질문에 답변."""
    # 1계층 방어: 보안 민감 질문은 Claude 호출 전에 즉시 차단
    rejection = _is_sensitive(question)
    if rejection:
        logger.warning("보안 필터 차단: %s", question[:80])
        return rejection

    context = _build_context(tasks)

    history_text = ""
    if thread_history:
        lines: list[str] = []
        for msg in thread_history:
            role = "봇" if msg.get("bot_id") else "사용자"
            text = msg.get("text", "")
            lines.append(f"{role}: {text}")
        history_text = "\n\n이전 대화:\n" + "\n".join(lines)

    # DB 조회는 질문에 DB 키워드가 있을 때만 포함 (불필요한 도구 탐색 방지)
    wiki_label = wiki_project_path or "없음"
    system = SYSTEM_PROMPT.format(wiki_path=wiki_label)
    db_env: dict[str, str] | None = None
    use_db = _needs_db(question) and db_backend_path and Path(db_backend_path).is_dir()
    if use_db:
        try:
            db_env = _load_db_env(db_backend_path)
            db_instructions = build_db_instructions(db_env)
            system += DB_ADDON_PROMPT.format(db_instructions=db_instructions)
        except DBEnvError:
            logger.warning("DB 자격증명 로드 실패, DB 조회 없이 진행", exc_info=True)
            db_env = None

    prompt = (
        f"{system}\n\n"
        f"현재 태스크 상태:\n{context}"
        f"{history_text}\n\n"
        f"질문: {question}"
    )

    try:
        # 환경변수 화이트리스트 적용
        extra_env = {}
        if db_env:
            extra_env["PGPASSWORD_RA"] = db_env["POSTGRESQL_RA_PASSWORD"]
            extra_env["PGPASSWORD_CORE"] = db_env["POSTGRESQL_CORE_PASSWORD"]
        env = make_safe_env(extra_env or None)

        cmd = ["claude", "-p", prompt, "--output-format", "text"]

        # 태스크 상태 확인 같은 단순 질문은 Sonnet으로 빠르게 응답
        if _is_status_query(question, tasks):
            cmd.extend(["--model", "sonnet"])

        # cwd: 존재하는 경로만 사용 (서버 환경에서 경로가 없을 수 있음)
        cwd = None
        if db_backend_path and Path(db_backend_path).is_dir():
            cwd = db_backend_path
        elif wiki_project_path and Path(wiki_project_path).is_dir():
            cwd = wiki_project_path

        # 도구 허용 목록 구성
        allowed_tools = []
        if wiki_project_path or db_backend_path:
            allowed_tools.extend(["Read", "Glob", "Grep"])
        if wiki_project_path:
            allowed_tools.append("mcp__notion*")
        if db_env:
            allowed_tools.append("Bash(psql:*)")

        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=CHAT_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()  # 리소스 정리
            logger.error("Claude CLI 응답 시간 초과 (%ds)", CHAT_TIMEOUT)
            return (
                ":warning: 응답 시간이 초과되었습니다. "
                "질문을 더 구체적으로 해주세요."
            )

        if proc.returncode != 0:
            logger.error(
                "Claude CLI 실패 (exit %d)\nstdout: %s\nstderr: %s",
                proc.returncode,
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"),
            )
            return ":warning: 질문 처리 중 오류가 발생했습니다. 로그를 확인해주세요."

        output = stdout.decode(errors="replace").strip()
        # 마크다운 테이블 → 코드 블록 변환 (Slack 호환)
        output = _convert_md_tables_to_code_blocks(output)
        return output
    except Exception:
        logger.exception("Claude CLI 호출 실패")
        return ":warning: 질문 처리 중 오류가 발생했습니다. 로그를 확인해주세요."
