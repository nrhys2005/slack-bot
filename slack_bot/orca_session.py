"""orca 터미널 세션으로 claude -p 를 실행하고, 끝나면 세션과 워크트리를 정리한다.

orca 터미널은 PTY 라서 출력이 재그리기 조각으로 섞이고 종료 직후에는 읽히지 않는다(tail 이 빔).
그래서 실행 스크립트가 stdout/stderr 를 파일로 남기고 봇은 그 파일을 읽는다.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass, field

from slack_bot.config import ProjectConfig
from slack_bot.task_manager import TaskInfo

logger = logging.getLogger(__name__)

ORCA_BIN = "orca-ide"
ORCA_CLI_TIMEOUT = 120
SESSION_PREFIX = "slackbot-"
SESSION_ROOT = os.path.join(tempfile.gettempdir(), "slack-bot-orca")
OUTPUT_POLL_SECONDS = 1.0
STDERR_READ_LIMIT = 7800


class OrcaError(Exception):
    """orca CLI 호출이 실패했거나 응답을 해석할 수 없을 때."""


@dataclass
class SessionOutcome:
    return_code: int
    timed_out: bool = False
    # 정리하지 못하고 남긴 것에 대한 사용자 안내. 없으면 빈 문자열.
    cleanup_note: str = ""


@dataclass
class OrcaSessionHandle:
    """TaskInfo.process 자리에 넣는 핸들. stop_task 의 terminate 를 세션 종료 요청으로 바꾼다."""

    pid: None = None
    returncode: int | None = None
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)

    def terminate(self) -> None:
        self.stop_requested.set()


async def _exec(
    *args: str, cwd: str | None = None, timeout: float = ORCA_CLI_TIMEOUT
) -> tuple[int, str]:
    """명령을 실행해 (종료 코드, stdout) 을 돌려준다. 시간 초과나 취소 때는 프로세스를 끝낸다."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout.decode(errors="replace")


async def _orca(*args: str, timeout: float = ORCA_CLI_TIMEOUT) -> dict:
    """orca CLI 를 --json 으로 호출해 result 를 돌려준다."""
    try:
        code, stdout = await _exec(ORCA_BIN, *args, "--json", timeout=timeout)
    except (OSError, asyncio.TimeoutError) as e:
        raise OrcaError(f"{args[:2]} 호출 실패: {e!r}") from e
    try:
        payload = json.loads(stdout)
    except ValueError as e:
        raise OrcaError(f"{args[:2]} 응답 해석 실패 (exit {code}): {stdout[:300]}") from e
    if code != 0 or not payload.get("ok"):
        raise OrcaError(f"{args[:2]} 실패 (exit {code}): {payload.get('error')}")
    return payload.get("result") or {}


async def _git(path: str, *args: str) -> tuple[int, str]:
    """path 는 작업 트리나 .git 디렉토리다. 워크트리를 지운 뒤의 브랜치 삭제는 .git 에서 실행한다."""
    return await _exec("git", "-C", path, *args)


def _new_session_name(task_id: str) -> str:
    return f"{SESSION_PREFIX}{task_id}-{time.strftime('%m%d-%H%M%S')}"


def _write_script(session_dir: str, cmd: list[str]) -> str:
    """실행 스크립트를 쓴다. 프롬프트를 터미널에 직접 입력하면 PTY 줄 길이 제한에 걸리므로 파일로 넘긴다."""
    script = os.path.join(session_dir, "run.sh")
    stdout_path = shlex.quote(os.path.join(session_dir, "stdout"))
    stderr_path = shlex.quote(os.path.join(session_dir, "stderr"))
    with open(script, "w", encoding="utf-8") as f:
        f.write(
            "#!/bin/bash\n"
            f"{shlex.join(cmd)} </dev/null 2>{stderr_path} | tee {stdout_path}\n"
            "exit ${PIPESTATUS[0]}\n"
        )
    return script


async def _keep_reasons(worktree_path: str) -> list[str]:
    """워크트리를 지우면 작업을 잃는 이유를 돌려준다. 확인하지 못해도 지우지 않는 쪽으로 판단한다."""
    code, status = await _git(worktree_path, "status", "--porcelain")
    if code != 0:
        return ["git status 를 확인하지 못함"]
    reasons = []
    if status.strip():
        reasons.append("커밋하지 않은 변경이 있음")

    code, listing = await _git(worktree_path, "worktree", "list", "--porcelain")
    if code != 0:
        return [*reasons, "하위 워크트리를 확인하지 못함"]
    nested_prefix = os.path.join(worktree_path, "")
    if any(
        line.startswith(f"worktree {nested_prefix}") for line in listing.splitlines()
    ):
        reasons.append("안에 다른 워크트리가 있음")
    return reasons


