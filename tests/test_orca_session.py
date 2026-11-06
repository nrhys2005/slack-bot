from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from slack_bot import orca_session
from slack_bot.config import ProjectConfig
from slack_bot.handlers import _cleanup_note_suffix
from slack_bot.orca_session import OrcaError, SessionOutcome, run_in_session, sweep_orphans
from slack_bot.runner import run_claude, run_free_prompt
from slack_bot.task_manager import TaskInfo


def _task(status: str = "running") -> TaskInfo:
    return TaskInfo(
        task_id="001", project_name="p", command="harness", args="", user="u",
        channel="c", start_time=0.0, status=status,
    )


def _project(tmp_path, orca: bool = True) -> ProjectConfig:
    return ProjectConfig(name="p", path=str(tmp_path), orca=orca)


WORKTREE = {"id": "r::/wt/a", "path": "/wt/a", "branch": "refs/heads/me/a"}


class FakeOrca:
    """orca CLI 호출을 기록하고 정해 둔 응답을 돌려준다."""

    def __init__(self, exit_code=0, fail: str = "", output: bytes = b"answer\n", fail_code: str = ""):
        self.calls: list[tuple[str, ...]] = []
        self.exit_code = exit_code
        self.fail = fail
        self.output = output
        self.fail_code = fail_code
        self.session_dir = ""

    async def __call__(self, *args, timeout=None):
        self.calls.append(args)
        key = " ".join(args[:2])
        if key == self.fail:
            raise OrcaError(key, code=self.fail_code)
        if key == "worktree create":
            return {"worktree": dict(WORKTREE)}
        if key == "terminal create":
            command = args[args.index("--command") + 1]
            self.session_dir = os.path.dirname(command.split()[1])
            with open(os.path.join(self.session_dir, "stdout"), "wb") as f:
                f.write(self.output)
            return {"terminal": {"handle": "term_1"}}
        if key == "terminal wait":
            return {"wait": {"satisfied": True, "exitCode": self.exit_code}}
        return {}

    def called(self, *prefix) -> bool:
        return any(c[: len(prefix)] == prefix for c in self.calls)


def _git(status="", listing="", unpushed="0", branch_code=0, head_orphans="0"):
    async def fake(path, *args):
        if args[0] == "status":
            return 0, status
        if args[0] == "worktree":
            return 0, listing
        if args[0] == "rev-parse":
            return 0, "/repo/.git\n"
        if args[0] == "rev-list":
            return 0, f"{head_orphans if 'HEAD' in args else unpushed}\n"
        if args[0] == "branch":
            return branch_code, ""
        raise AssertionError(args)

    return AsyncMock(side_effect=fake)


@pytest.fixture(autouse=True)
def _session_root(tmp_path, monkeypatch):
    monkeypatch.setattr(orca_session, "SESSION_ROOT", str(tmp_path / "sessions"))


@pytest.mark.asyncio
async def test_isolated_run_collects_output_and_removes_everything(tmp_path):
    orca, git = FakeOrca(), _git()
    task = _task()
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", git):
        outcome = await run_in_session(_project(tmp_path), ["claude"], task, True, 60)

    assert outcome == SessionOutcome(return_code=0)
    assert task.output_text == "answer\n"
    assert orca.called("terminal", "close", "--worktree", "id:r::/wt/a", "--all")
    rm = next(c for c in orca.calls if c[:2] == ("worktree", "rm"))
    assert "--force" not in rm
    git.assert_any_await("/repo/.git", "branch", "-D", "me/a")
    assert not os.path.exists(orca.session_dir)


@pytest.mark.asyncio
async def test_non_isolated_run_uses_checkout_and_touches_no_worktree(tmp_path):
    orca, git = FakeOrca(), _git()
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", git):
        await run_in_session(_project(tmp_path), ["claude"], _task(), False, 60)

    create = next(c for c in orca.calls if c[:2] == ("terminal", "create"))
    assert f"path:{tmp_path}" in create
    assert not orca.called("worktree", "create")
    assert not orca.called("worktree", "rm")
    assert orca.called("terminal", "close", "--terminal", "term_1")


