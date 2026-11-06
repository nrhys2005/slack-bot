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
import re
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
# _new_session_name 이 만드는 이름과 정확히 맞는 것만 봇의 세션으로 본다.
SESSION_NAME_RE = re.compile(rf"^{SESSION_PREFIX}\d+-\d{{4}}-\d{{6}}$")
SESSION_ROOT = os.path.join(tempfile.gettempdir(), "slack-bot-orca")
OUTPUT_POLL_SECONDS = 1.0
STDERR_READ_LIMIT = 7800
# 다시 만들 수 있는 산출물. 이것 말고 gitignore 된 파일이 있으면 워크트리를 지우지 않는다.
REBUILDABLE_IGNORED = {
    ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache",
}


class OrcaError(Exception):
    """orca CLI 호출이 실패했거나 응답을 해석할 수 없을 때. code 는 orca 가 돌려준 오류 코드다."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


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
        error = payload.get("error") or {}
        raise OrcaError(f"{args[:2]} 실패 (exit {code}): {error}", code=error.get("code", ""))
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


def _is_rebuildable(ignored_path: str) -> bool:
    parts = ignored_path.strip('"').rstrip("/").split("/")
    return any(part in REBUILDABLE_IGNORED for part in parts) or parts[-1].endswith(".pyc")


async def _keep_reasons(worktree_path: str) -> list[str]:
    """워크트리를 지우면 작업을 잃는 이유를 돌려준다. 확인하지 못해도 지우지 않는 쪽으로 판단한다."""
    code, status = await _git(worktree_path, "status", "--porcelain", "--ignored")
    if code != 0:
        return ["git status 를 확인하지 못함"]
    reasons = []
    lines = status.splitlines()
    if any(line.strip() and not line.startswith("!!") for line in lines):
        reasons.append("커밋하지 않은 변경이 있음")
    ignored = [line[3:] for line in lines if line.startswith("!!")]
    kept = [path for path in ignored if not _is_rebuildable(path)]
    if kept:
        reasons.append(f"gitignore 된 파일이 있음 ({', '.join(kept[:5])})")

    code, count = await _git(
        worktree_path, "rev-list", "--count", "HEAD", "--not", "--branches", "--remotes"
    )
    if code != 0:
        return [*reasons, "HEAD 의 커밋을 확인하지 못함"]
    if count.strip() != "0":
        reasons.append("어떤 브랜치에도 없는 커밋이 HEAD 에 있음")

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


async def _close_terminal(handle: str) -> str:
    """터미널을 닫는다. 닫지 못해 명령이 계속 돌 수 있으면 안내 문구를 돌려준다."""
    try:
        await _orca("terminal", "close", "--terminal", handle, "--tab")
    except OrcaError as e:
        # 이미 끝난 터미널은 핸들이 무효가 되어 이 코드로 실패한다.
        if e.code == "terminal_handle_stale":
            return ""
        logger.warning("orca 터미널 닫기 실패 %s: %s", handle, e)
        return f"터미널 `{handle}` 을 닫지 못해 명령이 계속 돌 수 있습니다."
    return ""


async def _close_unconfirmed_terminal(selector: str, name: str) -> str:
    """terminal create 의 성공 여부를 모를 때 만들어졌을지 모를 터미널을 찾아 닫는다."""
    warning = (
        f"orca 터미널 시작 결과를 확인하지 못했습니다. 명령이 돌고 있을 수 있으니 "
        f"`orca-ide terminal list` 에서 `{name}` 을 확인해 주세요."
    )
    try:
        listed = await _orca("terminal", "list", "--worktree", selector)
        for terminal in listed.get("terminals") or []:
            if terminal.get("title") == name:
                if await _close_terminal(terminal["handle"]):
                    return warning
                return "orca 터미널 시작 결과를 확인하지 못해 찾아서 닫았습니다."
    except (OrcaError, KeyError, OSError, asyncio.TimeoutError) as e:
        logger.warning("orca 터미널 확인 실패 %s: %s", name, e)
    return warning


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
        try:
            wait = waiter.result().get("wait") or {}
        except OrcaError as e:
            if e.code == "timeout":
                return -1, True
            raise
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

    terminal create 를 부르기 전에 실패하면 None 을 돌려준다. 호출자가 직접 실행으로 넘어갈 수 있다.
    그 뒤의 실패는 None 으로 돌려주지 않는다. terminal create 는 실패로 끝나도 터미널이 만들어졌을
    수 있고, 같은 작업을 두 번 돌리면 안 되기 때문이다.
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
    except (OrcaError, KeyError, OSError) as e:
        logger.warning("orca 세션 시작 실패 (%s): %s", project.name, e)
        if worktree is not None:
            await _cleanup_worktree(worktree)
        shutil.rmtree(session_dir, ignore_errors=True)
        return None

    try:
        created = await _orca(
            "terminal", "create", "--worktree", selector, "--title", name,
            "--command", f"bash {shlex.quote(script)}; exit $?",
        )
        terminal = created["terminal"]["handle"]
    except (OrcaError, KeyError, OSError) as e:
        logger.error("orca 터미널 시작 결과 불명 (%s): %s", project.name, e)
        # 워크트리는 _cleanup_worktree 가 터미널을 모두 닫고, 체크아웃은 이 세션의 터미널만 닫는다.
        note = await _close_unconfirmed_terminal(selector, name)
        cleanup_note = await _cleanup_worktree(worktree) if worktree is not None else ""
        # run.sh 를 지워 두면 터미널이 늦게 뜨더라도 명령이 시작되지 않는다.
        shutil.rmtree(session_dir, ignore_errors=True)
        return SessionOutcome(
            return_code=-1,
            cleanup_note=" ".join(part for part in (note, cleanup_note) if part),
        )

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
        close_note = await _close_terminal(terminal)
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
        cleanup_note=" ".join(part for part in (note, close_note, cleanup_note) if part),
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
    이번 실행의 세션과 섞이지 않도록 Slack 연결을 열기 전에 끝내야 한다.
    """
    orca_projects = [p for p in projects.values() if p.orca]
    if not orca_projects:
        return
    try:
        listed = await _orca("terminal", "list")
    except (OrcaError, OSError, asyncio.TimeoutError) as e:
        logger.warning("orca 고아 세션 점검 실패: %s", e)
        return
    # 일부만 받았으면 돌고 있는 세션을 끝난 것으로 잘못 볼 수 있다.
    if listed.get("truncated"):
        logger.warning("orca 터미널 목록이 잘려 고아 세션 정리를 건너뜁니다")
        return
    # 터미널 제목은 안에서 도는 프로그램이 바꿀 수 있어 경로로 판단한다. 끝난 터미널은 목록에서 빠진다.
    live_paths = {t.get("worktreePath") for t in listed.get("terminals") or []}

    for project in orca_projects:
        try:
            await _sweep_project(project, live_paths)
        except (OrcaError, KeyError, OSError, asyncio.TimeoutError) as e:
            logger.warning("orca 고아 세션 정리 실패 (%s): %s", project.name, e)

    # 실행 중인 세션의 폴더를 지워도 열린 파일에 계속 쓰고, 그 출력을 읽을 봇 태스크는 이미 없다.
    if os.path.isdir(SESSION_ROOT):
        for entry in os.listdir(SESSION_ROOT):
            shutil.rmtree(os.path.join(SESSION_ROOT, entry), ignore_errors=True)


async def _sweep_project(project: ProjectConfig, live_paths: set) -> None:
    listed = await _orca("worktree", "list", "--repo", f"path:{project.path}")
    for worktree in listed.get("worktrees") or []:
        name = worktree.get("displayName") or ""
        path = worktree["path"]
        if (
            worktree.get("isMainWorktree")
            or not SESSION_NAME_RE.match(name)
            or path in live_paths
        ):
            continue
        # 남겨 둔 워크트리는 사용자가 들어가 작업 중일 수 있으니 터미널도 닫지 않는다.
        reasons = await _keep_reasons(path)
        if reasons:
            logger.info("orca 세션 %s 를 남김: %s", name, ", ".join(reasons))
            continue
        note = await _cleanup_worktree(worktree)
        logger.info("orca 고아 세션 정리 %s: %s", name, note or "완료")
