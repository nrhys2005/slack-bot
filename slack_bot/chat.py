from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from slack_bot.config import ProjectConfig
from slack_bot.db_query import (
    DBEnvError,
    _convert_md_tables_to_code_blocks,
    _load_db_env,
    build_db_instructions,
    build_sqlite_db_instructions,
)
from slack_bot.intent import DB_KEYWORDS
from slack_bot.security import make_safe_env
from slack_bot.task_manager import TaskInfo

logger = logging.getLogger(__name__)

CHAT_TIMEOUT = 300  # 5분

_STATUS_KEYWORDS = frozenset({"어디까지", "진행", "상태", "멈", "끝났"})


def _needs_db(question: str) -> bool:
    """질문에 DB 관련 키워드가 포함되어 있는지 판별."""
    return any(kw.lower() in question.lower() for kw in DB_KEYWORDS)


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


_BASE_SYSTEM_PROMPT = (
    "너는 Slack 프로젝트 관리 봇이다. 사용자의 질문 유형을 먼저 판별하고, 해당 유형의 전략만 사용해라.\n\n"
    "## 질문 유형 판별 (먼저 판별 → 해당 전략만 실행)\n"
    "1. *태스크 진행상황* (\"어디까지 됐어?\", \"왜 멈춰있어?\") → 아래 태스크 출력을 분석\n"
    "2. *프로젝트 상태 파악* (\"상태 어때?\", \"현황\", \"돌아가?\") → 프로젝트 코드/로그/설정 읽기\n"
    "3. *코드 분석* (API, 버그, 로직, 에러, 필드, 라우트, 모델, 함수 등) → 코드 직접 탐색\n"
    "4. *위키/문서 탐색* (온보딩, 절차, 정책, 가이드 등) → 위키 검색\n"
    "5. *데이터 조회* (\"몇 건\", \"목록\", \"최근 가입\", 통계 등) → DB 조회\n"
    "6. 판단이 어려우면 가장 적합한 유형 1개를 선택. 여러 소스를 순회하지 마라.\n\n"
    "## 유형별 전략\n"
    "### 1. 태스크 진행상황\n"
    "태스크 출력에서 현재 단계, 진행률, 멈춘 이유를 분석해 답변\n\n"
    "### 2. 프로젝트 상태 파악\n"
    "프로젝트의 코드, 로그, 설정 파일을 Glob/Grep/Read로 읽어서 현재 상태를 파악한다.\n"
    "{status_paths_section}"
    "\n### 3. 코드 분석\n"
    "질문에 나온 API 경로·필드명·함수명을 Grep으로 바로 검색해 코드를 추적한다.\n"
    "- Glob/Grep/Read로 코드를 직접 탐색\n"
    "- 라우터 → 서비스 → 레포지토리 → 모델 순서로 추적\n\n"
    "{wiki_section}"
    "{db_section}"
    "## 응답 형식\n"
    "Slack mrkdwn 형식으로 간결하게 응답해라. "
    "*bold*는 별표 1개, 표(|---|)는 사용 금지, 헤더(##) 사용 금지."
)


def _build_system_prompt(
    target_project: ProjectConfig | None = None,
    wiki_projects: list[ProjectConfig] | None = None,
    db_instructions: str | None = None,
) -> str:
    """프로젝트 컨텍스트에 따라 시스템 프롬프트를 동적으로 구성."""
    # 상태 파악 경로 섹션
    status_section = ""
    if target_project and target_project.status_paths:
        paths = ", ".join(f"`{target_project.path}/{p}`" for p in target_project.status_paths)
        status_section = f"- 대상 프로젝트({target_project.name})의 주요 경로: {paths}\n"
    elif target_project:
        status_section = f"- 대상 프로젝트({target_project.name}) 경로: `{target_project.path}`\n"

    # 위키 섹션
    wiki_section = ""
    if wiki_projects:
        wiki_paths = ", ".join(f"`{p.path}`" for p in wiki_projects)
        wiki_section = (
            "### 4. 위키/문서 탐색\n"
            f"- 1순위: 위키 디렉토리({wiki_paths})에서 마크다운 파일 검색 (Glob, Grep, Read)\n"
            "- 2순위: 로컬에서 못 찾으면 Notion MCP 도구로 보완 검색\n"
            "- 출처(파일 경로 또는 페이지 제목)를 명시할 것\n\n"
        )

    # DB 섹션
    db_section = ""
    if db_instructions:
        db_section = (
            "### 5. 데이터 조회\n"
            f"{db_instructions}\n"
            "DB 조회 시 반드시 실행한 SQL 전문을 ``` 블록으로 결과에 포함할 것 (생략 금지)\n\n"
        )

    return _BASE_SYSTEM_PROMPT.format(
        status_paths_section=status_section,
        wiki_section=wiki_section,
        db_section=db_section,
    )


