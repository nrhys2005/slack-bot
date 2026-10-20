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


class TestEyesReactionLifecycle:
    """'읽는 중' :eyes: 리액션이 백그라운드 태스크 완료 시점에 제거되는지 확인.

    회귀 방지: 예전에는 _handle_message의 finally에서 리액션을 즉시 제거해,
    백그라운드로 위임되는 인텐트(command/shell/db/question)에서는 리액션이
    붙자마자 사라져 사용자에게는 아예 안 붙은 것처럼 보였다.
    """

    @pytest.mark.asyncio
    async def test_run_and_report_removes_eyes_when_event_ts_given(self):
        from slack_bot.handlers import _run_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()
        app.client.reactions_remove = AsyncMock()

        task_manager = MagicMock()
        task = MagicMock()
        task.task_id = "001"
        task.project_name = "test"
        task.channel = "C123"
        task.thread_ts = None
        task.elapsed_display = "1분"
        task.user = "user1"

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = "done"

        with patch(
            "slack_bot.handlers.run_claude",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            await _run_and_report(
                app, task_manager, MagicMock(), task, "/harness TEST-1", sem,
                event_ts="1000.0",
            )

        app.client.reactions_remove.assert_awaited_once_with(
            channel="C123", timestamp="1000.0", name="eyes"
        )

    @pytest.mark.asyncio
    async def test_run_and_report_no_reaction_call_without_event_ts(self):
        """레거시 경로(확인 버튼)처럼 event_ts가 없으면 리액션 제거를 시도하지 않는다."""
        from slack_bot.handlers import _run_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()
        app.client.reactions_remove = AsyncMock()

        task_manager = MagicMock()
        task = MagicMock()
        task.task_id = "001"
        task.project_name = "test"
        task.channel = "C123"
        task.thread_ts = None
        task.elapsed_display = "1분"
        task.user = "user1"

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = "done"

        with patch(
            "slack_bot.handlers.run_claude",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            await _run_and_report(
                app, task_manager, MagicMock(), task, "/harness TEST-1", sem,
            )

        app.client.reactions_remove.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_chat_report_removes_eyes_on_success(self):
        from slack_bot.handlers import _run_chat_question_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()
        app.client.reactions_remove = AsyncMock()

        task_manager = MagicMock()
        task = MagicMock()
        task.task_id = "042"
        task.status = "running"

        with patch(
            "slack_bot.handlers.answer_question",
            new_callable=AsyncMock,
            return_value="답변",
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
                event_ts="1000.0",
            )

        app.client.reactions_remove.assert_awaited_once_with(
            channel="C123", timestamp="1000.0", name="eyes"
        )

    @pytest.mark.asyncio
    async def test_chat_report_removes_eyes_even_on_error(self):
        """답변 처리 중 예외가 나도 리액션은 반드시 제거된다 (finally)."""
        from slack_bot.handlers import _run_chat_question_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()
        app.client.reactions_remove = AsyncMock()

        task_manager = MagicMock()
        task = MagicMock()
        task.task_id = "042"
        task.status = "running"

        with patch(
            "slack_bot.handlers.answer_question",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
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
                event_ts="1000.0",
            )

        app.client.reactions_remove.assert_awaited_once_with(
            channel="C123", timestamp="1000.0", name="eyes"
        )


class TestRunPromptAndReport:
    """_run_prompt_and_report(프로젝트 자유 문장 passthrough) 백그라운드 흐름."""

    @pytest.mark.asyncio
    async def test_posts_result_and_completes_task(self):
        from slack_bot.handlers import _run_prompt_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()
        app.client.reactions_remove = AsyncMock()

        project = MagicMock()
        task_manager = MagicMock()
        task = MagicMock()
        task.task_id = "007"
        task.status = "running"
        task.args = "매수 로직 정리"
        task.thread_ts = "1234.5678"
        task.channel = "C123"

        run_result = MagicMock()
        run_result.success = True
        run_result.output = "완료된 결과"

        with patch(
            "slack_bot.handlers.run_free_prompt",
            new_callable=AsyncMock,
            return_value=run_result,
        ) as mock_run:
            assert sem._value == 1
            await _run_prompt_and_report(
                app,
                task_manager,
                project,
                task,
                "매수 로직 정리",
                sem,
                event_ts="1000.0",
            )
            assert sem._value == 1

        mock_run.assert_awaited_once_with(project, "매수 로직 정리", task)
        app.client.chat_postMessage.assert_called_once()
        task_manager.complete_task.assert_called_once_with("007", True)
        app.client.reactions_remove.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stopped_task_reports_cancel_and_skips_complete(self):
        from slack_bot.handlers import _run_prompt_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()
        app.client.reactions_remove = AsyncMock()

        project = MagicMock()
        task_manager = MagicMock()
        task = MagicMock()
        task.task_id = "008"
        task.status = "stopped"
        task.args = "취소될 프롬프트"
        task.thread_ts = "1234.5678"
        task.channel = "C123"

        run_result = MagicMock()
        run_result.success = False
        run_result.output = ""

        with patch(
            "slack_bot.handlers.run_free_prompt",
            new_callable=AsyncMock,
            return_value=run_result,
        ):
            await _run_prompt_and_report(
                app, task_manager, project, task, "취소될 프롬프트", sem
            )

        # 취소 안내만 전송하고 complete_task는 호출하지 않는다.
        app.client.chat_postMessage.assert_called_once()
        task_manager.complete_task.assert_not_called()


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

    @pytest.mark.asyncio
    async def test_deletes_progress_message_after_answer(self):
        """progress_ts가 주어지면 답변 도착 후 시작 알림 메시지를 삭제한다."""
        from slack_bot.handlers import _run_chat_question_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()
        app.client.chat_delete = AsyncMock()

        task_manager = MagicMock()
        task = MagicMock()
        task.task_id = "045"
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
                progress_ts="9999.0001",
            )

        app.client.chat_postMessage.assert_awaited_once()
        app.client.chat_delete.assert_awaited_once_with(
            channel="C123", ts="9999.0001"
        )

    @pytest.mark.asyncio
    async def test_deletes_progress_message_on_cancellation(self):
        """취소 메시지를 보낸 직후에도 시작 알림 메시지를 삭제한다."""
        from slack_bot.handlers import _run_chat_question_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()
        app.client.chat_delete = AsyncMock()

        task_manager = MagicMock()
        task = MagicMock()
        task.task_id = "046"
        task.status = "stopped"

        with patch(
            "slack_bot.handlers.answer_question",
            new_callable=AsyncMock,
            return_value="중간 답변",
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
                progress_ts="9999.0002",
            )

        app.client.chat_delete.assert_awaited_once_with(
            channel="C123", ts="9999.0002"
        )

    @pytest.mark.asyncio
    async def test_skips_delete_when_progress_ts_none(self):
        """progress_ts가 None이면 chat_delete를 호출하지 않는다 (하위호환)."""
        from slack_bot.handlers import _run_chat_question_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()
        app.client.chat_delete = AsyncMock()

        task_manager = MagicMock()
        task = MagicMock()
        task.task_id = "047"
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

        app.client.chat_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_failure_does_not_break_flow(self):
        """chat_delete가 실패해도 답변 흐름은 정상 종료된다."""
        from slack_bot.handlers import _run_chat_question_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()
        app.client.chat_delete = AsyncMock(
            side_effect=RuntimeError("delete failed")
        )

        task_manager = MagicMock()
        task = MagicMock()
        task.task_id = "048"
        task.status = "running"

        with patch(
            "slack_bot.handlers.answer_question",
            new_callable=AsyncMock,
            return_value="답변 본문",
        ):
            # 예외가 흐름을 깨지 않아야 함
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
                progress_ts="9999.0003",
            )

        task_manager.complete_task.assert_called_once_with("048", True)
        app.client.chat_postMessage.assert_awaited_once()


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


