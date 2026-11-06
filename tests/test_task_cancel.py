"""SB-CANCEL-01: 시작 알림 [취소] 버튼과 대기 중 취소 가드 테스트."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

from slack_bot import handlers
from slack_bot.config import ProjectConfig
from slack_bot.handlers import (
    _BOOT_NONCE,
    _TASK_CANCEL_ACTION_ID,
    _build_progress_blocks,
    _handle_task_cancel,
    _post_progress,
    _run_and_report,
    _run_chat_question_and_report,
    _run_db_query_and_report,
    _run_db_query_export_and_report,
    _run_prompt_and_report,
    _run_shell_and_report,
)
from slack_bot.task_manager import TaskInfo, TaskManager

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_task(
    *,
    task_id: str = "001",
    user: str = "U1",
    channel: str = "C123",
    command: str = "harness",
    args: str = "SB-01",
) -> TaskInfo:
    return TaskInfo(
        task_id=task_id,
        project_name="trader",
        command=command,
        args=args,
        user=user,
        channel=channel,
        start_time=time.time(),
        thread_ts="1000.0",
    )


def _manager_with(task: TaskInfo) -> TaskManager:
    tm = TaskManager()
    tm._tasks[task.task_id] = task
    return tm


def _cancel_body(
    task_id: str = "001",
    *,
    nonce: str | None = None,
    user: str = "U1",
    channel: str = "C123",
    value: str | None = None,
) -> dict:
    if value is None:
        value = f"{task_id}:{_BOOT_NONCE if nonce is None else nonce}"
    return {
        "actions": [{"action_id": _TASK_CANCEL_ACTION_ID, "value": value}],
        "user": {"id": user},
        "channel": {"id": channel},
        "message": {
            "ts": "2000.0",
            "text": f"시작 알림 원문\n취소: `/stop {task_id}`",
            "thread_ts": "1000.0",
        },
    }


def _client() -> MagicMock:
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    client.chat_update = AsyncMock()
    return client


def _ephemeral_text(client: MagicMock) -> str:
    return " ".join(
        c.kwargs.get("text", "") for c in client.chat_postEphemeral.await_args_list
    )


def _app() -> MagicMock:
    app = MagicMock()
    app.client.chat_postMessage = AsyncMock()
    app.client.chat_update = AsyncMock()
    app.client.chat_delete = AsyncMock()
    app.client.reactions_remove = AsyncMock()
    return app


def _posted_texts(app: MagicMock) -> list[str]:
    return [
        c.kwargs.get("text", "") for c in app.client.chat_postMessage.await_args_list
    ]


def _project() -> ProjectConfig:
    return ProjectConfig(name="trader", path="/tmp", description="트레이더")


def _sqlite_project() -> ProjectConfig:
    from slack_bot.config import DBConfig

    return ProjectConfig(
        name="trader",
        path="/tmp",
        description="트레이더",
        db=DBConfig(db_type="sqlite", db_path="db.sqlite3"),
    )


def _export_result(**kwargs):
    from slack_bot.db_query import ExportResult

    return ExportResult(summary=kwargs.pop("summary", ""), **kwargs)


class _EmptyStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def read(self, n: int = -1) -> bytes:
        return b""


def _spawned_proc() -> MagicMock:
    proc = MagicMock()
    proc.stdout = _EmptyStream()
    proc.stderr = _EmptyStream()
    proc.wait = AsyncMock(return_value=-15)
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = -15
    return proc


def _stop_during_spawn(tm: TaskManager, task: TaskInfo, proc: MagicMock):
    """spawn await 중에 stop 한다. 이 시점엔 task.process 가 None 이라 stop_task 가 terminate 못 함."""

    async def _spawn(*args, **kwargs):
        tm.stop_task(task.task_id)
        return proc

    return _spawn


async def _run_queued_then_stop(coro_factory, task: TaskInfo, tm: TaskManager):
    """세마포어를 먼저 점유해 둔 상태에서 태스크를 대기시키고, 대기 중에 stop 한 뒤 풀어 준다."""
    sem = asyncio.Semaphore(1)
    await sem.acquire()
    bg = asyncio.create_task(coro_factory(sem))
    await asyncio.sleep(0)  # 백그라운드가 세마포어 대기에 들어가게 양보
    assert tm.stop_task(task.task_id) is True
    sem.release()
    await bg


# ---------------------------------------------------------------------------
# blocks 구조
# ---------------------------------------------------------------------------


class TestProgressBlocks:
    def test_blocks_carry_task_id_nonce_and_action_id(self):
        blocks = _build_progress_blocks("진행 중 (ID: 007)", "007")
        assert blocks[0]["type"] == "section"
        assert blocks[0]["text"]["text"] == "진행 중 (ID: 007)"
        button = blocks[1]["elements"][0]
        assert button["action_id"] == "task_cancel"
        assert button["style"] == "danger"
        assert button["value"] == f"007:{_BOOT_NONCE}"

    def test_long_text_truncated_within_section_limit(self):
        blocks = _build_progress_blocks("가" * 5000, "001")
        assert len(blocks[0]["text"]["text"]) <= 3000

    @pytest.mark.asyncio
    async def test_falls_back_to_plain_text_on_invalid_blocks(self):
        err = SlackApiError("invalid_blocks", {"ok": False, "error": "invalid_blocks"})
        say = AsyncMock(side_effect=[err, {"ts": "3000.0"}])

        resp = await _post_progress(say, "시작 (ID: 001)", "001", "1000.0")

        assert resp == {"ts": "3000.0"}
        assert say.await_count == 2
        first, second = say.await_args_list
        assert "blocks" in first.kwargs
        assert "blocks" not in second.kwargs
        assert second.kwargs["text"] == "시작 (ID: 001)\n취소: `/stop 001`"
        assert second.kwargs["thread_ts"] == "1000.0"

    @pytest.mark.asyncio
    async def test_other_slack_errors_are_raised_without_retry(self):
        err = SlackApiError(
            "channel_not_found", {"ok": False, "error": "channel_not_found"}
        )
        say = AsyncMock(side_effect=err)

        with pytest.raises(SlackApiError):
            await _post_progress(say, "시작 (ID: 001)", "001", "1000.0")

        assert say.await_count == 1

    @pytest.mark.asyncio
    async def test_progress_text_carries_stop_hint(self):
        say = AsyncMock(return_value={"ts": "3000.0"})

        await _post_progress(say, "시작 (ID: 001)", "001", "1000.0")

        text = say.await_args.kwargs["text"]
        assert text == "시작 (ID: 001)\n취소: `/stop 001`"
        assert say.await_args.kwargs["blocks"][0]["text"]["text"] == text


# ---------------------------------------------------------------------------
# [취소] 버튼 콜백
# ---------------------------------------------------------------------------


class TestTaskCancelCallback:
    @pytest.mark.asyncio
    async def test_owner_click_stops_task_and_removes_button(self):
        task = _make_task()
        tm = _manager_with(task)
        client = _client()

        await _handle_task_cancel(client=client, body=_cancel_body(), task_manager=tm)

        assert task.status == "stopped"
        client.chat_update.assert_awaited_once()
        kwargs = client.chat_update.await_args.kwargs
        assert kwargs["blocks"] == []
        assert kwargs["ts"] == "2000.0"
        assert "취소 요청됨" in kwargs["text"]
        client.chat_postEphemeral.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_user_rejected(self):
        task = _make_task(user="U1")
        tm = _manager_with(task)
        client = _client()

        await _handle_task_cancel(
            client=client, body=_cancel_body(user="U2"), task_manager=tm
        )

        assert task.status == "running"
        assert "본인이 요청한 작업만" in _ephemeral_text(client)
        client.chat_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_owner_check_runs_before_status_check(self):
        """끝난 남의 태스크라도 '이미 종료' 대신 소유자 거부가 나간다."""
        task = _make_task(user="U1")
        task.status = "completed"
        tm = _manager_with(task)
        client = _client()

        await _handle_task_cancel(
            client=client, body=_cancel_body(user="U2"), task_manager=tm
        )

        text = _ephemeral_text(client)
        assert "본인이 요청한 작업만" in text
        assert "이미 종료" not in text

    @pytest.mark.asyncio
    async def test_already_finished_task_clears_buttons(self):
        task = _make_task()
        task.status = "completed"
        tm = _manager_with(task)
        client = _client()

        await _handle_task_cancel(client=client, body=_cancel_body(), task_manager=tm)

        assert "이미 종료된 작업" in _ephemeral_text(client)
        # 본인 작업이 끝났으면 그 자리에서 버튼과 `/stop` 안내를 걷는다.
        client.chat_update.assert_awaited_once()
        kwargs = client.chat_update.await_args.kwargs
        assert kwargs["blocks"] == []
        assert "/stop" not in kwargs["text"]
        assert task.status == "completed"

    @pytest.mark.asyncio
    async def test_unknown_task_rejected_and_button_removed(self):
        tm = TaskManager()
        client = _client()

        await _handle_task_cancel(
            client=client, body=_cancel_body("099"), task_manager=tm
        )

        assert "작업을 찾을 수 없습니다" in _ephemeral_text(client)
        client.chat_update.assert_awaited_once()
        kwargs = client.chat_update.await_args.kwargs
        assert kwargs["blocks"] == []
        assert kwargs["text"] == "시작 알림 원문"

    @pytest.mark.asyncio
    async def test_other_channel_rejected_without_update(self):
        task = _make_task(channel="C123")
        tm = _manager_with(task)
        client = _client()

        await _handle_task_cancel(
            client=client, body=_cancel_body(channel="C999"), task_manager=tm
        )

        assert task.status == "running"
        assert "작업을 찾을 수 없습니다" in _ephemeral_text(client)
        client.chat_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nonce_mismatch_rejected(self):
        task = _make_task()
        tm = _manager_with(task)
        client = _client()

        await _handle_task_cancel(
            client=client, body=_cancel_body(nonce="deadbeef"), task_manager=tm
        )

        assert task.status == "running"
        assert "만료된 버튼" in _ephemeral_text(client)
        kwargs = client.chat_update.await_args.kwargs
        assert kwargs["blocks"] == []
        assert kwargs["text"] == "시작 알림 원문"

    @pytest.mark.asyncio
    async def test_current_nonce_accepted(self):
        task = _make_task()
        tm = _manager_with(task)
        client = _client()

        await _handle_task_cancel(
            client=client, body=_cancel_body(nonce=_BOOT_NONCE), task_manager=tm
        )

        assert task.status == "stopped"

    @pytest.mark.asyncio
    async def test_unparsable_value_only_logs(self):
        task = _make_task()
        tm = _manager_with(task)
        client = _client()

        await _handle_task_cancel(
            client=client, body=_cancel_body(value=""), task_manager=tm
        )
        await _handle_task_cancel(client=client, body={"actions": []}, task_manager=tm)

        assert task.status == "running"
        client.chat_postEphemeral.assert_not_awaited()
        client.chat_update.assert_not_awaited()


# ---------------------------------------------------------------------------
# 큐 대기 중 취소 가드
# ---------------------------------------------------------------------------


class TestQueuedCancelGuard:
    @pytest.mark.asyncio
    async def test_chat_question_not_run_when_stopped_while_queued(self):
        task = _make_task(command="chat")
        tm = _manager_with(task)
        app = _app()

        with patch(
            "slack_bot.handlers.answer_question", new_callable=AsyncMock
        ) as mock_answer:
            await _run_queued_then_stop(
                lambda sem: _run_chat_question_and_report(
                    app,
                    task_manager=tm,
                    task=task,
                    question="질문",
                    tasks=[],
                    thread_history=[],
                    projects={},
                    target_project=None,
                    channel="C123",
                    thread_ts="1000.0",
                    semaphore=sem,
                ),
                task,
                tm,
            )

        mock_answer.assert_not_awaited()
        assert any("취소되었습니다" in t for t in _posted_texts(app))

    @pytest.mark.asyncio
    async def test_db_query_not_run_when_stopped_while_queued(self):
        task = _make_task(command="db")
        tm = _manager_with(task)
        app = _app()

        with patch(
            "slack_bot.handlers.run_db_query", new_callable=AsyncMock
        ) as mock_db:
            await _run_queued_then_stop(
                lambda sem: _run_db_query_and_report(
                    app,
                    task_manager=tm,
                    task=task,
                    question="유저 수",
                    channel="C123",
                    thread_ts="1000.0",
                    user_id="U1",
                    db_project=_project(),
                    semaphore=sem,
                ),
                task,
                tm,
            )

        mock_db.assert_not_awaited()
        assert any("DB 조회가 취소되었습니다" in t for t in _posted_texts(app))

    @pytest.mark.asyncio
    async def test_command_not_run_when_stopped_while_queued(self):
        task = _make_task()
        tm = _manager_with(task)
        app = _app()

        with patch(
            "slack_bot.handlers.run_claude", new_callable=AsyncMock
        ) as mock_run:
            await _run_queued_then_stop(
                lambda sem: _run_and_report(
                    app, tm, _project(), task, "/harness SB-01", sem
                ),
                task,
                tm,
            )

        mock_run.assert_not_awaited()
        texts = _posted_texts(app)
        assert len(texts) == 1
        assert "취소되었습니다" in texts[0]

    @pytest.mark.asyncio
    async def test_db_export_not_run_when_stopped_while_queued(self):
        task = _make_task(command="db_export")
        tm = _manager_with(task)
        app = _app()

        with patch(
            "slack_bot.handlers.run_db_query_export", new_callable=AsyncMock
        ) as mock_export:
            await _run_queued_then_stop(
                lambda sem: _run_db_query_export_and_report(
                    app,
                    task_manager=tm,
                    task=task,
                    question="유저 목록 추출",
                    channel="C123",
                    thread_ts="1000.0",
                    user_id="U1",
                    db_project=_project(),
                    semaphore=sem,
                ),
                task,
                tm,
            )

        mock_export.assert_not_awaited()
        assert any("DB 조회가 취소되었습니다" in t for t in _posted_texts(app))

    @pytest.mark.asyncio
    async def test_prompt_not_run_when_stopped_while_queued(self):
        task = _make_task(command="prompt", args="매수 로직 정리")
        tm = _manager_with(task)
        app = _app()

        with patch(
            "slack_bot.handlers.run_free_prompt", new_callable=AsyncMock
        ) as mock_prompt:
            await _run_queued_then_stop(
                lambda sem: _run_prompt_and_report(
                    app, tm, _project(), task, "매수 로직 정리", sem
                ),
                task,
                tm,
            )

        mock_prompt.assert_not_awaited()
        texts = _posted_texts(app)
        assert len(texts) == 1
        assert "취소되었습니다" in texts[0]

    @pytest.mark.asyncio
    async def test_shell_not_spawned_when_stopped_while_queued(self, tmp_path):
        task = _make_task(command="shell", args="echo hi")
        tm = _manager_with(task)
        app = _app()

        with patch(
            "slack_bot.handlers.asyncio.create_subprocess_shell",
            new_callable=AsyncMock,
        ) as mock_shell:
            await _run_queued_then_stop(
                lambda sem: _run_shell_and_report(
                    app, tm, _project(), task, str(tmp_path / "log.txt"), sem
                ),
                task,
                tm,
            )

        mock_shell.assert_not_awaited()
        texts = _posted_texts(app)
        assert len(texts) == 1
        assert "취소되었습니다" in texts[0]


# ---------------------------------------------------------------------------
# 실행 중 취소 시 실패 대신 취소 안내
# ---------------------------------------------------------------------------


class TestCancelledWhileRunning:
    @pytest.mark.asyncio
    async def test_command_cancelled_posts_cancel_only(self):
        task = _make_task()
        tm = _manager_with(task)
        app = _app()

        async def _fake_run(*args, **kwargs):
            tm.stop_task(task.task_id)
            return MagicMock(success=False, output="terminated")

        with patch("slack_bot.handlers.run_claude", side_effect=_fake_run):
            await _run_and_report(
                app, tm, _project(), task, "/harness SB-01", asyncio.Semaphore(1)
            )

        texts = _posted_texts(app)
        assert len(texts) == 1
        assert "취소되었습니다" in texts[0]
        assert not app.client.chat_postMessage.await_args.kwargs.get("blocks")
        assert task.status == "stopped"

    @pytest.mark.asyncio
    async def test_shell_cancelled_posts_cancel_only(self, tmp_path):
        task = _make_task(command="shell", args="sleep 100")
        tm = _manager_with(task)
        app = _app()

        proc = MagicMock()
        proc.returncode = -15

        async def _wait():
            tm.stop_task(task.task_id)

        proc.wait = AsyncMock(side_effect=_wait)

        with patch(
            "slack_bot.handlers.asyncio.create_subprocess_shell",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            await _run_shell_and_report(
                app, tm, _project(), task, str(tmp_path / "log.txt"),
                asyncio.Semaphore(1),
            )

        texts = _posted_texts(app)
        assert len(texts) == 1
        assert "취소되었습니다" in texts[0]
        assert ":x:" not in texts[0]


# ---------------------------------------------------------------------------
# spawn 직전 / 직후 가드
# ---------------------------------------------------------------------------


class TestSpawnGuard:
    @pytest.mark.asyncio
    async def test_answer_question_skips_spawn_when_stopped(self):
        from slack_bot.chat import answer_question

        task = _make_task(command="chat")
        task.status = "stopped"

        with patch(
            "slack_bot.chat.asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec:
            await answer_question("질문", [], projects={}, task=task)

        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_claude_skips_spawn_when_stopped(self):
        from slack_bot.runner import run_claude

        task = _make_task()
        task.status = "stopped"

        with patch(
            "slack_bot.runner.asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec:
            result = await run_claude(_project(), "harness", "SB-01", task)

        mock_exec.assert_not_awaited()
        assert result.success is False

    @pytest.mark.asyncio
    async def test_run_claude_terminates_when_stopped_during_spawn(self):
        from slack_bot.runner import run_claude

        task = _make_task()
        tm = _manager_with(task)
        proc = _spawned_proc()

        with patch(
            "slack_bot.runner.asyncio.create_subprocess_exec",
            side_effect=_stop_during_spawn(tm, task, proc),
        ):
            await run_claude(_project(), "harness", "SB-01", task)

        proc.terminate.assert_called_once()


    @pytest.mark.asyncio
    async def test_run_db_query_skips_spawn_when_stopped(self):
        from slack_bot.db_query import run_db_query

        task = _make_task(command="db")
        task.status = "stopped"

        with patch(
            "slack_bot.db_query.asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec:
            await run_db_query("유저 수", _sqlite_project(), task=task)

        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_db_query_export_skips_spawn_when_stopped(self):
        from slack_bot.db_query import run_db_query_export

        task = _make_task(command="db_export")
        task.status = "stopped"

        with patch(
            "slack_bot.db_query.asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec:
            result = await run_db_query_export("유저 목록", _sqlite_project(), task=task)

        mock_exec.assert_not_awaited()
        assert result.excel_path is None

    @pytest.mark.asyncio
    async def test_answer_question_terminates_when_stopped_during_spawn(self):
        from slack_bot.chat import answer_question

        task = _make_task(command="chat")
        tm = _manager_with(task)
        proc = _spawned_proc()

        with patch(
            "slack_bot.chat.asyncio.create_subprocess_exec",
            side_effect=_stop_during_spawn(tm, task, proc),
        ):
            await answer_question("질문", [], projects={}, task=task)

        proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_db_query_terminates_when_stopped_during_spawn(self):
        from slack_bot.db_query import run_db_query

        task = _make_task(command="db")
        tm = _manager_with(task)
        proc = _spawned_proc()

        with patch(
            "slack_bot.db_query.asyncio.create_subprocess_exec",
            side_effect=_stop_during_spawn(tm, task, proc),
        ):
            await run_db_query("유저 수", _sqlite_project(), task=task)

        proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_db_query_export_terminates_when_stopped_during_spawn(self):
        from slack_bot.db_query import run_db_query_export

        task = _make_task(command="db_export")
        tm = _manager_with(task)
        proc = _spawned_proc()

        with patch(
            "slack_bot.db_query.asyncio.create_subprocess_exec",
            side_effect=_stop_during_spawn(tm, task, proc),
        ):
            await run_db_query_export("유저 목록", _sqlite_project(), task=task)

        proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_shell_terminates_when_stopped_during_spawn(self, tmp_path):
        task = _make_task(command="shell", args="sleep 100")
        tm = _manager_with(task)
        app = _app()
        proc = _spawned_proc()

        with patch(
            "slack_bot.handlers.asyncio.create_subprocess_shell",
            side_effect=_stop_during_spawn(tm, task, proc),
        ):
            await _run_shell_and_report(
                app, tm, _project(), task, str(tmp_path / "log.txt"),
                asyncio.Semaphore(1),
            )

        proc.terminate.assert_called_once()
        assert "취소되었습니다" in _posted_texts(app)[0]


# ---------------------------------------------------------------------------
# 시작 알림 정리
# ---------------------------------------------------------------------------


class TestProgressCleanup:
    @pytest.mark.asyncio
    async def test_db_progress_deleted_after_result(self):
        task = _make_task(command="db")
        task.progress_ts = "2000.0"
        tm = _manager_with(task)
        app = _app()

        with patch(
            "slack_bot.handlers.run_db_query",
            new_callable=AsyncMock,
            return_value="결과",
        ):
            await _run_db_query_and_report(
                app,
                task_manager=tm,
                task=task,
                question="유저 수",
                channel="C123",
                thread_ts="1000.0",
                user_id="U1",
                db_project=_project(),
                semaphore=asyncio.Semaphore(1),
            )

        app.client.chat_postMessage.assert_awaited_once()
        app.client.chat_delete.assert_awaited_once_with(channel="C123", ts="2000.0")

    @pytest.mark.asyncio
    async def test_command_completion_clears_buttons_with_original_text(self):
        task = _make_task()
        task.progress_ts = "2000.0"
        tm = _manager_with(task)
        app = _app()

        with patch(
            "slack_bot.handlers.run_claude",
            new_callable=AsyncMock,
            return_value=MagicMock(success=True, output="done"),
        ):
            await _run_and_report(
                app, tm, _project(), task, "/harness SB-01", asyncio.Semaphore(1),
                progress_text="시작 알림 원문",
            )

        app.client.chat_update.assert_awaited_once_with(
            channel="C123", ts="2000.0", text="시작 알림 원문", blocks=[]
        )

    @pytest.mark.asyncio
    async def test_command_stopped_clears_buttons_with_cancelled_text(self):
        """/stop 으로 끊으면 버튼 콜백을 거치지 않으므로 완료 시 취소됨 문구로 버튼을 걷는다."""
        task = _make_task()
        task.progress_ts = "2000.0"
        tm = _manager_with(task)
        tm.stop_task(task.task_id)
        app = _app()

        with patch("slack_bot.handlers.run_claude", new_callable=AsyncMock):
            await _run_and_report(
                app, tm, _project(), task, "/harness SB-01", asyncio.Semaphore(1),
                progress_text="시작 알림 원문",
            )

        app.client.chat_update.assert_awaited_once_with(
            channel="C123", ts="2000.0", text=":octagonal_sign: 취소됨 (ID: 001)",
            blocks=[],
        )

    @pytest.mark.asyncio
    async def test_shell_stopped_clears_buttons_with_cancelled_text(self, tmp_path):
        task = _make_task(command="shell", args="echo hi")
        task.progress_ts = "2000.0"
        tm = _manager_with(task)
        tm.stop_task(task.task_id)
        app = _app()

        await _run_shell_and_report(
            app, tm, _project(), task, str(tmp_path / "log.txt"),
            asyncio.Semaphore(1), progress_text="시작 알림 원문",
        )

        kwargs = app.client.chat_update.await_args.kwargs
        assert kwargs["blocks"] == []
        assert "취소됨" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_db_export_progress_deleted_after_result(self):
        task = _make_task(command="db_export")
        task.progress_ts = "2000.0"
        tm = _manager_with(task)
        app = _app()

        with patch(
            "slack_bot.handlers.run_db_query_export",
            new_callable=AsyncMock,
            return_value=_export_result(error="실패"),
        ):
            await _run_db_query_export_and_report(
                app,
                task_manager=tm,
                task=task,
                question="유저 목록 추출",
                channel="C123",
                thread_ts="1000.0",
                user_id="U1",
                db_project=_project(),
                semaphore=asyncio.Semaphore(1),
            )

        app.client.chat_postMessage.assert_awaited_once()
        app.client.chat_delete.assert_awaited_once_with(channel="C123", ts="2000.0")


# ---------------------------------------------------------------------------
# 등록과 질문 태스크 요청자 기록
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_task_cancel_action_registered(self):
        from slack_bot.config import AppConfig, SecurityConfig

        actions: dict = {}

        def capture_action(action_id):
            def decorator(func):
                actions[action_id] = func
                return func
            return decorator

        app = MagicMock()
        app.action = capture_action
        app.event = lambda event_type: lambda f: f
        app.command = lambda name: lambda f: f

        with patch("slack_bot.handlers.load_projects") as mock_load:
            mock_load.return_value = AppConfig(
                projects={}, security=SecurityConfig(allowed_users={"admin": ["*"]})
            )
            handlers.register_handlers(app, MagicMock())

        assert "task_cancel" in actions

    @pytest.mark.asyncio
    async def test_question_task_records_requester(self):
        from tests.test_handler_stability import (
            _drain_background_tasks,
            _register_event_handlers,
        )

        events, task_manager = _register_event_handlers()
        say = AsyncMock(return_value={"ts": "2000.0"})
        client = MagicMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        client.conversations_replies = AsyncMock(return_value={"messages": []})

        event = {
            "text": "<@U0BOT> 온보딩 절차 알려줘",
            "ts": "1000.0",
            "user": "U7",
            "channel": "C123",
            "channel_type": "channel",
        }
        with patch(
            "slack_bot.handlers.answer_question",
            new_callable=AsyncMock,
            return_value="답변",
        ):
            await events["app_mention"](event=event, say=say, client=client)
            await _drain_background_tasks()

        create_args = task_manager.create_task.await_args.args
        assert create_args[1] == "chat"
        assert create_args[3] == "U7"
        # 시작 알림에 [취소] 버튼이 붙는다
        progress_call = say.await_args_list[0]
        button = progress_call.kwargs["blocks"][1]["elements"][0]
        assert button["action_id"] == "task_cancel"


# ---------------------------------------------------------------------------
# 실행이 알림 게시보다 먼저 끝난 경우
# ---------------------------------------------------------------------------


def _register_with_task(projects, task: TaskInfo):
    """register_handlers 를 mock app 으로 부르고, create_task_if_absent 가 주어진 task 를 돌려주게 한다."""
    from slack_bot.config import AppConfig, SecurityConfig

    events: dict = {}

    def capture_event(event_type):
        def decorator(func):
            events[event_type] = func
            return func
        return decorator

    app = _app()
    app.event = capture_event
    app.action = lambda action_id: lambda f: f
    app.command = lambda name: lambda f: f

    task_manager = MagicMock()
    task_manager.create_task_if_absent = AsyncMock(return_value=(task, True))

    with patch("slack_bot.handlers.load_projects") as mock_load:
        mock_load.return_value = AppConfig(
            projects=projects, security=SecurityConfig(allowed_users={"admin": ["*"]})
        )
        handlers.register_handlers(app, task_manager)
    return app, events


async def _slow_say(*args, **kwargs):
    # 게시를 기다리는 동안 백그라운드 실행이 끝나도록 여러 번 양보한다.
    for _ in range(20):
        await asyncio.sleep(0)
    return {"ts": "2000.0"}


class TestRunFinishedBeforeProgressPosted:
    async def _run(self, text: str, patch_target: str, task: TaskInfo):
        app, events = _register_with_task({"trader": _project()}, task)
        client = MagicMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        event = {
            "text": text,
            "ts": "1000.0",
            "user": "U1",
            "channel": "C123",
            "channel_type": "channel",
        }

        async def _fake_run(*args, **kwargs):
            task.status = "completed"
            return MagicMock(success=True, output="done")

        with patch(patch_target, side_effect=_fake_run):
            await events["app_mention"](
                event=event, say=AsyncMock(side_effect=_slow_say), client=client
            )
            pending = [
                t for t in asyncio.all_tasks() if t is not asyncio.current_task()
            ]
            await asyncio.gather(*pending, return_exceptions=True)
        return app

    @pytest.mark.asyncio
    async def test_command_buttons_cleared_by_handler(self):
        task = _make_task()
        app = await self._run(
            "<@U0BOT> trader 하네스 TRD-128 돌려줘", "slack_bot.handlers.run_claude", task
        )

        assert task.progress_ts == "2000.0"
        kwargs = app.client.chat_update.await_args.kwargs
        assert kwargs["ts"] == "2000.0"
        assert kwargs["blocks"] == []
        assert "실행을 시작합니다" in kwargs["text"]
        assert "/stop" not in kwargs["text"]

    @pytest.mark.asyncio
    async def test_prompt_buttons_cleared_by_handler(self):
        task = _make_task(command="prompt", args="매수 로직 개선 방향 정리해줘")
        app = await self._run(
            "<@U0BOT> trader 매수 로직 개선 방향 정리해줘",
            "slack_bot.handlers.run_free_prompt",
            task,
        )

        kwargs = app.client.chat_update.await_args.kwargs
        assert kwargs["ts"] == "2000.0"
        assert kwargs["blocks"] == []
        assert "프롬프트 실행을 시작합니다" in kwargs["text"]


# ---------------------------------------------------------------------------
# 대기 중 취소가 슬롯을 기다리지 않음
# ---------------------------------------------------------------------------


class TestTaskSlot:
    @pytest.mark.asyncio
    async def test_cancel_reply_without_releasing_slot(self):
        """슬롯을 점유한 채 stop 하면 슬롯이 풀리지 않아도 취소 답글이 바로 나간다."""
        task = _make_task()
        tm = _manager_with(task)
        app = _app()
        sem = asyncio.Semaphore(1)
        await sem.acquire()

        with patch(
            "slack_bot.handlers.run_claude", new_callable=AsyncMock
        ) as mock_run:
            bg = asyncio.create_task(
                _run_and_report(app, tm, _project(), task, "/harness SB-01", sem)
            )
            await asyncio.sleep(0)
            tm.stop_task(task.task_id)
            await asyncio.wait_for(bg, timeout=1)

        mock_run.assert_not_awaited()
        assert "취소되었습니다" in _posted_texts(app)[0]
        # 슬롯은 여전히 바깥이 쥐고 있다
        assert sem.locked()
        sem.release()
        assert sem._value == 1

    @pytest.mark.asyncio
    async def test_no_leak_when_release_and_stop_race(self):
        """슬롯이 풀리는 것과 stop 이 같은 순간에 일어나도 세마포어 카운트가 새지 않는다."""
        task = _make_task()
        tm = _manager_with(task)
        sem = asyncio.Semaphore(1)
        await sem.acquire()
        entered: list[tuple[str, bool]] = []

        async def _body():
            async with handlers._task_slot(sem, task):
                # 취소된 태스크는 슬롯을 쥔 채 본문(취소 답글 게시)을 돌지 않는다
                entered.append((task.status, sem.locked()))

        bg = asyncio.create_task(_body())
        await asyncio.sleep(0)
        sem.release()  # 대기 중인 acquire 를 깨운다
        tm.stop_task(task.task_id)  # 같은 틱에 stop
        await asyncio.wait_for(bg, timeout=1)

        assert entered == [("stopped", False)]
        assert sem._value == 1
        assert not sem.locked()

    @pytest.mark.asyncio
    async def test_slot_released_after_normal_run(self):
        task = _make_task()
        sem = asyncio.Semaphore(1)

        async with handlers._task_slot(sem, task):
            assert sem.locked()

        assert sem._value == 1

    def test_stop_task_sets_stop_event(self):
        task = _make_task()
        tm = _manager_with(task)

        tm.stop_task(task.task_id)

        assert task.stop_event.is_set()


# ---------------------------------------------------------------------------
# shell 취소는 프로세스 그룹째 끝낸다
# ---------------------------------------------------------------------------


class TestProcessGroupTermination:
    def test_group_leader_killed_by_group(self):
        from slack_bot.task_manager import terminate_process

        proc = MagicMock()
        proc.pid = 4321
        with patch("slack_bot.task_manager.os.getpgid", return_value=4321), patch(
            "slack_bot.task_manager.os.killpg"
        ) as mock_killpg:
            terminate_process(proc)

        import signal

        mock_killpg.assert_called_once_with(4321, signal.SIGTERM)
        proc.terminate.assert_not_called()

    def test_non_leader_uses_terminate(self):
        """그룹 리더가 아니면 killpg 하지 않는다 (봇 자신의 그룹을 죽이지 않도록)."""
        from slack_bot.task_manager import terminate_process

        proc = MagicMock()
        proc.pid = 4321
        with patch("slack_bot.task_manager.os.getpgid", return_value=1), patch(
            "slack_bot.task_manager.os.killpg"
        ) as mock_killpg:
            terminate_process(proc)

        mock_killpg.assert_not_called()
        proc.terminate.assert_called_once()

    def test_already_exited_process_ignored(self):
        from slack_bot.task_manager import terminate_process

        proc = MagicMock()
        proc.pid = 4321
        with patch(
            "slack_bot.task_manager.os.getpgid", side_effect=ProcessLookupError
        ):
            terminate_process(proc)  # 예외가 새지 않는다

    def test_stop_task_kills_shell_group(self):
        task = _make_task(command="shell")
        tm = _manager_with(task)
        proc = MagicMock()
        proc.pid = 4321
        proc.returncode = None
        task.process = proc

        with patch("slack_bot.task_manager.os.getpgid", return_value=4321), patch(
            "slack_bot.task_manager.os.killpg"
        ) as mock_killpg:
            tm.stop_task(task.task_id)

        mock_killpg.assert_called_once()

    @pytest.mark.asyncio
    async def test_real_shell_group_terminated(self):
        """실제 sh 아래 자식까지 끝나는지 확인한다."""
        from slack_bot.task_manager import terminate_process

        proc = await asyncio.create_subprocess_shell(
            "sleep 30; echo done",
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        terminate_process(proc)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        assert b"done" not in out
        assert proc.returncode != 0

    @pytest.mark.asyncio
    async def test_shell_spawned_in_new_session(self, tmp_path):
        task = _make_task(command="shell", args="echo hi")
        tm = _manager_with(task)
        app = _app()
        proc = _spawned_proc()
        proc.returncode = 0

        with patch(
            "slack_bot.handlers.asyncio.create_subprocess_shell",
            new_callable=AsyncMock,
            return_value=proc,
        ) as mock_shell:
            await _run_shell_and_report(
                app, tm, _project(), task, str(tmp_path / "log.txt"),
                asyncio.Semaphore(1),
            )

        assert mock_shell.await_args.kwargs["start_new_session"] is True

    @pytest.mark.asyncio
    async def test_remote_shell_spawned_in_new_session_and_cancel_notes_remote(
        self, tmp_path
    ):
        from slack_bot.config import RemoteConfig

        project = ProjectConfig(
            name="trader", path="/tmp", remote=RemoteConfig(host="prod")
        )
        task = _make_task(command="shell", args="sleep 100")
        tm = _manager_with(task)
        app = _app()
        proc = _spawned_proc()

        with patch(
            "slack_bot.handlers.asyncio.create_subprocess_exec",
            side_effect=_stop_during_spawn(tm, task, proc),
        ) as mock_exec:
            await _run_shell_and_report(
                app, tm, project, task, str(tmp_path / "log.txt"),
                asyncio.Semaphore(1),
            )

        assert mock_exec.call_args.kwargs["start_new_session"] is True
        assert "원격 서버의 명령은 계속" in _posted_texts(app)[0]


# ---------------------------------------------------------------------------
# 시작 알림 게시 실패 시 태스크를 닫는다
# ---------------------------------------------------------------------------


class TestProgressPostFailureClosesTask:
    async def _run(self, projects, text: str):
        from tests.test_handler_stability import _register_event_handlers

        events, task_manager = _register_event_handlers(projects)
        err = SlackApiError("ratelimited", {"ok": False, "error": "ratelimited"})
        say = AsyncMock(side_effect=err)
        client = MagicMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        client.conversations_replies = AsyncMock(return_value={"messages": []})
        event = {
            "text": text,
            "ts": "1000.0",
            "user": "U1",
            "channel": "C123",
            "channel_type": "channel",
        }
        with patch(
            "slack_bot.handlers.answer_question", new_callable=AsyncMock
        ), patch("slack_bot.handlers.run_db_query", new_callable=AsyncMock), patch(
            "slack_bot.handlers.run_db_query_export", new_callable=AsyncMock
        ), patch(
            "slack_bot.handlers.asyncio.create_subprocess_shell", new_callable=AsyncMock
        ) as mock_shell:
            with pytest.raises(SlackApiError):
                await events["app_mention"](event=event, say=say, client=client)
        return task_manager, mock_shell

    @pytest.mark.asyncio
    async def test_shell_task_failed_when_progress_post_fails(self):
        from tests.test_handler_stability import _shell_event, _trader_projects

        tm, mock_shell = await self._run(_trader_projects(), _shell_event()["text"])

        tm.complete_task.assert_called_once_with("001", False)
        mock_shell.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_db_task_failed_when_progress_post_fails(self):
        from tests.test_handler_stability import _db_event, _trader_db_projects

        tm, _ = await self._run(_trader_db_projects(), _db_event()["text"])

        tm.complete_task.assert_called_once_with("001", False)

    @pytest.mark.asyncio
    async def test_db_export_task_failed_when_progress_post_fails(self):
        from tests.test_handler_stability import _db_event, _trader_db_projects

        tm, _ = await self._run(
            _trader_db_projects(), _db_event(export=True)["text"]
        )

        tm.complete_task.assert_called_once_with("001", False)

    @pytest.mark.asyncio
    async def test_question_task_failed_when_progress_post_fails(self):
        tm, _ = await self._run(None, "<@U0BOT> 온보딩 절차 알려줘")

        tm.complete_task.assert_called_once_with("001", False)
