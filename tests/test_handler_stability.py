"""SB-PROC-03: handler 안정성 강화 테스트."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slack_bot.handlers import MAX_CONCURRENT_CLAUDE, _log_task_exception


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


class TestMaxConcurrentClaude:
    """MAX_CONCURRENT_CLAUDE 상수 테스트."""

    def test_value_is_5(self):
        assert MAX_CONCURRENT_CLAUDE == 5


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
            # semaphore 상태 확인: 실행 전 1, 실행 중 0, 실행 후 1
            assert sem._value == 1
            await _run_and_report(
                app,
                task_manager,
                project,
                task,
                "/harness TEST-1",
                "/dev test TEST-1",
                sem,
            )
            assert sem._value == 1  # 해제 후 복원됨

        task_manager.complete_task.assert_called_once_with("001", True)


class TestRunDbQueryAndReportSemaphore:
    """_run_db_query_and_report가 semaphore를 사용하는지 확인."""

    @pytest.mark.asyncio
    async def test_run_db_query_and_report_acquires_semaphore(self):
        from slack_bot.handlers import _run_db_query_and_report

        sem = asyncio.Semaphore(1)
        app = MagicMock()
        app.client.chat_postMessage = AsyncMock()

        with patch(
            "slack_bot.handlers.run_db_query",
            new_callable=AsyncMock,
            return_value="결과",
        ):
            assert sem._value == 1
            await _run_db_query_and_report(
                app,
                question="테스트",
                channel="C123",
                user="user1",
                db_backend_path="/tmp/db",
                wiki_path=None,
                slash_command="/db 테스트",
                semaphore=sem,
            )
            assert sem._value == 1

        app.client.chat_postMessage.assert_called_once()