class TestUnknownShellRouting:
    """unknown_shell 인텐트가 즉시 에러 메시지로 응답하는지 — 1시간 안전망 방지."""

    @pytest.mark.asyncio
    async def test_unknown_shell_responds_immediately_with_project_list(self):
        """등록 안 된 프로젝트 + 셸 명령 입력 시 즉시 에러 응답 (백그라운드 미진입)."""
        from slack_bot.handlers import register_handlers

        events: dict = {}

        def capture_event(event_type):
            def decorator(func):
                events[event_type] = func
                return func
            return decorator

        app = MagicMock()
        app.event = capture_event
        app.action = lambda action_id: lambda f: f
        app.command = lambda name: lambda f: f

        task_manager = MagicMock()
        task_manager.get_running_tasks.return_value = []
        task_manager.get_tasks_for_channel.return_value = []
        task_manager.cleanup_old = MagicMock()

        from slack_bot.config import AppConfig, ProjectConfig, SecurityConfig

        with patch("slack_bot.handlers.load_projects") as mock_load:
            mock_load.return_value = AppConfig(
                projects={
                    "ra-backend": ProjectConfig(
                        name="ra-backend",
                        path="/tmp/ra-backend",
                        description="RA 백엔드",
                    ),
                },
                security=SecurityConfig(allowed_users={"admin": ["*"]}),
            )
            register_handlers(app, task_manager)

        handler = events.get("app_mention")
        assert handler is not None

        say = AsyncMock()
        client = MagicMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()

        event = {
            "text": "<@U0BOT> trader 에서\n  uv run python -m scripts.foo 실행해",
            "ts": "1000.0",
            "user": "U1",
            "channel": "C123",
            "channel_type": "channel",
        }

        await handler(event=event, say=say, client=client)

        # 즉시 에러 응답 (백그라운드 클로드 호출 없이)
        say.assert_awaited()
        msg_text = " ".join(
            str(call.args[0]) if call.args else str(call.kwargs.get("text", ""))
            for call in say.await_args_list
        )
        assert "프로젝트를 식별하지 못했습니다" in msg_text or "셸 명령" in msg_text
        assert "ra-backend" in msg_text


