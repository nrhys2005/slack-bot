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

    def test_restart_via_slash_command(self):
        """/restart 슬래시 명령으로만 재시작이 트리거된다."""
        intent = parse_intent("/restart", self._projects)
        assert intent.type == "admin"
        assert intent.command == "restart"

    @pytest.mark.parametrize(
        "text",
        [
            "재시작",
            "재시작해줘",
            "리스타트 해줘",
            "restart please",
            "봇 재시작이 필요할까?",
        ],
    )
    def test_restart_natural_language_does_not_trigger_admin(self, text: str):
        """자연어 '재시작/restart/리스타트'는 admin으로 잡히지 않는다 (오매칭 방지)."""
        intent = parse_intent(text, self._projects)
        assert not (intent.type == "admin" and intent.command == "restart"), (
            f"'{text}'가 admin/restart로 오매칭됨"
        )


class TestTaskControlIntent:
    """task_control 인텐트 파싱 회귀 — `/stop` 슬래시 전용."""

    _projects: dict[str, ProjectConfig] = {}

    def test_stop_via_slash_with_task_id(self):
        """/stop <task_id> → task_control/stop."""
        intent = parse_intent("/stop 003", self._projects)
        assert intent.type == "task_control"
        assert intent.command == "stop"
        assert intent.args == "003"

    def test_stop_via_slash_without_args_shows_list(self):
        """/stop (인자 없음) → task_control/list."""
        intent = parse_intent("/stop", self._projects)
        assert intent.type == "task_control"
        assert intent.command == "list"

    def test_task_list_natural_language(self):
        """'태스크' 자연어는 목록 조회로 매칭된다."""
        intent = parse_intent("실행중인 태스크 보여줘", self._projects)
        assert intent.type == "task_control"
        assert intent.command == "list"

    @pytest.mark.parametrize(
        "text",
        [
            "중단",
            "003번 중단해줘",
            "멈춰",
            "stop 003",
            "이거 중단해야겠다",
            "프로젝트 중단됐어",
        ],
    )
    def test_stop_natural_language_does_not_trigger(self, text: str):
        """자연어 '중단/멈춰/stop'은 task_control/stop으로 잡히지 않는다."""
        intent = parse_intent(text, self._projects)
        assert not (
            intent.type == "task_control" and intent.command == "stop"
        ), f"'{text}'가 task_control/stop으로 오매칭됨"


# ----------------------------------------------------------------
# confirm_auth_login 핸들러 테스트
# ----------------------------------------------------------------


def _make_body(channel_id: str = "C123", msg_ts: str = "1234.5678") -> dict:
    return {
        "channel": {"id": channel_id},
        "message": {"ts": msg_ts},
        "actions": [{"value": '{"user_id":"U1","channel":"C123"}'}],
    }


def _setup_handlers():
    """`register_handlers`를 호출하고 등록된 action 핸들러를 캡처해서 반환."""
    from slack_bot.handlers import register_handlers

    app = MagicMock()
    task_manager = MagicMock()
    task_manager.get_running_tasks.return_value = []
    task_manager.cleanup_old = MagicMock()

    handlers: dict = {}

    def capture_action(action_id):
        def decorator(func):
            handlers[action_id] = func
            return func
        return decorator

    app.action = capture_action
    app.event = lambda event_type: lambda f: f
    app.command = lambda cmd: lambda f: f

    with patch("slack_bot.handlers.load_projects") as mock_load:
        mock_config = MagicMock()
        mock_config.projects = {}
        mock_config.security.allowed_users = []
        mock_load.return_value = mock_config
        register_handlers(app, task_manager)

    return app, handlers


def _make_mock_proc(url_line: bytes, returncode_after_stdin: int):
    """URL 한 줄을 stdout으로 흘리고, stdin이 닫히면 returncode를 세팅하는 mock proc."""
    proc = MagicMock()
    proc.returncode = None

    # stdout: URL 한 줄 후 종료
    async def fake_stdout():
        yield url_line

    # stderr: 비어 있음
    async def fake_stderr():
        if False:
            yield b""
        return

    proc.stdout = fake_stdout()
    proc.stderr = fake_stderr()

    # stdin: write/drain/close 호출되면 returncode 세팅
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()

    def _close():
        proc.returncode = returncode_after_stdin
    stdin.close = MagicMock(side_effect=_close)
    proc.stdin = stdin

    async def fake_wait():
        # close가 호출된 뒤 returncode가 세팅되어 있어야 함
        return proc.returncode if proc.returncode is not None else 0
    proc.wait = fake_wait

    def _kill():
        proc.returncode = -9
    proc.kill = MagicMock(side_effect=_kill)
    return proc


async def _inject_code(app, code: str, *, max_wait: float = 5.0) -> None:
    """`_pending_auth_sessions`에 세션이 등록되면 code_future를 resolve."""
    deadline = asyncio.get_running_loop().time() + max_wait
    while asyncio.get_running_loop().time() < deadline:
        sessions = getattr(app, "_pending_auth_sessions", {})
        for session in list(sessions.values()):
            if not session.code_future.done():
                session.code_future.set_result(code)
                return
        await asyncio.sleep(0.01)
    raise AssertionError("auth session never registered")


