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
        "message": {"ts": "2000.0", "text": "시작 알림 원문", "thread_ts": "1000.0"},
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

        resp = await _post_progress(say, "시작 (취소: `/stop 001`)", "001", "1000.0")

        assert resp == {"ts": "3000.0"}
        assert say.await_count == 2
        first, second = say.await_args_list
        assert "blocks" in first.kwargs
        assert "blocks" not in second.kwargs
        assert second.kwargs["text"] == "시작 (취소: `/stop 001`)"
        assert second.kwargs["thread_ts"] == "1000.0"


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
    async def test_already_finished_task_ephemeral_only(self):
        task = _make_task()
        task.status = "completed"
        tm = _manager_with(task)
        client = _client()

        await _handle_task_cancel(client=client, body=_cancel_body(), task_manager=tm)

        assert "이미 종료된 작업" in _ephemeral_text(client)
        client.chat_update.assert_not_awaited()
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

        class _EmptyStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def read(self, n: int = -1) -> bytes:
                return b""

        proc = MagicMock()
        proc.stdout = _EmptyStream()
        proc.stderr = _EmptyStream()
        proc.wait = AsyncMock(return_value=-15)
        proc.returncode = -15

        async def _spawn(*args, **kwargs):
            # spawn await 중에 stop: 이 시점엔 task.process 가 None 이라 terminate 못 함
            tm.stop_task(task.task_id)
            return proc

        with patch(
            "slack_bot.runner.asyncio.create_subprocess_exec", side_effect=_spawn
        ):
            await run_claude(_project(), "harness", "SB-01", task)

        proc.terminate.assert_called_once()


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
    async def test_command_cancelled_keeps_cancel_requested_text(self):
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

        app.client.chat_update.assert_not_awaited()


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