def _register_event_handlers(projects=None):
    """register_handlers를 mock app으로 호출하고 @app.event 콜백을 캡쳐."""
    from slack_bot.config import AppConfig, SecurityConfig
    from slack_bot.handlers import register_handlers

    events: dict = {}

    def capture_event(event_type):
        def decorator(func):
            events[event_type] = func
            return func
        return decorator

    app = MagicMock()
    app.event = capture_event
    app.action = lambda action_id: lambda f: f
    app.command = lambda name: lambda f: f
    # 백그라운드 리포트 태스크가 app.client로 결과를 게시하므로 awaitable로 둔다.
    app.client.chat_postMessage = AsyncMock()
    app.client.chat_delete = AsyncMock()
    app.client.reactions_remove = AsyncMock()
    app.client.reactions_add = AsyncMock()

    task_manager = MagicMock()
    task_manager.get_running_tasks.return_value = []
    task_manager.get_tasks_for_channel.return_value = []
    task_manager.cleanup_old = MagicMock()
    task_manager.find_running_duplicate.return_value = None

    async def _create_task(*args, **kwargs):
        t = MagicMock()
        t.task_id = "001"
        t.status = "running"
        return t

    task_manager.create_task = AsyncMock(side_effect=_create_task)

    async def _create_task_if_absent(*args, **kwargs):
        t = await _create_task()
        return t, True  # 기본: 중복 없음 → 생성됨

    task_manager.create_task_if_absent = AsyncMock(side_effect=_create_task_if_absent)

    with patch("slack_bot.handlers.load_projects") as mock_load:
        mock_load.return_value = AppConfig(
            projects=projects or {},
            security=SecurityConfig(allowed_users={"admin": ["*"]}),
        )
        register_handlers(app, task_manager)

    return events, task_manager


