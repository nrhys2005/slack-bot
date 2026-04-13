from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from slack_bot.config import ProjectConfig
from slack_bot.task_manager import TaskInfo

logger = logging.getLogger(__name__)

MAX_OUTPUT_LENGTH = 3900  # Slack 메시지 제한 (~4000) 여유분 확보


@dataclass
class RunResult:
    success: bool
    output: str
    return_code: int


async def run_claude(project: ProjectConfig, command: str, args: str, task: TaskInfo) -> RunResult:
    """프로젝트 디렉토리에서 claude -p 를 비동기로 실행한다. stdout을 라인별로 누적한다."""
    # args에서 --auto 플래그 분리
    auto = False
    if args:
        arg_parts = args.split()
        if "--auto" in arg_parts:
            auto = True
            arg_parts.remove("--auto")
        args = " ".join(arg_parts)

    prompt = f"/{command} {args}" if args else f"/{command}"

    # ANTHROPIC_API_KEY를 제거하여 Claude Code OAuth 인증 사용
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    cmd = ["claude", "-p", prompt, "--output-format", "text"]
    if auto:
        cmd.append("--allowedTools")
        cmd.append("Edit,Write,Bash,Glob,Grep,Read,Agent,mcp__*")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=project.path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    task.process = proc

    # stdout 라인별 스트리밍 읽기
    assert proc.stdout is not None
    async for line in proc.stdout:
        task.output_lines.append(line.decode())

    await proc.wait()

    # stderr 처리
    assert proc.stderr is not None
    stderr_data = await proc.stderr.read()
    if not task.output_lines and stderr_data:
        task.output_lines.append(stderr_data.decode())

    output = task.output_text.strip()
    if len(output) > MAX_OUTPUT_LENGTH:
        output = output[:MAX_OUTPUT_LENGTH] + "\n\n... (truncated)"

    return RunResult(
        success=proc.returncode == 0,
        output=output,
        return_code=proc.returncode or 0,
    )
