"""handler 안정성 테스트."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slack_bot.handlers import _log_task_exception


class TestLogTaskException:
    """_log_task_exception done callback 테스트."""

    def test_cancelled_task_does_not_log(self, caplog):
        task = MagicMock(spec=asyncio.Task)
        task.cancelled.return_value = True

        with caplog.at_level(logging.ERROR):
            _log_task_exception(task)

        assert len(caplog.records) == 0

    def test_successful_task_does_not_log(self, caplog):
        task = MagicMock(spec=asyncio.Task)
        task.cancelled.return_value = False
        task.exception.return_value = None

        with caplog.at_level(logging.ERROR):
            _log_task_exception(task)

        assert len(caplog.records) == 0

    def test_failed_task_logs_exception(self, caplog):
        task = MagicMock(spec=asyncio.Task)
        task.cancelled.return_value = False
        exc = RuntimeError("test error")
        task.exception.return_value = exc

        with caplog.at_level(logging.ERROR):
            _log_task_exception(task)

        assert len(caplog.records) == 1
        assert "백그라운드 태스크 예외" in caplog.records[0].message
        assert "test error" in caplog.records[0].message


class TestSemaphoreBehavior:
    """Semaphore 동시실행 제한 동작 테스트."""

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        """Semaphore가 동시실행 수를 제한하는지 확인."""
        sem = asyncio.Semaphore(2)
        active = 0
        max_active = 0

        async def worker():
            nonlocal active, max_active
            async with sem:
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.01)
                active -= 1

        tasks = [asyncio.create_task(worker()) for _ in range(5)]
        await asyncio.gather(*tasks)

        assert max_active <= 2

    @pytest.mark.asyncio
    async def test_semaphore_waits_then_proceeds(self):
        """Semaphore가 가득 찬 상태에서 대기 후 정상 처리되는지 확인."""
        sem = asyncio.Semaphore(1)
        results = []

        async def worker(n: int):
            async with sem:
                results.append(n)
                await asyncio.sleep(0.01)

        tasks = [asyncio.create_task(worker(i)) for i in range(3)]
        await asyncio.gather(*tasks)

        assert sorted(results) == [0, 1, 2]


class TestRunAndReportSemaphore:
    """_run_and_report가 semaphore를 사용하는지 확인."""

    @pytest.mark.asyncio
    async def test_run_and_report_acquires_semaphore(self):
        """_run_and_report가 semaphore를 acquire하는지 확인."""
        from slack_bot.handlers import _run_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()

        task_manager = MagicMock()
        project = MagicMock()
        task = MagicMock()
        task.task_id = "001"
        task.project_name = "test"
        task.command = "harness"
        task.args = "TEST-1"
        task.user = "user1"
        task.channel = "C123"
        task.elapsed_display = "1분"

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = "done"

        with patch(
            "slack_bot.handlers.run_claude",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            assert sem._value == 1
            await _run_and_report(
                app,
                task_manager,
                project,
                task,
                "/harness TEST-1",
                sem,
            )
            assert sem._value == 1

        task_manager.complete_task.assert_called_once_with("001", True)


class TestRunDbQueryAndReportSemaphore:
    """_run_db_query_and_report가 semaphore를 사용하는지 확인."""

    @pytest.mark.asyncio
    async def test_run_db_query_and_report_acquires_semaphore(self):
        from slack_bot.handlers import _run_db_query_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()

        db_project = MagicMock()
        task_manager = MagicMock()
        task = MagicMock()
        task.task_id = "001"
        task.status = "running"

        with patch(
            "slack_bot.handlers.run_db_query",
            new_callable=AsyncMock,
            return_value="결과",
        ):
            assert sem._value == 1
            await _run_db_query_and_report(
                app,
                task_manager=task_manager,
                task=task,
                question="테스트",
                channel="C123",
                thread_ts="1234.5678",
                user_id="U123",
                db_project=db_project,
                wiki_path=None,
                semaphore=sem,
            )
            assert sem._value == 1

        app.client.chat_postMessage.assert_called_once()
        task_manager.complete_task.assert_called_once_with("001", True)


class TestRunChatQuestionAndReport:
    """_run_chat_question_and_report 백그라운드 흐름 테스트."""

    @pytest.mark.asyncio
    async def test_posts_answer_to_thread_on_success(self):
        from slack_bot.handlers import _run_chat_question_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()

        task_manager = MagicMock()
        task = MagicMock()
        task.task_id = "042"
        task.status = "running"

        with patch(
            "slack_bot.handlers.answer_question",
            new_callable=AsyncMock,
            return_value="답변 본문",
        ):
            await _run_chat_question_and_report(
                app,
                task_manager=task_manager,
                task=task,
                question="질문",
                tasks=[],
                thread_history=[],
                projects={},
                target_project=None,
                channel="C123",
                thread_ts="1234.5678",
                semaphore=sem,
            )

        task_manager.complete_task.assert_called_once_with("042", True)
        app.client.chat_postMessage.assert_called_once()
        kwargs = app.client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "C123"
        assert kwargs["thread_ts"] == "1234.5678"
        assert "답변 본문" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_truncates_long_answer_for_slack_limit(self):
        """Slack text 필드 4000자 제한을 넘는 답변은 3900자에서 잘라낸다."""
        from slack_bot.handlers import _run_chat_question_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()

        task_manager = MagicMock()
        task = MagicMock()
        task.task_id = "044"
        task.status = "running"

        long_answer = "가" * 5000

        with patch(
            "slack_bot.handlers.answer_question",
            new_callable=AsyncMock,
            return_value=long_answer,
        ):
            await _run_chat_question_and_report(
                app,
                task_manager=task_manager,
                task=task,
                question="질문",
                tasks=[],
                thread_history=[],
                projects={},
                target_project=None,
                channel="C123",
                thread_ts="1234.5678",
                semaphore=sem,
            )

        kwargs = app.client.chat_postMessage.call_args.kwargs
        assert len(kwargs["text"]) < 4000
        assert "(truncated)" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_reports_cancellation_when_stopped(self):
        from slack_bot.handlers import _run_chat_question_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()

        task_manager = MagicMock()
        task = MagicMock()
        task.task_id = "043"
        task.status = "stopped"

        with patch(
            "slack_bot.handlers.answer_question",
            new_callable=AsyncMock,
            return_value=":octagonal_sign: 질문 처리가 취소되었습니다.",
        ):
            await _run_chat_question_and_report(
                app,
                task_manager=task_manager,
                task=task,
                question="질문",
                tasks=[],
                thread_history=[],
                projects={},
                target_project=None,
                channel="C123",
                thread_ts="1234.5678",
                semaphore=sem,
            )

        task_manager.complete_task.assert_not_called()
        kwargs = app.client.chat_postMessage.call_args.kwargs
        assert ":octagonal_sign:" in kwargs["text"]


def _register_slash_handlers():
    """register_handlers를 mock app으로 호출하고 @app.command 콜백을 캡쳐."""
    from slack_bot.handlers import register_handlers

    commands: dict = {}

    def capture_command(name):
        def decorator(func):
            commands[name] = func
            return func
        return decorator

    app = MagicMock()
    app.command = capture_command
    app.action = lambda action_id: lambda f: f
    app.event = lambda event_type: lambda f: f

    task_manager = MagicMock()
    task_manager.get_running_tasks.return_value = []

    with patch("slack_bot.handlers.load_projects") as mock_load:
        mock_config = MagicMock()
        mock_config.projects = {}
        mock_config.security.allowed_users = {"admin": ["*"]}
        mock_load.return_value = mock_config
        register_handlers(app, task_manager)

    return commands, task_manager


class TestSlashCommandRestart:
    """/restart 슬래시 커맨드 핸들러 — Slack에 등록된 명령이 DM/채널에서 동작하도록."""

    @pytest.mark.asyncio
    async def test_posts_restart_confirmation(self):
        """ack 후 재시작 확인 버튼 메시지를 channel_id로 포스트한다."""
        commands, _ = _register_slash_handlers()
        handler = commands.get("/restart")
        assert handler is not None, "/restart 핸들러가 등록되어야 한다"

        ack = AsyncMock()
        client = MagicMock()
        client.chat_postMessage = AsyncMock()
        body = {"user_id": "U_ADMIN", "channel_id": "D123", "text": ""}

        await handler(ack=ack, body=body, client=client)

        ack.assert_awaited_once()
        client.chat_postMessage.assert_awaited()
        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "D123"
        # 확인 버튼 블록이 포함되었는지 (action_id로 확인)
        blocks_text = str(call_kwargs.get("blocks", []))
        assert "confirm_restart" in blocks_text


class TestSlashCommandStop:
    """/stop 슬래시 커맨드 핸들러."""

    @pytest.mark.asyncio
    async def test_with_task_id_calls_stop(self):
        """/stop 042 → stop_task('042') 호출."""
        commands, task_manager = _register_slash_handlers()
        task_manager.stop_task.return_value = True
        handler = commands.get("/stop")
        assert handler is not None

        ack = AsyncMock()
        client = MagicMock()
        client.chat_postMessage = AsyncMock()
        body = {"user_id": "U1", "channel_id": "C123", "text": "042"}

        await handler(ack=ack, body=body, client=client)

        ack.assert_awaited_once()
        task_manager.stop_task.assert_called_once_with("042")
        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert "042" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_without_args_lists_running_tasks(self):
        """/stop (인자 없음) → 실행 중 태스크 목록."""
        commands, task_manager = _register_slash_handlers()
        task_manager.get_running_tasks.return_value = []
        handler = commands.get("/stop")
        assert handler is not None

        ack = AsyncMock()
        client = MagicMock()
        client.chat_postMessage = AsyncMock()
        body = {"user_id": "U1", "channel_id": "C123", "text": ""}

        await handler(ack=ack, body=body, client=client)

        ack.assert_awaited_once()
        task_manager.stop_task.assert_not_called()
        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert "태스크" in call_kwargs["text"]