async def _drain_background_tasks():
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class TestAttachmentFlow:
    """첨부파일이 다운로드되어 질문 프롬프트에 주입되는지 — 엔드투엔드 배선."""

    @pytest.mark.asyncio
    async def test_dm_file_share_subtype_not_dropped(self):
        """파일 업로드 DM(subtype=file_share)이 무시되지 않고 다운로드까지 간다."""
        from slack_bot.attachments import DownloadedFile

        events, _ = _register_event_handlers()
        handler = events["message"]

        say = AsyncMock()
        client = MagicMock()
        client.token = "xoxb-test"
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        client.conversations_history = AsyncMock(return_value={"messages": []})
        client.chat_postMessage = AsyncMock()
        client.chat_delete = AsyncMock()

        say.return_value = {"ts": "1.0"}

        event = {
            "channel_type": "im",
            "subtype": "file_share",
            "text": "이 표 분석해줘",
            "ts": "1000.0",
            "user": "U1",
            "channel": "D123",
            "files": [{"name": "data.csv", "url_private_download": "http://x"}],
        }

        captured = {}

        async def fake_answer(question, *args, **kwargs):
            captured["question"] = question
            return "분석 결과"

        with patch(
            "slack_bot.handlers.download_slack_files",
            new_callable=AsyncMock,
            return_value=(
                [DownloadedFile("/tmp/x/data.csv", "data.csv", "text/csv", 10)],
                [],
            ),
        ) as mock_dl, patch(
            "slack_bot.handlers.answer_question",
            new=fake_answer,
        ):
            await handler(event=event, say=say, client=client)
            await _drain_background_tasks()

        # 다운로드가 호출됐고, 받은 파일 경로가 질문 프롬프트에 주입됐다.
        mock_dl.assert_awaited_once()
        assert mock_dl.await_args.args[0] == event["files"]
        assert "/tmp/x/data.csv" in captured["question"]
        assert "이 표 분석해줘" in captured["question"]

    @pytest.mark.asyncio
    async def test_non_file_subtype_still_dropped(self):
        """file_share 외 subtype(message_changed 등)은 여전히 무시된다."""
        events, _ = _register_event_handlers()
        handler = events["message"]

        say = AsyncMock()
        client = MagicMock()
        client.reactions_add = AsyncMock()

        event = {
            "channel_type": "im",
            "subtype": "message_changed",
            "text": "edited",
            "ts": "1000.0",
            "user": "U1",
            "channel": "D123",
        }

        await handler(event=event, say=say, client=client)
        say.assert_not_awaited()
        client.reactions_add.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_text_only_message_skips_download(self):
        """첨부가 없으면 download_slack_files를 호출하지 않는다."""
        events, _ = _register_event_handlers()
        handler = events["message"]

        say = AsyncMock()
        client = MagicMock()
        client.token = "xoxb-test"
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        client.conversations_history = AsyncMock(return_value={"messages": []})
        client.chat_postMessage = AsyncMock()
        client.chat_delete = AsyncMock()

        say.return_value = {"ts": "1.0"}

        event = {
            "channel_type": "im",
            "text": "그냥 질문",
            "ts": "1000.0",
            "user": "U1",
            "channel": "D123",
        }

        with patch(
            "slack_bot.handlers.download_slack_files",
            new_callable=AsyncMock,
            return_value=([], []),
        ) as mock_dl, patch(
            "slack_bot.handlers.answer_question",
            new_callable=AsyncMock,
            return_value="답변",
        ):
            await handler(event=event, say=say, client=client)
            await _drain_background_tasks()

        mock_dl.assert_not_awaited()


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
        # slash 컨텍스트엔 thread가 없으므로 thread_ts가 API에 도달하지 않아야 함
        assert "thread_ts" not in call_kwargs


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


def _trader_projects():
    from slack_bot.config import ProjectConfig

    return {
        "trader": ProjectConfig(
            name="trader", path="/tmp/trader", description="트레이더"
        ),
    }


def _command_event(ts: str = "1000.0"):
    return {
        "text": "<@U0BOT> trader 하네스 TRD-128 돌려줘",
        "ts": ts,
        "user": "U1",
        "channel": "C123",
        "channel_type": "channel",
    }


def _say_text(say: AsyncMock) -> str:
    return " ".join(
        str(c.args[0]) if c.args else str(c.kwargs.get("text", ""))
        for c in say.await_args_list
    )


