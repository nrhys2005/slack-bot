"""원격 운영 서버(SSH) 관리 테스트."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slack_bot.chat import _build_system_prompt
from slack_bot.config import ProjectConfig, RemoteConfig, load_projects
from slack_bot.handlers import _run_shell_and_report
from slack_bot.remote import build_remote_instructions, build_remote_shell_args
from slack_bot.task_manager import TaskInfo, TaskManager


def _remote_project(tmp_path) -> ProjectConfig:
    return ProjectConfig(
        name="cow",
        path=str(tmp_path),
        remote=RemoteConfig(
            host="cow-server",
            path="/Users/me/alert cow",
            notes="launchd com.alertcow.*",
        ),
    )


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_load_projects_parses_remote(tmp_path):
    cfg = tmp_path / "projects.yaml"
    cfg.write_text(
        "projects:\n"
        "  cow:\n"
        "    path: /tmp/cow\n"
        "    remote:\n"
        "      host: cow-server\n"
        "      path: /Users/me/alert-cow\n"
        "      notes: launchd\n"
        "  local:\n"
        "    path: /tmp/local\n"
        "  dash-host:\n"
        "    path: /tmp/d\n"
        "    remote:\n"
        "      host: -oProxyCommand=evil\n"
        "  no-host:\n"
        "    path: /tmp/x\n"
        "    remote:\n"
        "      path: /srv/x\n"
    )
    app_config = load_projects(str(cfg))
    remote = app_config.projects["cow"].remote
    assert remote == RemoteConfig(host="cow-server", path="/Users/me/alert-cow", notes="launchd")
    assert app_config.projects["local"].remote is None
    # host 가 없으면 원격 설정을 무시한다
    assert app_config.projects["no-host"].remote is None
    # "-" 로 시작하는 host 는 ssh 옵션 주입이 되므로 거부한다
    assert app_config.projects["dash-host"].remote is None


# ---------------------------------------------------------------------------
# 명령 조립
# ---------------------------------------------------------------------------


def test_build_remote_shell_args_quotes_path(tmp_path):
    args = build_remote_shell_args(_remote_project(tmp_path).remote, "launchctl list | grep alertcow")
    assert args[:7] == [
        "ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "cow-server",
    ]
    script = args[7]
    assert "cd '/Users/me/alert cow' && launchctl list | grep alertcow" in script
    assert script.startswith('export PATH="/opt/homebrew/bin:')


def test_build_remote_shell_args_without_path():
    args = build_remote_shell_args(RemoteConfig(host="h"), "uptime")
    assert "cd " not in args[-1]
    assert args[-1].endswith("uptime")


def test_build_remote_shell_args_expands_home_path():
    args = build_remote_shell_args(RemoteConfig(host="h", path="~/my svc"), "ls")
    assert "cd \"$HOME\"/'my svc' && ls" in args[-1]


def test_remote_instructions_empty_for_local_project(tmp_path):
    assert build_remote_instructions(ProjectConfig(name="x", path=str(tmp_path))) == ""


def test_system_prompt_includes_remote_section(tmp_path):
    prompt = _build_system_prompt(target_project=_remote_project(tmp_path))
    assert "원격 운영 서버 (cow)" in prompt
    assert "ssh -n -o BatchMode=yes -o ConnectTimeout=10 cow-server" in prompt
    assert "launchd com.alertcow.*" in prompt


# ---------------------------------------------------------------------------
# 실행 경로
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_free_prompt_appends_remote_system_prompt(tmp_path):
    from slack_bot.runner import run_free_prompt

    proc = MagicMock()
    proc.stdout.__aiter__.return_value = iter([])
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = 0
    task = TaskInfo(
        task_id="001", project_name="cow", command="prompt", args="상태",
        user="U1", channel="C1", start_time=time.time(),
    )
    with patch(
        "slack_bot.runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ) as mock_exec:
        await run_free_prompt(_remote_project(tmp_path), "서버 상태 봐 줘", task)

    cmd = list(mock_exec.call_args.args)
    idx = cmd.index("--append-system-prompt")
    assert "cow-server" in cmd[idx + 1]


@pytest.mark.asyncio
async def test_shell_exec_runs_over_ssh_for_remote_project(tmp_path):
    project = _remote_project(tmp_path)
    tm = TaskManager()
    task = await tm.create_task("cow", "shell", "uptime", "U1", "C1", thread_ts="1.0")

    proc = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = 0
    app = MagicMock()
    app.client.chat_postMessage = AsyncMock()

    with (
        patch(
            "slack_bot.handlers.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec,
        patch(
            "slack_bot.handlers.asyncio.create_subprocess_shell", new=AsyncMock()
        ) as mock_shell,
    ):
        await _run_shell_and_report(
            app, tm, project, task, str(tmp_path / "log"), asyncio.Semaphore(1),
        )

    mock_shell.assert_not_called()
    argv = list(mock_exec.call_args.args)
    assert argv[0] == "ssh" and "cow-server" in argv
    assert argv[-1].endswith("uptime")