@pytest.mark.parametrize(
    ("git_kwargs", "expected"),
    [
        ({"status": " M a.py\n"}, "커밋하지 않은 변경"),
        ({"listing": "worktree /wt/a\nworktree /wt/a/.claude/worktrees/x\n"}, "다른 워크트리"),
        ({"head_orphans": "1"}, "어떤 브랜치에도 없는 커밋"),
        ({"status": "!! .venv/\n!! src/__pycache__/\n!! .plans/A-1.md\n"}, ".plans/A-1.md"),
    ],
)
@pytest.mark.asyncio
async def test_worktree_with_work_is_kept(tmp_path, git_kwargs, expected):
    orca, git = FakeOrca(), _git(**git_kwargs)
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", git):
        outcome = await run_in_session(_project(tmp_path), ["claude"], _task(), True, 60)

    assert not orca.called("worktree", "rm")
    assert expected in outcome.cleanup_note
    assert "/wt/a" in outcome.cleanup_note
    # 터미널은 작업을 남기더라도 닫는다
    assert orca.called("terminal", "close", "--worktree")


@pytest.mark.asyncio
async def test_branch_with_unpushed_commits_is_kept(tmp_path):
    orca, git = FakeOrca(), _git(unpushed="2")
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", git):
        outcome = await run_in_session(_project(tmp_path), ["claude"], _task(), True, 60)

    assert orca.called("worktree", "rm")
    assert all(c.args[1] != "branch" for c in git.await_args_list)
    assert "me/a" in outcome.cleanup_note and "2개" in outcome.cleanup_note


@pytest.mark.asyncio
async def test_rebuildable_ignored_files_do_not_keep_worktree(tmp_path):
    orca, git = FakeOrca(), _git(status="!! .venv/\n!! src/__pycache__/\n!! a.pyc\n")
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", git):
        outcome = await run_in_session(_project(tmp_path), ["claude"], _task(), True, 60)

    assert orca.called("worktree", "rm")
    assert outcome.cleanup_note == ""


@pytest.mark.asyncio
async def test_failure_before_terminal_create_allows_fallback(tmp_path):
    orca, git = FakeOrca(fail="repo add"), _git()
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", git):
        outcome = await run_in_session(_project(tmp_path), ["claude"], _task(), True, 60)

    assert outcome is None
    assert not orca.called("terminal", "create")


@pytest.mark.parametrize("isolated", [True, False])
@pytest.mark.asyncio
async def test_terminal_create_failure_never_falls_back(tmp_path, isolated):
    # 호출은 실패로 끝났어도 터미널이 만들어졌을 수 있으므로 다시 실행하면 안 된다.
    class Orca(FakeOrca):
        async def __call__(self, *args, timeout=None):
            if args[:2] == ("terminal", "list"):
                self.calls.append(args)
                return {"terminals": [{"handle": "term_late", "title": self.created_title}]}
            if args[:2] == ("terminal", "create"):
                self.created_title = args[args.index("--title") + 1]
            return await super().__call__(*args, timeout=timeout)

    orca, git = Orca(fail="terminal create"), _git()
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", git):
        outcome = await run_in_session(_project(tmp_path), ["claude"], _task(), isolated, 60)

    assert outcome is not None
    assert outcome.return_code == -1
    assert orca.called("terminal", "close", "--terminal", "term_late")
    assert orca.called("worktree", "rm") is isolated
    assert "시작 결과를 확인하지 못" in outcome.cleanup_note


@pytest.mark.asyncio
async def test_wait_timeout_is_reported_as_timeout(tmp_path):
    orca, git = FakeOrca(fail="terminal wait", fail_code="timeout"), _git()
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", git):
        outcome = await run_in_session(_project(tmp_path), ["claude"], _task(), True, 60)

    assert outcome.timed_out is True
    assert outcome.cleanup_note == ""
    assert orca.called("terminal", "close", "--terminal", "term_1")