class TestEventDedup:
    """Slack 이벤트 재전송 중복 차단 — 같은 event_ts는 한 번만 처리한다.

    회귀 방지: 중복 차단이 없으면 재전송된 멘션마다 claude -p 하네스가 겹쳐
    실행돼 토큰을 배수로 소모했다 (TRD-128 사고).
    """

    @pytest.mark.asyncio
    async def test_same_event_ts_processed_once(self):
        events, task_manager = _register_event_handlers(_trader_projects())
        handler = events["app_mention"]

        say = AsyncMock()
        client = MagicMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        client.chat_postMessage = AsyncMock()

        event = _command_event("1000.0")

        with patch(
            "slack_bot.handlers.run_claude",
            new_callable=AsyncMock,
            return_value=MagicMock(success=True, output="done"),
        ):
            await handler(event=event, say=say, client=client)
            await handler(event=event, say=say, client=client)  # 재전송
            await _drain_background_tasks()

        # 태스크 생성은 첫 이벤트에서만
        assert task_manager.create_task_if_absent.await_count == 1
        # 재전송은 리액션조차 추가하지 않고 즉시 반환
        assert client.reactions_add.await_count == 1

    @pytest.mark.asyncio
    async def test_distinct_event_ts_both_processed(self):
        """서로 다른 event_ts(= 다른 메시지)는 둘 다 처리된다."""
        events, task_manager = _register_event_handlers(_trader_projects())
        handler = events["app_mention"]

        say = AsyncMock()
        client = MagicMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        client.chat_postMessage = AsyncMock()

        with patch(
            "slack_bot.handlers.run_claude",
            new_callable=AsyncMock,
            return_value=MagicMock(success=True, output="done"),
        ):
            await handler(event=_command_event("1000.0"), say=say, client=client)
            await handler(event=_command_event("2000.0"), say=say, client=client)
            await _drain_background_tasks()

        assert task_manager.create_task_if_absent.await_count == 2


def _trader_db_projects():
    """db 설정이 있는 trader 프로젝트 — db_query 인텐트 라우팅용."""
    from slack_bot.config import DBConfig, ProjectConfig

    return {
        "trader": ProjectConfig(
            name="trader",
            path="/tmp/trader",
            description="트레이더",
            db=DBConfig(db_type="sqlite", db_path="db.sqlite3"),
        ),
    }


def _shell_event(ts: str = "1000.0"):
    return {
        "text": "<@U0BOT> trader 에서 uv run pytest 실행해",
        "ts": ts,
        "user": "U1",
        "channel": "C123",
        "channel_type": "channel",
    }


def _db_event(ts: str = "1000.0", *, export: bool = False):
    text = (
        "<@U0BOT> trader 유저 목록 조회해서 엑셀로 추출해줘"
        if export
        else "<@U0BOT> trader 유저 수 조회해줘"
    )
    return {
        "text": text,
        "ts": ts,
        "user": "U1",
        "channel": "C123",
        "channel_type": "channel",
    }


def _mark_duplicate(task_manager, task_id: str = "007"):
    """create_task_if_absent가 (기존 태스크, False)를 반환하도록 오버라이드."""
    existing = MagicMock()
    existing.task_id = task_id
    existing.status = "running"
    task_manager.create_task_if_absent = AsyncMock(return_value=(existing, False))
    return existing