async def answer_question(
    question: str,
    tasks: list[TaskInfo],
    thread_history: list[dict] | None = None,
    projects: dict[str, ProjectConfig] | None = None,
    target_project: ProjectConfig | None = None,
) -> str:
    """태스크 출력 분석, 위키 검색, DB 조회, 프로젝트 상태 파악으로 질문에 답변."""
    projects = projects or {}
    context = _build_context(tasks)

    history_text = ""
    if thread_history:
        lines: list[str] = []
        for msg in thread_history:
            role = "봇" if msg.get("bot_id") else "사용자"
            text = msg.get("text", "")
            lines.append(f"{role}: {text}")
        history_text = "\n\n이전 대화:\n" + "\n".join(lines)

    # 프로젝트 분류
    wiki_projects = [p for p in projects.values() if p.wiki]
    db_projects = {n: p for n, p in projects.items() if p.db is not None}

    # DB 프로젝트 결정: target이 DB 가능하면 target, 아니면 첫 번째 DB 프로젝트
    db_project: ProjectConfig | None = None
    db_envs: dict[str, dict[str, str]] | None = None
    db_instructions_text: str | None = None

    if _needs_db(question):
        if target_project and target_project.db:
            db_project = target_project
        elif db_projects:
            db_project = next(iter(db_projects.values()))

        if db_project and db_project.db:
            if db_project.db.db_type == "sqlite":
                db_instructions_text = build_sqlite_db_instructions(db_project)
            else:
                try:
                    db_envs = _load_db_env(db_project)
                    db_instructions_text = build_db_instructions(
                        db_envs, db_project.db.model_paths
                    )
                except DBEnvError:
                    logger.warning("DB 자격증명 로드 실패, DB 조회 없이 진행", exc_info=True)
                    db_envs = None

    system = _build_system_prompt(
        target_project=target_project,
        wiki_projects=wiki_projects,
        db_instructions=db_instructions_text,
    )

    prompt = (
        f"{system}\n\n"
        f"현재 태스크 상태:\n{context}"
        f"{history_text}\n\n"
        f"질문: {question}"
    )

    try:
        # 환경변수 화이트리스트 적용
        extra_env: dict[str, str] = {}
        if db_envs:
            for name, creds in db_envs.items():
                extra_env[f"PGPASSWORD_{name.upper()}"] = creds["password"]
        env = make_safe_env(extra_env or None)

        cmd = [
            "claude", "-p", prompt,
            "--output-format", "text",
            "--permission-mode", "bypassPermissions",
        ]

        # 태스크 상태 확인 같은 단순 질문은 Sonnet으로 빠르게 응답
        if _is_status_query(question, tasks):
            cmd.extend(["--model", "sonnet"])

        # cwd 선택: target_project > db_project > 첫 번째 wiki > None
        cwd = None
        if target_project and Path(target_project.path).is_dir():
            cwd = target_project.path
        elif db_project and Path(db_project.path).is_dir():
            cwd = db_project.path
        elif wiki_projects and Path(wiki_projects[0].path).is_dir():
            cwd = wiki_projects[0].path

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
            await proc.communicate()
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
        output = _convert_md_tables_to_code_blocks(output)
        return output
    except Exception:
        logger.exception("Claude CLI 호출 실패")
        return ":warning: 질문 처리 중 오류가 발생했습니다. 로그를 확인해주세요."