@pytest.mark.parametrize(
    ("fail_code", "warned"), [("terminal_handle_stale", False), ("runtime_unavailable", True)]
)
@pytest.mark.asyncio
async def test_close_failure_warns_unless_terminal_already_ended(tmp_path, fail_code, warned):
    orca = FakeOrca(fail="terminal close", fail_code=fail_code)
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", _git()):
        outcome = await run_in_session(_project(tmp_path), ["claude"], _task(), False, 60)

    assert ("닫지 못해" in outcome.cleanup_note) is warned


@pytest.mark.asyncio
async def test_wait_failure_after_start_does_not_ask_for_fallback(tmp_path):
    orca, git = FakeOrca(fail="terminal wait"), _git()
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", git):
        outcome = await run_in_session(_project(tmp_path), ["claude"], _task(), True, 60)

    assert outcome is not None
    assert outcome.return_code == -1
    assert "종료를 확인하지 못했습니다" in outcome.cleanup_note


@pytest.mark.asyncio
async def test_stopped_before_terminal_create_never_starts_command(tmp_path):
    orca, git = FakeOrca(), _git()
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", git):
        await run_in_session(_project(tmp_path), ["claude"], _task("stopped"), True, 60)

    assert not orca.called("terminal", "create")
    assert orca.called("worktree", "rm")


@pytest.mark.asyncio
async def test_stop_closes_terminal_while_running(tmp_path):
    import asyncio

    release = asyncio.Event()

    class BlockingOrca(FakeOrca):
        async def __call__(self, *args, timeout=None):
            if args[:2] == ("terminal", "wait"):
                self.calls.append(args)
                await release.wait()
                return {"wait": {"satisfied": True, "exitCode": 0}}
            return await super().__call__(*args, timeout=timeout)

    orca, git = BlockingOrca(), _git()
    task = _task()
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", git):
        running = asyncio.create_task(
            run_in_session(_project(tmp_path), ["claude"], task, True, 60)
        )
        while not orca.called("terminal", "wait"):
            await asyncio.sleep(0)
        task.process.terminate()
        outcome = await running

    assert outcome.return_code == -1
    assert orca.called("terminal", "close", "--worktree", "id:r::/wt/a", "--all")


def _sweep_orca(worktrees, terminals, truncated=False):
    calls: list[tuple[str, ...]] = []

    async def orca(*args, timeout=None):
        calls.append(args)
        if args[:2] == ("worktree", "list"):
            return {"worktrees": worktrees}
        if args[:2] == ("terminal", "list"):
            return {"terminals": terminals, "truncated": truncated}
        return {}

    return orca, calls


def _removed(calls) -> list[str]:
    return [c[c.index("--worktree") + 1] for c in calls if c[:2] == ("worktree", "rm")]


SWEEP_WORKTREES = [
    {"id": "r::/w/1", "path": "/w/1", "branch": "refs/heads/1", "displayName": "slackbot-001-1106-010203"},
    {"id": "r::/w/2", "path": "/w/2", "branch": "refs/heads/2", "displayName": "slackbot-002-1106-010203"},
    {"id": "r::/w/3", "path": "/w/3", "branch": "refs/heads/3", "displayName": "my-feature"},
    {"id": "r::/w/4", "path": "/w/4", "branch": "refs/heads/4", "displayName": "slackbot-docs"},
    {
        "id": "r::/w/5", "path": "/w/5", "branch": "refs/heads/5",
        "displayName": "slackbot-005-1106-010203", "isMainWorktree": True,
    },
]