class TestConfirmAuthLogin:
    """confirm_auth_login 액션 핸들러 테스트."""

    @pytest.mark.asyncio
    async def test_success_flow(self):
        """URL 출력 → 코드 입력 → 성공 메시지."""
        app, handlers = _setup_handlers()
        handler = handlers["confirm_auth_login"]

        ack = AsyncMock()
        client = MagicMock()
        client.chat_update = AsyncMock()
        client.chat_postMessage = AsyncMock()

        mock_proc = _make_mock_proc(
            b"Open this URL: https://auth.example.com/login?code=abc\n",
            returncode_after_stdin=0,
        )
        body = _make_body()

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            # 핸들러를 백그라운드로 실행하면서 별도 태스크에서 코드 주입
            handler_task = asyncio.create_task(
                handler(ack=ack, body=body, client=client)
            )
            await _inject_code(app, "test-auth-code-123")
            await asyncio.wait_for(handler_task, timeout=5)

        ack.assert_awaited_once()
        # URL 안내 메시지가 게시됐는지
        url_calls = [
            c for c in client.chat_postMessage.call_args_list
            if "브라우저에서" in str(c)
        ]
        assert len(url_calls) >= 1
        # 코드가 stdin에 기록됐는지
        mock_proc.stdin.write.assert_called_once_with(b"test-auth-code-123\n")
        mock_proc.stdin.close.assert_called_once()
        # 성공 메시지
        success_calls = [
            c for c in client.chat_postMessage.call_args_list
            if "인증이 완료되었습니다" in str(c)
        ]
        assert len(success_calls) >= 1

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        """claude CLI가 없을 때 에러 메시지 전송 확인."""
        app, handlers = _setup_handlers()
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
        """코드 입력 후 인증 실패 시 에러 메시지 전송 확인."""
        app, handlers = _setup_handlers()
        handler = handlers["confirm_auth_login"]

        ack = AsyncMock()
        client = MagicMock()
        client.chat_update = AsyncMock()
        client.chat_postMessage = AsyncMock()

        mock_proc = _make_mock_proc(
            b"Open this URL: https://auth.example.com/login?code=abc\n",
            returncode_after_stdin=1,
        )
        body = _make_body()

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            handler_task = asyncio.create_task(
                handler(ack=ack, body=body, client=client)
            )
            await _inject_code(app, "wrong-code")
            await asyncio.wait_for(handler_task, timeout=5)

        error_calls = [
            c for c in client.chat_postMessage.call_args_list
            if "인증 실패" in str(c)
        ]
        assert len(error_calls) == 1


class TestAuthCodeRouting:
    """진행 중 인증 세션이 후속 메시지를 코드 입력으로 가로채는지 확인."""

    @pytest.mark.asyncio
    async def test_full_flow_via_message_handler(self):
        """confirm → URL → 사용자가 스레드에 코드 입력 → 인증 완료."""
        from slack_bot.handlers import AuthSession

        app, handlers = _setup_handlers()
        confirm_handler = handlers["confirm_auth_login"]

        ack = AsyncMock()
        client = MagicMock()
        client.chat_update = AsyncMock()
        client.chat_postMessage = AsyncMock()

        mock_proc = _make_mock_proc(
            b"Visit: https://auth.example.com/x\n",
            returncode_after_stdin=0,
        )
        body = _make_body(channel_id="C1", msg_ts="T1")

        async def _drive_message():
            """세션이 등록되면 _find/_resolve를 흉내내어 코드를 주입."""
            deadline = asyncio.get_running_loop().time() + 5.0
            while asyncio.get_running_loop().time() < deadline:
                sessions = app._pending_auth_sessions
                # 채널 C1, 스레드=T1 (URL 메시지가 게시된 스레드)
                target: AuthSession | None = None
                for s in sessions.values():
                    if s.channel == "C1" and s.thread_ts == "T1" and not s.code_future.done():
                        target = s
                        break
                if target:
                    target.code_future.set_result("my-code")
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("session never appeared")

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            await asyncio.gather(
                confirm_handler(ack=ack, body=body, client=client),
                _drive_message(),
            )

        mock_proc.stdin.write.assert_called_once_with(b"my-code\n")
        assert any(
            "인증이 완료되었습니다" in str(c)
            for c in client.chat_postMessage.call_args_list
        )

    @pytest.mark.asyncio
    async def test_cancel_keyword_kills_auth(self):
        """사용자가 '취소'를 입력하면 future가 CancelledError로 닫히고 프로세스가 죽는다."""
        from slack_bot.handlers import AuthSession

        app, handlers = _setup_handlers()
        confirm_handler = handlers["confirm_auth_login"]

        ack = AsyncMock()
        client = MagicMock()
        client.chat_update = AsyncMock()
        client.chat_postMessage = AsyncMock()

        mock_proc = _make_mock_proc(
            b"URL: https://auth.example.com/x\n",
            returncode_after_stdin=0,
        )
        body = _make_body()

        async def _cancel_session():
            deadline = asyncio.get_running_loop().time() + 5.0
            while asyncio.get_running_loop().time() < deadline:
                for s in app._pending_auth_sessions.values():
                    if not s.code_future.done():
                        # 메시지 핸들러의 취소 경로와 동일하게 처리
                        if s.proc.returncode is None:
                            s.proc.kill()
                        s.code_future.set_exception(asyncio.CancelledError("user cancelled"))
                        return
                await asyncio.sleep(0.01)
            raise AssertionError("session never appeared")

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            await asyncio.gather(
                confirm_handler(ack=ack, body=body, client=client),
                _cancel_session(),
            )

        # stdin이 사용되지 않았어야 함 (취소되었으므로)
        mock_proc.stdin.write.assert_not_called()
        # kill 호출
        mock_proc.kill.assert_called()


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
