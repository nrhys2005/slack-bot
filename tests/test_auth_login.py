"""claude auth login 기능 테스트."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slack_bot.config import ProjectConfig
from slack_bot.intent import Intent, parse_intent


# ----------------------------------------------------------------
# intent 파싱 테스트
# ----------------------------------------------------------------


class TestAuthLoginIntent:
    """auth_login 관련 키워드가 admin 인텐트로 파싱되는지 확인."""

    _projects: dict[str, ProjectConfig] = {}

    @pytest.mark.parametrize(
        "text",
        [
            "claude 로그인",
            "클로드 로그인 해줘",
            "claude login",
            "claude auth 해줘",
        ],
    )
    def test_auth_login_keywords(self, text: str):
        intent = parse_intent(text, self._projects)
        assert intent.type == "admin"
        assert intent.command == "auth_login"

    def test_claude_install_still_works(self):
        """auth_login 추가 후에도 install_claude가 정상 동작하는지 확인."""
        intent = parse_intent("claude 설치", self._projects)
        assert intent.type == "admin"
        assert intent.command == "install_claude"

    def test_restart_still_works(self):
        """기존 restart 키워드가 정상 동작하는지 확인."""
        intent = parse_intent("재시작", self._projects)
        assert intent.type == "admin"
        assert intent.command == "restart"


# ----------------------------------------------------------------
# confirm_auth_login 핸들러 테스트
# ----------------------------------------------------------------


def _make_body(channel_id: str = "C123", msg_ts: str = "1234.5678") -> dict:
    return {
        "channel": {"id": channel_id},
        "message": {"ts": msg_ts},
        "actions": [{"value": '{"user_id":"U1","channel":"C123"}'}],
    }


class TestConfirmAuthLogin:
    """confirm_auth_login 액션 핸들러 테스트."""

    @pytest.mark.asyncio
    async def test_success_flow(self):
        """인증 성공 시 완료 메시지가 전송되는지 확인."""
        from slack_bot.handlers import register_handlers

        app = MagicMock()
        task_manager = MagicMock()
        task_manager.get_running_tasks.return_value = []
        task_manager.cleanup_old = MagicMock()

        # register_handlers를 호출하여 핸들러를 등록
        handlers = {}

        def capture_action(action_id):
            def decorator(func):
                handlers[action_id] = func
                return func
            return decorator

        app.action = capture_action
        app.event = lambda event_type: lambda f: f

        with patch("slack_bot.handlers.load_projects") as mock_load:
            mock_config = MagicMock()
            mock_config.projects = {}
            mock_config.security.allowed_users = []
            mock_load.return_value = mock_config
            register_handlers(app, task_manager)

        handler = handlers.get("confirm_auth_login")
        assert handler is not None

        ack = AsyncMock()
        client = MagicMock()
        client.chat_update = AsyncMock()
        client.chat_postMessage = AsyncMock()

        # stdout에 URL을 출력하고 성공적으로 종료하는 프로세스 시뮬레이션
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        async def fake_stdout():
            yield b"Open this URL: https://auth.example.com/login?code=abc\n"

        async def fake_stderr():
            return
            yield  # make it an async generator

        mock_proc.stdout = fake_stdout()
        mock_proc.stderr = fake_stderr()

        async def fake_wait():
            return 0

        mock_proc.wait = fake_wait

        body = _make_body()

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
            await handler(ack=ack, body=body, client=client)

        ack.assert_awaited_once()
        client.chat_update.assert_awaited_once()

        # 성공 메시지 확인
        success_calls = [
            c for c in client.chat_postMessage.call_args_list
            if "인증이 완료되었습니다" in str(c)
        ]
        assert len(success_calls) >= 1

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        """claude CLI가 없을 때 에러 메시지 전송 확인."""
        from slack_bot.handlers import register_handlers

        app = MagicMock()
        task_manager = MagicMock()
        task_manager.get_running_tasks.return_value = []

        handlers = {}

        def capture_action(action_id):
            def decorator(func):
                handlers[action_id] = func
                return func
            return decorator

        app.action = capture_action
        app.event = lambda event_type: lambda f: f

        with patch("slack_bot.handlers.load_projects") as mock_load:
            mock_config = MagicMock()
            mock_config.projects = {}
            mock_config.security.allowed_users = []
            mock_load.return_value = mock_config
            register_handlers(app, task_manager)

        handler = handlers["confirm_auth_login"]

        ack = AsyncMock()
        client = MagicMock()
        client.chat_update = AsyncMock()
        client.chat_postMessage = AsyncMock()

        body = _make_body()

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=FileNotFoundError("claude not found"),
        ):
            await handler(ack=ack, body=body, client=client)

        ack.assert_awaited_once()
        error_calls = [
            c for c in client.chat_postMessage.call_args_list
            if "설치되어 있지 않습니다" in str(c)
        ]
        assert len(error_calls) == 1

    @pytest.mark.asyncio
    async def test_nonzero_exit(self):
        """비정상 종료 시 에러 메시지 전송 확인."""
        from slack_bot.handlers import register_handlers

        app = MagicMock()
        task_manager = MagicMock()
        task_manager.get_running_tasks.return_value = []

        handlers = {}

        def capture_action(action_id):
            def decorator(func):
                handlers[action_id] = func
                return func
            return decorator

        app.action = capture_action
        app.event = lambda event_type: lambda f: f

        with patch("slack_bot.handlers.load_projects") as mock_load:
            mock_config = MagicMock()
            mock_config.projects = {}
            mock_config.security.allowed_users = []
            mock_load.return_value = mock_config
            register_handlers(app, task_manager)

        handler = handlers["confirm_auth_login"]

        ack = AsyncMock()
        client = MagicMock()
        client.chat_update = AsyncMock()
        client.chat_postMessage = AsyncMock()

        mock_proc = MagicMock()
        mock_proc.returncode = 1

        async def fake_stdout():
            yield b"Error: authentication failed\n"

        async def fake_stderr():
            return
            yield

        mock_proc.stdout = fake_stdout()
        mock_proc.stderr = fake_stderr()

        async def fake_wait():
            return 1

        mock_proc.wait = fake_wait

        body = _make_body()

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
            await handler(ack=ack, body=body, client=client)

        error_calls = [
            c for c in client.chat_postMessage.call_args_list
            if "인증 실패" in str(c)
        ]
        assert len(error_calls) == 1


class TestCancelAuthLogin:
    """cancel_auth_login 액션 핸들러 테스트."""

    @pytest.mark.asyncio
    async def test_cancel_updates_message(self):
        """취소 버튼 클릭 시 메시지 업데이트 확인."""
        from slack_bot.handlers import register_handlers

        app = MagicMock()
        task_manager = MagicMock()
        task_manager.get_running_tasks.return_value = []

        handlers = {}

        def capture_action(action_id):
            def decorator(func):
                handlers[action_id] = func
                return func
            return decorator

        app.action = capture_action
        app.event = lambda event_type: lambda f: f

        with patch("slack_bot.handlers.load_projects") as mock_load:
            mock_config = MagicMock()
            mock_config.projects = {}
            mock_config.security.allowed_users = []
            mock_load.return_value = mock_config
            register_handlers(app, task_manager)

        handler = handlers["cancel_auth_login"]

        ack = AsyncMock()
        client = MagicMock()
        client.chat_update = AsyncMock()

        body = _make_body()

        await handler(ack=ack, body=body, client=client)

        ack.assert_awaited_once()
        client.chat_update.assert_awaited_once()
        call_kwargs = client.chat_update.call_args
        assert "취소했습니다" in str(call_kwargs)