async def _remove_branch_if_safe(git_dir: str, branch: str) -> str:
    """원격에 올라가지 않은 커밋이 없을 때만 로컬 브랜치를 지운다. 남기면 안내 문구를 돌려준다."""
    name = branch.removeprefix("refs/heads/")
    code, count = await _git(
        git_dir, "rev-list", "--count", f"refs/heads/{name}", "--not", "--remotes"
    )
    if code != 0:
        return f"브랜치 `{name}` 상태를 확인하지 못해 남겼습니다."
    if count.strip() != "0":
        return f"브랜치 `{name}` 에 푸시되지 않은 커밋 {count.strip()}개가 있어 남겼습니다."
    code, output = await _git(git_dir, "branch", "-D", name)
    if code != 0:
        return f"브랜치 `{name}` 을 지우지 못했습니다: {output.strip()[:200]}"
    return ""


async def _cleanup_worktree(worktree: dict) -> str:
    """터미널을 모두 닫고, 작업을 잃지 않을 때만 워크트리와 브랜치를 지운다. 남긴 내용을 돌려준다."""
    selector = f"id:{worktree['id']}"
    path = worktree["path"]
    try:
        await _orca("terminal", "close", "--worktree", selector, "--all")
        reasons = await _keep_reasons(path)
        if reasons:
            return f"워크트리 `{path}` 를 남겼습니다: {', '.join(reasons)}."
        code, git_dir = await _git(path, "rev-parse", "--git-common-dir")
        if code != 0:
            return f"워크트리 `{path}` 의 저장소를 찾지 못해 남겼습니다."
        # --force 는 쓰지 않는다. 위 확인 뒤에 생긴 변경이 있으면 orca 가 삭제를 거부한다.
        await _orca("worktree", "rm", "--worktree", selector)
        return await _remove_branch_if_safe(git_dir.strip(), worktree["branch"])
    except (OrcaError, OSError, asyncio.TimeoutError) as e:
        logger.warning("orca 워크트리 정리 실패 %s: %s", path, e)
        return f"워크트리 `{path}` 를 정리하지 못했습니다."


async def _close_terminal(handle: str) -> None:
    try:
        await _orca("terminal", "close", "--terminal", handle, "--tab")
    except OrcaError as e:
        # 이미 끝난 터미널이면 닫을 것이 없어 실패할 수 있다.
        logger.info("orca 터미널 닫기 실패 %s: %s", handle, e)


