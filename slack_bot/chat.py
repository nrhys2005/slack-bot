from __future__ import annotations

import asyncio
import logging
import os

from slack_bot.task_manager import TaskInfo

logger = logging.getLogger(__name__)


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
    "2. 위키/문서/정보 탐색 질문 → Notion 도구로 검색하여 답변. 출처 페이지 제목을 명시할 것\n"
    "3. 판단이 어려우면 둘 다 활용\n"
    "Slack 마크다운 형식으로 간결하게 응답해라."
)


async def answer_question(
    question: str,
    tasks: list[TaskInfo],
    thread_history: list[dict] | None = None,
    wiki_project_path: str | None = None,
) -> str:
    """태스크 출력 분석 또는 위키 검색으로 질문에 답변."""
    context = _build_context(tasks)

    history_text = ""
    if thread_history:
        lines: list[str] = []
        for msg in thread_history:
            role = "봇" if msg.get("bot_id") else "사용자"
            text = msg.get("text", "")
            lines.append(f"{role}: {text}")
        history_text = f"\n\n이전 대화:\n" + "\n".join(lines)

    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"현재 태스크 상태:\n{context}"
        f"{history_text}\n\n"
        f"질문: {question}"
    )

    try:
        # ANTHROPIC_API_KEY를 제거하여 Claude Code OAuth 인증 사용
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        cmd = ["claude", "-p", prompt, "--output-format", "text"]

        # 위키 프로젝트가 설정되어 있으면 Notion MCP 도구 허용
        cwd = None
        if wiki_project_path:
            cwd = wiki_project_path
            cmd.extend(["--allowedTools", "mcp__mcp-server__notion_*"])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(
                "Claude CLI 실패 (exit %d)\nstdout: %s\nstderr: %s",
                proc.returncode,
                stdout.decode(),
                stderr.decode(),
            )
            return ":warning: 질문 처리 중 오류가 발생했습니다. 로그를 확인해주세요."

        return stdout.decode().strip()
    except Exception:
        logger.exception("Claude CLI 호출 실패")
        return ":warning: 질문 처리 중 오류가 발생했습니다. 로그를 확인해주세요."
