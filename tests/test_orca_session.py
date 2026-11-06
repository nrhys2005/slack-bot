from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from slack_bot import orca_session
from slack_bot.config import ProjectConfig
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

    def __init__(self, exit_code=0, fail: str = "", output: bytes = b"answer\n"):
        self.calls: list[tuple[str, ...]] = []
        self.exit_code = exit_code
        self.fail = fail
        self.output = output
        self.session_dir = ""

    async def __call__(self, *args, timeout=None):
        self.calls.append(args)
        key = " ".join(args[:2])
        if key == self.fail:
            raise OrcaError(key)
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


def _git(status="", listing="", unpushed="0", branch_code=0):
    async def fake(path, *args):
        if args[0] == "status":
            return 0, status
        if args[0] == "worktree":
            return 0, listing
        if args[0] == "rev-parse":
            return 0, "/repo/.git\n"
        if args[0] == "rev-list":
            return 0, f"{unpushed}\n"
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
async def test_failure_before_start_returns_none_and_cleans_worktree(tmp_path):
    orca, git = FakeOrca(fail="terminal create"), _git()
    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", git):
        outcome = await run_in_session(_project(tmp_path), ["claude"], _task(), True, 60)

    assert outcome is None
    assert orca.called("worktree", "rm")


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


@pytest.mark.asyncio
async def test_sweep_skips_live_and_foreign_worktrees(tmp_path):
    worktrees = [
        {"id": "r::/w/1", "path": "/w/1", "branch": "refs/heads/1", "displayName": "slackbot-001-a"},
        {"id": "r::/w/2", "path": "/w/2", "branch": "refs/heads/2", "displayName": "slackbot-002-b"},
        {"id": "r::/w/3", "path": "/w/3", "branch": "refs/heads/3", "displayName": "my-feature"},
    ]
    calls: list[tuple[str, ...]] = []

    async def orca(*args, timeout=None):
        calls.append(args)
        if args[:2] == ("worktree", "list"):
            return {"worktrees": worktrees}
        if args[:2] == ("terminal", "list"):
            return {"terminals": [{"title": "slackbot-002-b"}]}
        return {}

    with patch.object(orca_session, "_orca", orca), patch.object(orca_session, "_git", _git()):
        await sweep_orphans({"p": _project(tmp_path)})

    removed = [c[c.index("--worktree") + 1] for c in calls if c[:2] == ("worktree", "rm")]
    assert removed == ["id:r::/w/1"]


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