async def _pump_output(stdout_path: str, task: TaskInfo, stop: asyncio.Event) -> None:
    """stdout 파일에 새로 쌓인 내용을 task.output_lines 로 옮긴다. stop 뒤 한 번 더 읽고 끝낸다."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    offset = 0
    while True:
        stopping = stop.is_set()
        try:
            with open(stdout_path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
        except FileNotFoundError:
            chunk = b""
        if chunk:
            offset += len(chunk)
            text = decoder.decode(chunk)
            if text:
                task.output_lines.append(text)
        if stopping:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=OUTPUT_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _wait_terminal(
    terminal: str, handle: OrcaSessionHandle, timeout_seconds: int
) -> tuple[int, bool]:
    """터미널 종료나 중단 요청을 기다려 (종료 코드, 시간 초과 여부) 를 돌려준다."""
    waiter = asyncio.create_task(
        _orca(
            "terminal", "wait", "--terminal", terminal, "--for", "exit",
            "--timeout-ms", str(timeout_seconds * 1000),
            timeout=timeout_seconds + 30,
        )
    )
    stopper = asyncio.create_task(handle.stop_requested.wait())
    try:
        await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if handle.stop_requested.is_set():
            return -1, False
        wait = waiter.result().get("wait") or {}
        if not wait.get("satisfied"):
            return -1, True
        exit_code = wait.get("exitCode")
        return (-1 if exit_code is None else int(exit_code)), False
    finally:
        stopper.cancel()
        waiter.cancel()


async def run_in_session(
    project: ProjectConfig,
    cmd: list[str],
    task: TaskInfo,
    isolated: bool,
    timeout_seconds: int,
) -> SessionOutcome | None:
    """cmd 를 orca 세션에서 실행한다. isolated 면 새 워크트리에서, 아니면 프로젝트 체크아웃에서 돈다.

    명령을 시작하기 전에 orca 가 실패하면 None 을 돌려준다. 호출자가 직접 실행으로 넘어갈 수 있다.
    시작한 뒤의 실패는 None 으로 돌려주지 않는다. 같은 작업을 두 번 돌리면 안 되기 때문이다.
    """
    handle = OrcaSessionHandle()
    task.attach_process(handle)

    name = _new_session_name(task.task_id)
    worktree: dict | None = None
    terminal = ""
    session_dir = os.path.join(SESSION_ROOT, name)
    try:
        await _orca("repo", "add", "--path", project.path)
        selector = f"path:{project.path}"
        if isolated:
            created = await _orca(
                "worktree", "create", "--name", name, "--repo", f"path:{project.path}",
                "--setup", "skip", "--no-parent",
            )
            worktree = created["worktree"]
            selector = f"id:{worktree['id']}"
        if handle.stop_requested.is_set():
            if worktree is not None:
                await _cleanup_worktree(worktree)
            return SessionOutcome(return_code=-1)

        os.makedirs(session_dir, mode=0o700)
        script = _write_script(session_dir, cmd)
        created = await _orca(
            "terminal", "create", "--worktree", selector, "--title", name,
            "--command", f"bash {shlex.quote(script)}; exit $?",
        )
        terminal = created["terminal"]["handle"]
    except (OrcaError, KeyError, OSError) as e:
        logger.warning("orca 세션 시작 실패 (%s): %s", project.name, e)
        if worktree is not None:
            await _cleanup_worktree(worktree)
        shutil.rmtree(session_dir, ignore_errors=True)
        return None

    pump_stop = asyncio.Event()
    pump = asyncio.create_task(
        _pump_output(os.path.join(session_dir, "stdout"), task, pump_stop)
    )
    return_code, timed_out = -1, False
    note = ""
    try:
        return_code, timed_out = await _wait_terminal(
            terminal, handle, timeout_seconds
        )
    except (OrcaError, OSError, asyncio.TimeoutError) as e:
        logger.error("orca 터미널 대기 실패 %s: %s", terminal, e)
        note = "orca 터미널의 종료를 확인하지 못했습니다."
    finally:
        handle.returncode = return_code
        # 정리는 작업 결과와 상관없이 항상 한다. 터미널을 닫아야 파일도 더 늘어나지 않는다.
        await _close_terminal(terminal)
        pump_stop.set()
        await pump
        if not task.output_lines:
            stderr_text = _read_stderr(session_dir)
            if stderr_text:
                task.output_lines.append(stderr_text)
        cleanup_note = ""
        if worktree is not None:
            cleanup_note = await _cleanup_worktree(worktree)
        shutil.rmtree(session_dir, ignore_errors=True)

    return SessionOutcome(
        return_code=return_code,
        timed_out=timed_out,
        cleanup_note=" ".join(part for part in (note, cleanup_note) if part),
    )


def _read_stderr(session_dir: str) -> str:
    try:
        with open(os.path.join(session_dir, "stderr"), "rb") as f:
            return f.read(STDERR_READ_LIMIT).decode(errors="replace")
    except FileNotFoundError:
        return ""


async def sweep_orphans(projects: dict[str, ProjectConfig]) -> None:
    """봇이 재시작되며 주인을 잃은 세션을 정리한다. 터미널이 아직 돌고 있는 세션은 건드리지 않는다.

    봇이 재시작돼도 orca 터미널은 계속 돌기 때문에 이전 실행의 워크트리가 남을 수 있다.
    """
    if not any(p.orca for p in projects.values()):
        return
    try:
        worktrees = (await _orca("worktree", "list")).get("worktrees") or []
        terminals = (await _orca("terminal", "list")).get("terminals") or []
    except OrcaError as e:
        logger.warning("orca 고아 세션 점검 실패: %s", e)
        return

    live_titles = {t.get("title") for t in terminals}
    for worktree in worktrees:
        name = worktree.get("displayName") or ""
        if not name.startswith(SESSION_PREFIX) or name in live_titles:
            continue
        # 남겨 둔 워크트리는 사용자가 들어가 작업 중일 수 있으니 터미널도 닫지 않는다.
        reasons = await _keep_reasons(worktree["path"])
        if reasons:
            logger.info("orca 세션 %s 를 남김: %s", name, ", ".join(reasons))
            continue
        note = await _cleanup_worktree(worktree)
        logger.info("orca 고아 세션 정리 %s: %s", name, note or "완료")

    if os.path.isdir(SESSION_ROOT):
        for entry in os.listdir(SESSION_ROOT):
            if entry not in live_titles:
                shutil.rmtree(os.path.join(SESSION_ROOT, entry), ignore_errors=True)
