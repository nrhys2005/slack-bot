from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from slack_bot.config import ProjectConfig
from slack_bot.security import make_safe_env
from slack_bot.task_manager import TaskInfo

logger = logging.getLogger(__name__)

MAX_OUTPUT_LENGTH = 3900  # Slack 메시지 제한 (~4000) 여유분 확보
SUBPROCESS_TIMEOUT = 3600  # 1시간. harness 파이프라인은 오래 걸릴 수 있음


@dataclass
class RunResult:
    success: bool
    output: str
    return_code: int


async def run_claude(
    project: ProjectConfig, command: str, args: str, task: TaskInfo
) -> RunResult:
    """프로젝트 디렉토리에서 claude -p 를 비동기로 실행한다. stdout을 라인별로 누적한다."""
    # 비대화형 환경이므로 --auto 플래그를 항상 포함
    if args:
        arg_parts = args.split()
        if "--auto" not in arg_parts:
            arg_parts.append("--auto")
        args = " ".join(arg_parts)
    else:
        args = "--auto"

    prompt = f"/{command} {args}"

    env = make_safe_env()
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

    task.process = proc

    if proc.stdout is None:
        raise RuntimeError("stdout pipe not created")
    if proc.stderr is None:
        raise RuntimeError("stderr pipe not created")

    # stderr를 동시에 읽는 태스크 (deadlock 방지, 바이트 제한)
    async def _drain_stderr() -> bytes:
        return await proc.stderr.read(MAX_OUTPUT_LENGTH * 2)

    stderr_task = asyncio.create_task(_drain_stderr())

    # stdout 라인별 스트리밍 읽기
    async def _read_stdout() -> None:
        async for line in proc.stdout:
            task.output_lines.append(line.decode(errors="replace"))

    # stdout 스트리밍 + 프로세스 종료 대기를 병렬 실행하며 전체 타임아웃 적용
    try:
        await asyncio.wait_for(
            asyncio.gather(_read_stdout(), proc.wait()),
            timeout=SUBPROCESS_TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        stderr_task.cancel()
        logger.error(
            "Claude CLI 프로세스 시간 초과 (%ds), 강제 종료", SUBPROCESS_TIMEOUT
        )
        output = task.output_text.strip()
        if len(output) > MAX_OUTPUT_LENGTH:
            output = output[:MAX_OUTPUT_LENGTH] + "\n\n... (truncated)"
        timeout_msg = (
            f"\n\n:warning: 실행 시간이 {SUBPROCESS_TIMEOUT}초를 초과하여 "
            "강제 종료되었습니다."
        )
        return RunResult(success=False, output=output + timeout_msg, return_code=-1)

    stderr_data = await stderr_task

    # stderr 처리
    if not task.output_lines and stderr_data:
        task.output_lines.append(stderr_data.decode(errors="replace"))

    output = task.output_text.strip()
    if len(output) > MAX_OUTPUT_LENGTH:
        output = output[:MAX_OUTPUT_LENGTH] + "\n\n... (truncated)"

    return RunResult(
        success=proc.returncode == 0,
        output=output,
        return_code=proc.returncode or 0,
    )
