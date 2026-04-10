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
    "너는 Slack 봇이다. "
    "사용자가 실행 중인 Claude Code 태스크에 대해 질문하면, "
    "아래 태스크 출력을 분석해서 간결하고 명확하게 답변해라. "
    "진행상황, 현재 단계, 멈춘 이유 등을 파악해서 알려줘. "
    "Slack 마크다운 형식으로 응답해라."
)


async def answer_question(question: str, tasks: list[TaskInfo]) -> str:
    """실행 중인 태스크 출력을 컨텍스트로 사용해 질문에 답변."""
    context = _build_context(tasks)

    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"현재 태스크 상태:\n{context}\n\n"
        f"질문: {question}"
    )

    try:
        # ANTHROPIC_API_KEY를 제거하여 Claude Code OAuth 인증 사용
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt, "--output-format", "text",
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