class TestDuplicateRunningGuard:
    """동일 작업이 이미 실행 중이면 재실행을 건너뛴다 (중복 토큰 소모 방지)."""

    @pytest.mark.asyncio
    async def test_command_skipped_when_duplicate_running(self):
        events, task_manager = _register_event_handlers(_trader_projects())
        _mark_duplicate(task_manager)

        handler = events["app_mention"]
        say = AsyncMock()
        client = MagicMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        client.chat_postMessage = AsyncMock()

        with patch(
            "slack_bot.handlers.run_claude", new_callable=AsyncMock
        ) as mock_run:
            await handler(event=_command_event("3000.0"), say=say, client=client)
            await _drain_background_tasks()

        # 원자 경로를 정확히 한 번 거치고, claude -p는 돌지 않는다
        task_manager.create_task_if_absent.assert_awaited_once()
        mock_run.assert_not_awaited()
        # 사용자에게 중복 안내(기존 태스크 ID 포함)
        msg = _say_text(say)
        assert "이미" in msg and "007" in msg

    @pytest.mark.asyncio
    async def test_command_runs_when_no_duplicate(self):
        """중복이 없으면 정상적으로 태스크를 생성한다 (가드가 정상 실행을 막지 않음)."""
        events, task_manager = _register_event_handlers(_trader_projects())

        handler = events["app_mention"]
        say = AsyncMock()
        client = MagicMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        client.chat_postMessage = AsyncMock()

        with patch(
            "slack_bot.handlers.run_claude",
            new_callable=AsyncMock,
            return_value=MagicMock(success=True, output="done"),
        ):
            await handler(event=_command_event("4000.0"), say=say, client=client)
            await _drain_background_tasks()

        task_manager.create_task_if_absent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shell_skipped_when_duplicate_running(self):
        events, task_manager = _register_event_handlers(_trader_projects())
        _mark_duplicate(task_manager)

        handler = events["app_mention"]
        say = AsyncMock()
        client = MagicMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        client.chat_postMessage = AsyncMock()

        with patch(
            "slack_bot.handlers._run_shell_and_report", new_callable=AsyncMock
        ) as mock_run:
            await handler(event=_shell_event("3100.0"), say=say, client=client)
            await _drain_background_tasks()

        task_manager.create_task_if_absent.assert_awaited_once()
        mock_run.assert_not_awaited()
        msg = _say_text(say)
        assert "이미" in msg and "007" in msg

    @pytest.mark.asyncio
    async def test_shell_runs_when_no_duplicate(self):
        """shell 중복이 없으면 정상적으로 백그라운드 실행된다."""
        events, task_manager = _register_event_handlers(_trader_projects())

        handler = events["app_mention"]
        say = AsyncMock()
        client = MagicMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        client.chat_postMessage = AsyncMock()

        with patch(
            "slack_bot.handlers._run_shell_and_report", new_callable=AsyncMock
        ) as mock_run:
            await handler(event=_shell_event("4100.0"), say=say, client=client)
            await _drain_background_tasks()

        task_manager.create_task_if_absent.assert_awaited_once()
        mock_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_db_query_skipped_when_duplicate_running(self):
        events, task_manager = _register_event_handlers(_trader_db_projects())
        _mark_duplicate(task_manager)

        handler = events["app_mention"]
        say = AsyncMock()
        client = MagicMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        client.chat_postMessage = AsyncMock()

        with patch(
            "slack_bot.handlers._run_db_query_and_report", new_callable=AsyncMock
        ) as mock_run:
            await handler(event=_db_event("3200.0"), say=say, client=client)
            await _drain_background_tasks()

        task_manager.create_task_if_absent.assert_awaited_once()
        mock_run.assert_not_awaited()
        msg = _say_text(say)
        assert "이미" in msg and "007" in msg

    @pytest.mark.asyncio
    async def test_db_query_runs_when_no_duplicate(self):
        """db 일반 조회 중복이 없으면 정상적으로 백그라운드 실행된다."""
        events, task_manager = _register_event_handlers(_trader_db_projects())

        handler = events["app_mention"]
        say = AsyncMock()
        client = MagicMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        client.chat_postMessage = AsyncMock()

        with patch(
            "slack_bot.handlers._run_db_query_and_report", new_callable=AsyncMock
        ) as mock_run:
            await handler(event=_db_event("4200.0"), say=say, client=client)
            await _drain_background_tasks()

        task_manager.create_task_if_absent.assert_awaited_once()
        mock_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_db_export_skipped_when_duplicate_running(self):
        events, task_manager = _register_event_handlers(_trader_db_projects())
        _mark_duplicate(task_manager)

        handler = events["app_mention"]
        say = AsyncMock()
        client = MagicMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        client.chat_postMessage = AsyncMock()

        with patch(
            "slack_bot.handlers._run_db_query_export_and_report",
            new_callable=AsyncMock,
        ) as mock_run:
            await handler(
                event=_db_event("3300.0", export=True), say=say, client=client
            )
            await _drain_background_tasks()

        task_manager.create_task_if_absent.assert_awaited_once()
        mock_run.assert_not_awaited()
        msg = _say_text(say)
        assert "이미" in msg and "007" in msg

    @pytest.mark.asyncio
    async def test_db_export_runs_when_no_duplicate(self):
        """db export 중복이 없으면 정상적으로 백그라운드 실행된다."""
        events, task_manager = _register_event_handlers(_trader_db_projects())

        handler = events["app_mention"]
        say = AsyncMock()
        client = MagicMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()
        client.chat_postMessage = AsyncMock()

        with patch(
            "slack_bot.handlers._run_db_query_export_and_report",
            new_callable=AsyncMock,
        ) as mock_run:
            await handler(
                event=_db_event("4300.0", export=True), say=say, client=client
            )
            await _drain_background_tasks()

        task_manager.create_task_if_absent.assert_awaited_once()
        mock_run.assert_awaited_once()
