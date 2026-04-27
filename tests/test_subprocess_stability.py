"""SB-PROC-01: subprocess 안정화 테스트."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slack_bot.config import ProjectConfig
from slack_bot.runner import run_claude
from slack_bot.task_manager import TaskInfo


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_task_info(channel: str = "C123") -> TaskInfo:
    return TaskInfo(
        task_id="001",
        project_name="test",
        command="harness",
        args="SB-01",
        user="U123",
        channel=channel,
        start_time=time.time(),
    )


class FakeStreamReader:
    """async for line in proc.stdout 를 시뮬레이션."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._index >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._index]
        self._index += 1
        return line

    async def read(self, n: int = -1) -> bytes:
        return b"".join(self._lines[self._index :])


def _make_fake_proc(
    stdout_lines: list[bytes],
    stderr_data: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    proc = MagicMock()
    proc.stdout = FakeStreamReader(stdout_lines)
    proc.stderr = FakeStreamReader([stderr_data] if stderr_data else [])
    proc.stderr.read = AsyncMock(return_value=stderr_data)
    proc.returncode = returncode

    async def _wait():
        return returncode

    proc.wait = _wait
    proc.kill = MagicMock()
    return proc


def _setup_asyncio_mock(mock_asyncio: MagicMock, proc: MagicMock) -> None:
    """runner.py 테스트용 asyncio mock 공통 설정."""
    mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
    mock_asyncio.subprocess = asyncio.subprocess
    mock_asyncio.create_task = lambda coro: asyncio.ensure_future(coro)
    mock_asyncio.wait_for = asyncio.wait_for
    mock_asyncio.gather = asyncio.gather
    mock_asyncio.TimeoutError = asyncio.TimeoutError


# ---------------------------------------------------------------------------
# runner.py 테스트
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_claude_success():
    """정상 종료 시 stdout/stderr 모두 캡처."""
    project = ProjectConfig(name="test", path="/tmp/test", commands=["harness"])
    task = _make_task_info()
    stdout_lines = [b"line1\n", b"line2\n"]
    proc = _make_fake_proc(stdout_lines, stderr_data=b"some warning", returncode=0)

    with patch("slack_bot.runner.asyncio") as mock_asyncio:
        _setup_asyncio_mock(mock_asyncio, proc)
        result = await run_claude(project, "harness", "SB-01", task)

    assert result.success is True
    assert result.return_code == 0
    assert "line1" in result.output
    assert "line2" in result.output
    assert len(task.output_lines) == 2


@pytest.mark.asyncio
async def test_run_claude_failure():
    """subprocess 비정상 종료 시 RunResult.success=False."""
    project = ProjectConfig(name="test", path="/tmp/test", commands=["harness"])
    task = _make_task_info()
    proc = _make_fake_proc([b"error output\n"], stderr_data=b"fatal", returncode=1)

    with patch("slack_bot.runner.asyncio") as mock_asyncio:
        _setup_asyncio_mock(mock_asyncio, proc)
        result = await run_claude(project, "harness", "SB-01", task)

    assert result.success is False
    assert result.return_code == 1


@pytest.mark.asyncio
async def test_run_claude_stderr_only():
    """stderr만 있고 stdout 없는 경우."""
    project = ProjectConfig(name="test", path="/tmp/test", commands=["harness"])
    task = _make_task_info()
    proc = _make_fake_proc([], stderr_data=b"error from stderr\n", returncode=1)

    with patch("slack_bot.runner.asyncio") as mock_asyncio:
        _setup_asyncio_mock(mock_asyncio, proc)
        result = await run_claude(project, "harness", "SB-01", task)

    assert "error from stderr" in result.output


@pytest.mark.asyncio
async def test_run_claude_non_utf8():
    """stdout에 non-UTF-8 바이트가 포함된 경우 decode(errors='replace')로 처리."""
    project = ProjectConfig(name="test", path="/tmp/test", commands=["harness"])
    task = _make_task_info()
    bad_bytes = b"hello \xff\xfe world\n"
    proc = _make_fake_proc([bad_bytes], returncode=0)

    with patch("slack_bot.runner.asyncio") as mock_asyncio:
        _setup_asyncio_mock(mock_asyncio, proc)
        result = await run_claude(project, "harness", "SB-01", task)

    assert result.success is True
    assert "\ufffd" in result.output or "hello" in result.output


@pytest.mark.asyncio
async def test_run_claude_timeout():
    """timeout 초과 시 프로세스 kill + 에러 메시지 반환."""
    project = ProjectConfig(name="test", path="/tmp/test", commands=["harness"])
    task = _make_task_info()

    proc = MagicMock()
    proc.stdout = FakeStreamReader([b"partial output\n"])
    proc.stderr = FakeStreamReader([])
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.returncode = None
    proc.kill = MagicMock()

    # proc.wait()는 kill 후 호출 시 정상 반환
    wait_call_count = 0

    async def _wait():
        nonlocal wait_call_count
        wait_call_count += 1
        return -9

    proc.wait = _wait

    async def fake_wait_for(coro, timeout):
        # gather가 반환하는 Future를 cancel하여 정리
        if hasattr(coro, "cancel"):
            coro.cancel()
            try:
                await coro
            except asyncio.CancelledError:
                pass
        elif hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with patch("slack_bot.runner.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.TimeoutError = asyncio.TimeoutError
        mock_asyncio.create_task = lambda coro: asyncio.ensure_future(coro)
        mock_asyncio.gather = asyncio.gather
        mock_asyncio.wait_for = fake_wait_for

        result = await run_claude(project, "harness", "SB-01", task)

    assert result.success is False
    assert result.return_code == -1
    assert "초과" in result.output
    proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# chat.py 테스트
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_question_timeout():
    """chat.py: communicate() timeout 시 에러 메시지 반환."""
    from slack_bot.chat import answer_question

    proc = MagicMock()
    proc.returncode = None
    proc.kill = MagicMock()

    # communicate 후 리소스 정리용
    async def _communicate_cleanup():
        return b"", b""

    proc.communicate = _communicate_cleanup

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError()

    with patch("slack_bot.chat.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.TimeoutError = asyncio.TimeoutError
        mock_asyncio.wait_for = fake_wait_for

        result = await answer_question("test question", [])

    assert "초과" in result


@pytest.mark.asyncio
async def test_answer_question_success():
    """chat.py: 정상 응답."""
    from slack_bot.chat import answer_question

    proc = MagicMock()
    proc.returncode = 0

    async def _communicate():
        return b"test answer", b""

    proc.communicate = _communicate

    with patch("slack_bot.chat.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = asyncio.wait_for

        result = await answer_question("test question", [])

    assert result == "test answer"


# ---------------------------------------------------------------------------
# db_query.py 테스트
# ---------------------------------------------------------------------------

_FAKE_DB_ENVS = {
    "ra": {
        "username": "u",
        "password": "p",
        "read_host": "h",
        "port": "5432",
        "db_name": "db",
    },
}


def _make_fake_db_project():
    from slack_bot.config import DBConfig, ProjectConfig
    return ProjectConfig(
        name="test-db",
        path="/tmp/db",
        db=DBConfig(
            env_file="app/.env",
            env_prefix={"ra": "POSTGRESQL_RA"},
            model_paths=["app/models/ra"],
        ),
    )


@pytest.mark.asyncio
async def test_run_db_query_timeout():
    """db_query.py: wait() timeout 시 에러 메시지 반환."""
    from slack_bot.db_query import run_db_query

    proc = MagicMock()
    proc.stdout = FakeStreamReader([b"partial\n"])
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.returncode = None
    proc.kill = MagicMock()

    async def _wait():
        return -9

    proc.wait = _wait

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError()

    with (
        patch("slack_bot.db_query._load_db_env", return_value=_FAKE_DB_ENVS),
        patch("slack_bot.db_query._build_system_prompt", return_value="fake prompt"),
        patch("slack_bot.db_query.asyncio") as mock_asyncio,
    ):
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.TimeoutError = asyncio.TimeoutError
        mock_asyncio.wait_for = fake_wait_for

        result = await run_db_query("test question", _make_fake_db_project())

    assert "초과" in result
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_run_db_query_assert_replaced():
    """db_query.py: proc.stdout가 None이면 RuntimeError (assert 대신 raise)."""
    from slack_bot.db_query import run_db_query

    proc = MagicMock()
    proc.stdout = None
    proc.stderr = MagicMock()
    proc.returncode = None

    with (
        patch("slack_bot.db_query._load_db_env", return_value=_FAKE_DB_ENVS),
        patch("slack_bot.db_query._build_system_prompt", return_value="fake prompt"),
        patch("slack_bot.db_query.asyncio") as mock_asyncio,
    ):
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.subprocess = asyncio.subprocess

        # RuntimeError -> except Exception -> :warning: 메시지
        result = await run_db_query("test", _make_fake_db_project())
        assert ":warning:" in result
