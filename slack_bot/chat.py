from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from slack_bot.db_query import (
    DBEnvError,
    _convert_md_tables_to_code_blocks,
    _load_db_env,
    build_db_instructions,
)
from slack_bot.task_manager import TaskInfo

logger = logging.getLogger(__name__)

CHAT_TIMEOUT = 300  # 5분


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
    "너는 Slack 봇이다. 사용자의 질문 유형에 따라 적절히 답변해라.\n"
    "1. 실행 중인 태스크에 대한 질문 → 아래 태스크 출력을 분석해서 진행상황, 현재 단계, 멈춘 이유 등을 답변\n"
    "2. 위키/문서/정보 탐색 질문 → 아래 우선순위로 검색:\n"
    "   - 1순위: 위키 디렉토리({wiki_path})에서 마크다운 파일 검색 (Glob, Grep, Read). 빠르고 정확함\n"
    "   - 2순위: 로컬에서 못 찾으면 Notion MCP 도구로 보완 검색\n"
    "   출처(파일 경로 또는 페이지 제목)를 명시할 것\n"
    "3. 판단이 어려우면 둘 다 활용\n"
    "Slack mrkdwn 형식으로 간결하게 응답해라. "
    "*bold*는 별표 1개, 표(|---|)는 사용 금지, 헤더(##) 사용 금지."
)

DB_ADDON_PROMPT = (
    "\n4. 데이터 조회/검색 질문 → DB에서 직접 조회:\n"
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
    context = _build_context(tasks)

    history_text = ""
    if thread_history:
        lines: list[str] = []
        for msg in thread_history:
            role = "봇" if msg.get("bot_id") else "사용자"
            text = msg.get("text", "")
            lines.append(f"{role}: {text}")
        history_text = "\n\n이전 대화:\n" + "\n".join(lines)

    # DB 조회 가능 시 시스템 프롬프트에 DB 지시사항 추가
    wiki_label = wiki_project_path or "없음"
    system = SYSTEM_PROMPT.format(wiki_path=wiki_label)
    db_env: dict[str, str] | None = None
    if db_backend_path and Path(db_backend_path).is_dir():
        try:
            db_env = _load_db_env(db_backend_path)
            db_instructions = build_db_instructions(db_env)
            system += DB_ADDON_PROMPT.format(db_instructions=db_instructions)
        except DBEnvError:
            logger.warning("DB 자격증명 로드 실패, DB 조회 없이 진행", exc_info=True)

    prompt = (
        f"{system}\n\n"
        f"현재 태스크 상태:\n{context}"
        f"{history_text}\n\n"
        f"질문: {question}"
    )

    try:
        # ANTHROPIC_API_KEY를 제거하여 Claude Code OAuth 인증 사용
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        if db_env:
            env["PGPASSWORD_RA"] = db_env["POSTGRESQL_RA_PASSWORD"]
            env["PGPASSWORD_CORE"] = db_env["POSTGRESQL_CORE_PASSWORD"]

        cmd = ["claude", "-p", prompt, "--output-format", "text"]

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