@pytest.mark.asyncio
async def test_sweep_removes_only_ended_bot_sessions(tmp_path):
    # 실행 중인 터미널의 제목은 프로그램이 바꿀 수 있으므로 일부러 세션 이름과 다르게 둔다.
    terminals = [{"title": "ubuntu@pi: /w/2", "worktreePath": "/w/2"}]
    orca, calls = _sweep_orca(SWEEP_WORKTREES, terminals)
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", _git()):
        await sweep_orphans({"p": _project(tmp_path)})

    assert _removed(calls) == ["id:r::/w/1"]
    assert ("worktree", "list", "--repo", f"path:{tmp_path}") in calls


@pytest.mark.asyncio
async def test_sweep_skips_everything_when_terminal_list_is_truncated(tmp_path):
    orca, calls = _sweep_orca(SWEEP_WORKTREES, [], truncated=True)
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", _git()):
        await sweep_orphans({"p": _project(tmp_path)})

    assert _removed(calls) == []


@pytest.mark.asyncio
async def test_sweep_error_on_one_worktree_is_not_raised(tmp_path):
    orca, _ = _sweep_orca(SWEEP_WORKTREES, [])
    git = AsyncMock(side_effect=TimeoutError())
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", git):
        await sweep_orphans({"p": _project(tmp_path)})


@pytest.mark.asyncio
async def test_sweep_does_nothing_without_orca_projects(tmp_path):
    orca = AsyncMock()
    with patch.object(orca_session, "_orca", orca):
        await sweep_orphans({"p": _project(tmp_path, orca=False)})
    orca.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_routes_command_to_new_worktree_and_prompt_to_checkout(tmp_path):
    session = AsyncMock(return_value=SessionOutcome(return_code=0))
    with patch("slack_bot.runner.run_in_session", session):
        await run_claude(_project(tmp_path), "harness", "A-1", _task())
        await run_free_prompt(_project(tmp_path), "로그 봐 줘", _task())

    assert [c.args[3] for c in session.await_args_list] == [True, False]
    assert session.await_args_list[0].args[1][2] == "/harness A-1 --auto"


@pytest.mark.asyncio
async def test_runner_falls_back_to_direct_run_with_notice(tmp_path):
    from slack_bot import runner

    async def fake_stream():
        yield b"direct\n"

    class FakeProc:
        stdout = fake_stream()
        stderr = AsyncMock()
        returncode = 0
        pid = None

        async def wait(self):
            return 0

    FakeProc.stderr.read = AsyncMock(return_value=b"")
    spawn = AsyncMock(return_value=FakeProc())
    with (
        patch("slack_bot.runner.run_in_session", AsyncMock(return_value=None)),
        patch.object(runner.asyncio, "create_subprocess_exec", spawn),
    ):
        result = await run_free_prompt(_project(tmp_path), "질문", _task())

    assert spawn.await_count == 1
    assert "direct" in result.output
    assert "orca 세션을 만들지 못해" in result.output


@pytest.mark.asyncio
async def test_runner_exposes_cleanup_note_for_cancel_message(tmp_path):
    outcome = SessionOutcome(return_code=-1, cleanup_note="워크트리 `/w` 를 남겼습니다.")
    with patch("slack_bot.runner.run_in_session", AsyncMock(return_value=outcome)):
        result = await run_claude(_project(tmp_path), "harness", "A-1", _task())

    assert result.cleanup_note == outcome.cleanup_note
    assert _cleanup_note_suffix(result) == "\n:warning: 워크트리 `/w` 를 남겼습니다."
    assert _cleanup_note_suffix(None) == ""


@pytest.mark.asyncio
async def test_runner_does_not_use_orca_when_disabled(tmp_path):
    from slack_bot import runner

    session = AsyncMock()
    spawn = AsyncMock(side_effect=RuntimeError("direct"))
    with (
        patch("slack_bot.runner.run_in_session", session),
        patch.object(runner.asyncio, "create_subprocess_exec", spawn),
        pytest.raises(RuntimeError, match="direct"),
    ):
        await run_free_prompt(_project(tmp_path, orca=False), "질문", _task())
    session.assert_not_awaited()
