from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass

from slack_bolt.async_app import AsyncApp

from slack_bot.chat import answer_question
from slack_bot.config import ProjectConfig, load_projects
from slack_bot.db_query import run_db_query, run_db_query_export
from slack_bot.intent import _TASK_ID_RE, Intent, parse_intent
from slack_bot.runner import run_claude
from slack_bot.security import check_auth, make_safe_env, redact_output
from slack_bot.task_manager import TaskManager

logger = logging.getLogger(__name__)

MAX_CONCURRENT_CHAT = 3
MAX_CONCURRENT_TASK = 3

# `claude auth login`이 인증 URL을 출력한 뒤 stdin으로 입력받는 코드를
# Slack 메시지로 이어 받기 위해 사용하는 세션 상태.
@dataclass
class AuthSession:
    proc: asyncio.subprocess.Process
    user_id: str
    channel: str
    thread_ts: str   # URL 메시지가 게시된 스레드 (사용자 응답이 도착하는 곳)
    msg_ts: str      # 확인 버튼 메시지의 ts (세션 키 일부)
    code_future: asyncio.Future
    created_at: float


def _log_task_exception(t: asyncio.Task) -> None:
    """백그라운드 태스크의 미처리 예외를 로깅."""
    if t.cancelled():
        return
    exc = t.exception()
    if exc is not None:
        logger.error("백그라운드 태스크 예외: %s", exc, exc_info=exc)


def register_handlers(app: AsyncApp, task_manager: TaskManager) -> None:
    _chat_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHAT)
    _task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASK)
    _background_tasks: set[asyncio.Task] = set()

    # `claude auth login` 진행 중 코드 입력을 대기하는 세션들.
    # key = f"{channel}:{msg_ts}" (msg_ts는 확인 버튼 메시지의 ts).
    _pending_auth_sessions: dict[str, AuthSession] = {}
    # 테스트가 진행 중인 세션에 접근할 수 있도록 app에 노출 (production no-op).
    app._pending_auth_sessions = _pending_auth_sessions

    def _find_auth_session_for_message(
        channel: str,
        thread_ts: str,
        user_id: str,
        channel_type: str,
    ) -> AuthSession | None:
        """수신 메시지가 진행 중인 인증 세션의 코드 입력인지 판별."""
        for session in _pending_auth_sessions.values():
            if session.code_future.done():
                continue
            if session.user_id != user_id or session.channel != channel:
                continue
            # DM: 스레드가 없어도 동일 사용자의 진행 중 세션을 코드 입력으로 본다.
            if channel_type == "im":
                return session
            # 채널: 인증 URL이 게시된 스레드 안에서만 코드 입력으로 본다.
            if thread_ts and thread_ts in (session.thread_ts, session.msg_ts):
                return session
        return None

    app_config = load_projects()
    projects = app_config.projects

    # 프로젝트 분류
    wiki_projects = [p for p in projects.values() if p.wiki]
    db_projects = {n: p for n, p in projects.items() if p.db is not None}

    # ----------------------------------------------------------------
    # 통합 메시지 처리 로직
    # ----------------------------------------------------------------

    async def _handle_message(
        question: str,
        user_id: str,
        channel: str,
        thread_ts: str,
        event_ts: str,
        say,
        client,
        *,
        is_thread: bool = True,
        channel_type: str = "channel",
    ) -> None:
        """@멘션과 DM 공통 메시지 처리 흐름."""
        if not question:
            await say(
                "무엇을 도와드릴까요? 프로젝트 명령 실행, 상태 확인, 질문 등을 할 수 있습니다.",
                thread_ts=thread_ts,
            )
            return

        # `claude auth login` 진행 중이면 이 메시지를 인증 코드로 해석한다.
        # (intent 파싱이나 reactions 추가보다 먼저 가로채야 정상 흐름과 섞이지 않음)
        auth_session = _find_auth_session_for_message(
            channel, thread_ts, user_id, channel_type
        )
        if auth_session is not None and not auth_session.code_future.done():
            code = question.strip()
            if code.lower() in ("취소", "cancel", "stop"):
                if auth_session.proc.returncode is None:
                    try:
                        auth_session.proc.kill()
                    except ProcessLookupError:
                        pass
                if not auth_session.code_future.done():
                    auth_session.code_future.set_exception(
                        asyncio.CancelledError("user cancelled")
                    )
                await say(
                    ":no_entry_sign: Claude CLI 인증을 취소했습니다.",
                    thread_ts=thread_ts,
                )
            else:
                auth_session.code_future.set_result(code)
                await say(
                    ":key: 코드를 받았습니다. 인증을 마무리합니다…",
                    thread_ts=thread_ts,
                )
            return

        # 즉시 리액션
        try:
            await client.reactions_add(channel=channel, timestamp=event_ts, name="eyes")
        except Exception:
            logger.warning("리액션 추가 실패", exc_info=True)

        # 인텐트 파싱
        intent = parse_intent(question, projects)

        try:
            if intent.type == "admin":
                await _handle_admin_intent(intent, user_id, channel, thread_ts, say)
                return
            elif intent.type == "command":
                await _handle_command_intent(
                    intent, user_id, channel, thread_ts, say, client
                )
            elif intent.type == "shell_exec":
                await _handle_shell_exec_intent(
                    intent, user_id, channel, thread_ts, say, client
                )
            elif intent.type == "task_control":
                await _handle_task_control(intent, user_id, channel, thread_ts, say)
            elif intent.type == "db_query":
                await _handle_db_query_intent(
                    intent, user_id, channel, thread_ts, event_ts, say, client
                )
            elif intent.type in ("status", "question"):
                await _handle_question_intent(
                    intent,
                    question,
                    channel,
                    thread_ts,
                    event_ts,
                    say,
                    client,
                    is_thread=is_thread,
                    channel_type=channel_type,
                )
            elif intent.type == "unknown_shell":
                await _handle_unknown_shell(intent, thread_ts, say)
            else:
                await say(
                    "무엇을 도와드릴까요? 프로젝트 명령 실행, 상태 확인, 질문 등을 할 수 있습니다.",
                    thread_ts=thread_ts,
                )
        finally:
            # 리액션 제거
            try:
                await client.reactions_remove(
                    channel=channel, timestamp=event_ts, name="eyes"
                )
            except Exception:
                logger.warning("리액션 제거 실패", exc_info=True)

    # ----------------------------------------------------------------
    # 인텐트별 처리 함수
    # ----------------------------------------------------------------

    async def _handle_admin_intent(
        intent: Intent,
        user_id: str,
        channel: str,
        thread_ts: str,
        say,
    ) -> None:
        """관리 명령 (재시작/설치) → 확인 버튼 후 실행."""
        if not check_auth(user_id, "admin", app_config.security.allowed_users):
            await say(":no_entry: 관리 명령 권한이 없습니다.", thread_ts=thread_ts)
            return

        if intent.command == "restart":
            running = task_manager.get_running_tasks()
            warning = ""
            if running:
                warning = f"\n:warning: 실행 중인 태스크 {len(running)}개가 중단됩니다."

            action_data = json.dumps({"user_id": user_id, "channel": channel})
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"봇을 업데이트하고 재시작할까요?{warning}",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "재시작"},
                            "style": "danger",
                            "action_id": "confirm_restart",
                            "value": action_data,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "취소"},
                            "action_id": "cancel_restart",
                            "value": action_data,
                        },
                    ],
                },
            ]
            await say(blocks=blocks, text="봇 재시작 확인", thread_ts=thread_ts)

        elif intent.command == "auth_login":
            action_data = json.dumps({"user_id": user_id, "channel": channel})
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "Claude CLI 인증(`claude auth login`)을 실행할까요?\n"
                            "인증 URL이 생성되면 이 채널에 공유됩니다."
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "인증 시작"},
                            "style": "primary",
                            "action_id": "confirm_auth_login",
                            "value": action_data,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "취소"},
                            "action_id": "cancel_auth_login",
                            "value": action_data,
                        },
                    ],
                },
            ]
            await say(blocks=blocks, text="Claude CLI 인증 확인", thread_ts=thread_ts)

        elif intent.command == "install_claude":
            action_data = json.dumps({"user_id": user_id, "channel": channel})
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Claude CLI (`@anthropic-ai/claude-code`)를 설치할까요?",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "설치"},
                            "style": "primary",
                            "action_id": "confirm_install_claude",
                            "value": action_data,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "취소"},
                            "action_id": "cancel_install_claude",
                            "value": action_data,
                        },
                    ],
                },
            ]
            await say(blocks=blocks, text="Claude CLI 설치 확인", thread_ts=thread_ts)

    async def _handle_command_intent(
        intent: Intent,
        user_id: str,
        channel: str,
        thread_ts: str,
        say,
        client,
    ) -> None:
        """명령 실행 요청 → 즉시 백그라운드 실행 + 시작 알림."""
        project = projects.get(intent.project)
        if not project:
            project_list = ", ".join(f"`{n}`" for n in projects)
            await say(
                f"프로젝트를 식별하지 못했습니다. 등록된 프로젝트: {project_list}",
                thread_ts=thread_ts,
            )
            return

        prompt_display = f"/{intent.command} {intent.args}".strip()

        # 태스크 생성 및 백그라운드 실행
        task_manager.cleanup_old()
        task = await task_manager.create_task(
            intent.project,
            intent.command,
            intent.args,
            user_id,
            channel,
            thread_ts=thread_ts,
        )

        bg_task = asyncio.create_task(
            _run_and_report(
                app,
                task_manager,
                project,
                task,
                prompt_display,
                _task_semaphore,
            )
        )
        _background_tasks.add(bg_task)
        bg_task.add_done_callback(_background_tasks.discard)
        bg_task.add_done_callback(_log_task_exception)

        await say(
            f":rocket: *{intent.project}* `{prompt_display}` 실행을 시작합니다. "
            f"(태스크 ID: {task.task_id})",
            thread_ts=thread_ts,
        )

    async def _handle_unknown_shell(
        intent: Intent,
        thread_ts: str,
        say,
    ) -> None:
        """셸 명령처럼 보이지만 프로젝트를 식별하지 못한 경우 — 즉시 에러로 응답.

        이 경로가 없으면 question으로 흘러가 claude -p가 다중행 셸 명령을
        "질문"으로 받아 답을 못 찾고 1시간 안전 한계에 도달한다.
        """
        if projects:
            project_list = ", ".join(f"`{n}`" for n in projects)
            text = (
                ":warning: 셸 명령을 실행하려는 것으로 보이지만 프로젝트를 "
                "식별하지 못했습니다.\n"
                f"등록된 프로젝트: {project_list}\n"
                "형식: `<프로젝트명>에서 <셸 명령> 실행해줘`"
            )
        else:
            text = (
                ":warning: 셸 명령을 실행하려는 것으로 보이지만 등록된 "
                "프로젝트가 없습니다. `projects.yaml`을 확인해주세요."
            )
        await say(text, thread_ts=thread_ts)

    async def _handle_shell_exec_intent(
        intent: Intent,
        user_id: str,
        channel: str,
        thread_ts: str,
        say,
        client,
    ) -> None:
        """셸 명령 직접 실행 → 즉시 응답 + 백그라운드 실행."""
        project = projects.get(intent.project)
        if not project:
            project_list = ", ".join(f"`{n}`" for n in projects)
            await say(
                f"프로젝트를 식별하지 못했습니다. 등록된 프로젝트: {project_list}",
                thread_ts=thread_ts,
            )
            return

        log_path = f"/tmp/slackbot_shell_{int(time.time())}.log"

        task_manager.cleanup_old()
        task = await task_manager.create_task(
            intent.project, "shell", intent.command, user_id, channel,
            thread_ts=thread_ts,
        )

        # 시작 메시지를 먼저 전송 (백그라운드 태스크보다 먼저)
        await say(
            f":rocket: 명령어 실행을 시작합니다.\n"
            f"*   태스크 ID: {task.task_id}\n"
            f"*   프로젝트: {intent.project}\n"
            f"*   명령어: `{intent.command}`\n"
            f"*   로그: `{log_path}`\n\n"
            f"나중에 결과 확인하실 때 말씀해주세요.",
            thread_ts=thread_ts,
        )

        bg_task = asyncio.create_task(
            _run_shell_and_report(
                app, task_manager, project, task, log_path, _task_semaphore,
            )
        )
        _background_tasks.add(bg_task)
        bg_task.add_done_callback(_background_tasks.discard)
        bg_task.add_done_callback(_log_task_exception)

    async def _handle_task_control(
        intent: Intent,
        user_id: str,
        channel: str,
        thread_ts: str,
        say,
    ) -> None:
        """태스크 목록 조회 또는 중단."""
        if intent.command == "stop" and intent.args:
            if task_manager.stop_task(intent.args):
                await say(
                    f"태스크 {intent.args} 중단됨 :octagonal_sign:",
                    thread_ts=thread_ts,
                )
            else:
                await say(
                    f"태스크 `{intent.args}`를 찾을 수 없거나 이미 종료되었습니다.",
                    thread_ts=thread_ts,
                )
        else:
            # 목록 조회
            running = task_manager.get_running_tasks()
            if not running:
                await say("실행 중인 태스크가 없습니다.", thread_ts=thread_ts)
                return
            lines = []
            for t in running:
                lines.append(
                    f"*{t.task_id}* | {t.project_name} `/{t.command} {t.args}` | {t.elapsed_display} 경과"
                )
            await say(
                "실행 중인 태스크:\n" + "\n".join(lines),
                thread_ts=thread_ts,
            )

    async def _handle_db_query_intent(
        intent: Intent,
        user_id: str,
        channel: str,
        thread_ts: str,
        event_ts: str,
        say,
        client,
    ) -> None:
        """DB 조회 인텐트 처리."""
        # DB 프로젝트 결정
        db_project = None
        if intent.project and intent.project in db_projects:
            db_project = db_projects[intent.project]
        elif db_projects:
            db_project = next(iter(db_projects.values()))

        if not db_project:
            await say(
                "DB 조회가 가능한 프로젝트가 설정되어 있지 않습니다.",
                thread_ts=thread_ts,
            )
            return

        wiki_path = wiki_projects[0].path if wiki_projects else None

        task_manager.cleanup_old()
        db_task = await task_manager.create_task(
            db_project.name,
            "db_export" if intent.export else "db",
            intent.raw_text[:80],
            user_id,
            channel,
            thread_ts=thread_ts,
        )

        if intent.export:
            await say(
                f":outbox_tray: `{intent.raw_text}` 데이터 추출 중... "
                f"(ID: {db_task.task_id}, 취소: `/stop {db_task.task_id}`)",
                thread_ts=thread_ts,
            )
            bg_task = asyncio.create_task(
                _run_db_query_export_and_report(
                    app,
                    task_manager=task_manager,
                    task=db_task,
                    question=intent.raw_text,
                    channel=channel,
                    thread_ts=thread_ts,
                    user_id=user_id,
                    db_project=db_project,
                    wiki_path=wiki_path,
                    semaphore=_chat_semaphore,
                )
            )
        else:
            await say(
                f":mag: `{intent.raw_text}` 조회 중... "
                f"(ID: {db_task.task_id}, 취소: `/stop {db_task.task_id}`)",
                thread_ts=thread_ts,
            )
            bg_task = asyncio.create_task(
                _run_db_query_and_report(
                    app,
                    task_manager=task_manager,
                    task=db_task,
                    question=intent.raw_text,
                    channel=channel,
                    thread_ts=thread_ts,
                    user_id=user_id,
                    db_project=db_project,
                    wiki_path=wiki_path,
                    semaphore=_chat_semaphore,
                )
            )
        _background_tasks.add(bg_task)
        bg_task.add_done_callback(_background_tasks.discard)
        bg_task.add_done_callback(_log_task_exception)

    async def _handle_question_intent(
        intent: Intent,
        question: str,
        channel: str,
        thread_ts: str,
        event_ts: str,
        say,
        client,
        *,
        is_thread: bool = True,
        channel_type: str = "channel",
    ) -> None:
        """일반 질문 / 상태 조회 → 즉시 시작 알림 + 백그라운드 실행."""
        tasks = task_manager.get_tasks_for_channel(channel)

        # 대화 이력 조회
        thread_history: list[dict] = []
        try:
            if is_thread:
                result = await client.conversations_replies(
                    channel=channel,
                    ts=thread_ts,
                    limit=20,
                )
                messages = result.get("messages", [])
                thread_history = [m for m in messages if m["ts"] != event_ts][-20:]
            elif channel_type == "im":
                result = await client.conversations_history(
                    channel=channel,
                    limit=20,
                )
                messages = result.get("messages", [])
                messages.reverse()
                thread_history = [m for m in messages if m["ts"] != event_ts][-20:]
        except Exception:
            logger.warning("대화 이력 조회 실패", exc_info=True)

        task_manager.cleanup_old()

        target_project = projects.get(intent.project) if intent.project else None

        chat_task = await task_manager.create_task(
            intent.project or "general",
            "chat",
            (intent.raw_text or question)[:80],
            "",
            channel,
            thread_ts=thread_ts,
        )

        # 답변 도착 시 삭제할 수 있도록 시작 메시지의 ts를 보관한다.
        # say() 결과가 dict-like가 아닐 수도 있어 방어적으로 처리.
        progress_response = await say(
            f":mag: 질문 처리를 시작합니다. "
            f"(ID: {chat_task.task_id}, 취소: `/stop {chat_task.task_id}`)",
            thread_ts=thread_ts,
        )
        progress_ts: str | None = None
        try:
            if progress_response is not None:
                progress_ts = progress_response.get("ts")
        except Exception:
            progress_ts = None

        bg_task = asyncio.create_task(
            _run_chat_question_and_report(
                app,
                task_manager=task_manager,
                task=chat_task,
                question=question,
                tasks=tasks,
                thread_history=thread_history,
                projects=projects,
                target_project=target_project,
                channel=channel,
                thread_ts=thread_ts,
                semaphore=_chat_semaphore,
                progress_ts=progress_ts,
            )
        )
        _background_tasks.add(bg_task)
        bg_task.add_done_callback(_background_tasks.discard)
        bg_task.add_done_callback(_log_task_exception)

    # ----------------------------------------------------------------
    # 이벤트 핸들러 등록
    # ----------------------------------------------------------------

    @app.event("app_mention")
    async def handle_mention(event, say, client):
        """@봇 멘션 시 통합 메시지 처리"""
        raw_text = event.get("text", "")
        question = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()
        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = event.get("user", "")
        channel = event["channel"]

        await _handle_message(
            question,
            user_id,
            channel,
            thread_ts,
            event["ts"],
            say,
            client,
            is_thread=bool(event.get("thread_ts")),
            channel_type=event.get("channel_type", "channel"),
        )

    @app.event("message")
    async def handle_dm(event, say, client):
        """1:1 DM 메시지 처리"""
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id") or event.get("subtype"):
            return

        raw_text = event.get("text", "")
        question = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()
        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = event.get("user", "")
        channel = event["channel"]

        await _handle_message(
            question,
            user_id,
            channel,
            thread_ts,
            event["ts"],
            say,
            client,
            is_thread=bool(event.get("thread_ts")),
            channel_type="im",
        )

    # ----------------------------------------------------------------
    # 슬래시 커맨드 핸들러
    # ----------------------------------------------------------------
    #
    # Slack 워크스페이스에 등록된 /restart, /stop 슬래시 커맨드는
    # `app_mention`/`message`가 아니라 `slash_commands` 페이로드로
    # 도착하므로 별도 핸들러가 필요하다. 핸들러가 없으면 Slack 클라이언트
    # 입장에서는 입력해도 아무 일도 일어나지 않는다.

    def _slash_say_factory(client, channel: str):
        """슬래시 커맨드 컨텍스트에서 `say(...)`를 흉내내는 콜러블.

        slash 컨텍스트엔 thread가 없으므로 호출자가 `thread_ts=""`로 전달해도
        Slack API가 잘못된 값을 받지 않도록 None/빈 문자열을 필터링한다.
        """

        async def _say(text: str | None = None, **kwargs):
            clean_kwargs = {
                k: v for k, v in kwargs.items() if v is not None and v != ""
            }
            return await client.chat_postMessage(
                channel=channel,
                text=text or "",
                **clean_kwargs,
            )

        return _say

    @app.command("/restart")
    async def handle_slash_restart(ack, body, client):
        """`/restart` — 채널/DM 어디서든 봇 재시작 확인 버튼을 띄운다."""
        await ack()
        user_id = body.get("user_id", "")
        channel = body.get("channel_id", "")
        if not channel:
            return
        intent = Intent(type="admin", command="restart", raw_text="/restart")
        say = _slash_say_factory(client, channel)
        # _handle_admin_intent는 thread_ts: str을 기대 — slash엔 thread가
        # 없으므로 ""를 넘기고, 위 팩토리에서 chat_postMessage 호출 시 걸러낸다.
        await _handle_admin_intent(intent, user_id, channel, "", say)

    @app.command("/stop")
    async def handle_slash_stop(ack, body, client):
        """`/stop [task_id]` — 인자가 있으면 해당 태스크 중단, 없으면 목록."""
        await ack()
        user_id = body.get("user_id", "")
        channel = body.get("channel_id", "")
        if not channel:
            return
        text = (body.get("text") or "").strip()
        task_id_match = _TASK_ID_RE.search(text)
        if task_id_match:
            intent = Intent(
                type="task_control",
                command="stop",
                args=task_id_match.group(1),
                raw_text=f"/stop {text}",
            )
        else:
            intent = Intent(type="task_control", command="list", raw_text="/stop")
        say = _slash_say_factory(client, channel)
        await _handle_task_control(intent, user_id, channel, "", say)

    # ----------------------------------------------------------------
    # 버튼 액션 핸들러 (확인/취소)
    # ----------------------------------------------------------------

    @app.action("confirm_execute")
    async def handle_confirm(ack, body, client):
        """실행 확인 버튼 클릭."""
        await ack()

        action = body["actions"][0]
        data = json.loads(action["value"])
        project_name = data["project"]
        command = data["command"]
        args = data["args"]
        user_id = data["user_id"]
        channel = data["channel"]

        project = projects.get(project_name)
        if not project:
            await client.chat_postMessage(
                channel=channel,
                text=f"프로젝트 `{project_name}`을 찾을 수 없습니다.",
            )
            return

        # 확인 메시지 업데이트
        prompt_display = f"/{command} {args}".strip()
        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=f"*{project_name}* `{prompt_display}` 실행 중... :hourglass_flowing_sand:",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{project_name}* `{prompt_display}` 실행 중... :hourglass_flowing_sand:",
                    },
                }
            ],
        )

        # 태스크 생성 및 백그라운드 실행
        task_manager.cleanup_old()
        thread_ts = body["message"]["ts"]
        task = await task_manager.create_task(
            project_name,
            command,
            args,
            user_id,
            channel,
            thread_ts=thread_ts,
        )

        bg_task = asyncio.create_task(
            _run_and_report(
                app,
                task_manager,
                project,
                task,
                prompt_display,
                _task_semaphore,
            )
        )
        _background_tasks.add(bg_task)
        bg_task.add_done_callback(_background_tasks.discard)
        bg_task.add_done_callback(_log_task_exception)

    @app.action("cancel_execute")
    async def handle_cancel(ack, body, client):
        """실행 취소 버튼 클릭."""
        await ack()

        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="취소했습니다.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "취소했습니다. :no_entry_sign:",
                    },
                }
            ],
        )

    @app.action("confirm_restart")
    async def handle_confirm_restart(ack, body, client):
        """재시작 확인 버튼 클릭."""
        await ack()

        channel_id = body["channel"]["id"]
        msg_ts = body["message"]["ts"]
        bot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # git pull
        try:
            result = subprocess.run(
                ["git", "pull", "origin", "main"],
                cwd=bot_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            pull_output = result.stdout.strip() or result.stderr.strip()
        except Exception as e:
            await client.chat_update(
                channel=channel_id,
                ts=msg_ts,
                text=f":x: git pull 실패: {e}",
                blocks=[],
            )
            return

        if result.returncode != 0:
            await client.chat_update(
                channel=channel_id,
                ts=msg_ts,
                text=f":x: git pull 실패 (exit {result.returncode}):\n```\n{pull_output}\n```",
                blocks=[],
            )
            return

        await client.chat_update(
            channel=channel_id,
            ts=msg_ts,
            text=f":arrows_counterclockwise: 업데이트 완료, 재시작합니다.\n```\n{pull_output}\n```",
            blocks=[],
        )

        # 메시지 전송 완료 후 프로세스 교체
        await asyncio.sleep(0.5)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    @app.action("cancel_restart")
    async def handle_cancel_restart(ack, body, client):
        """재시작 취소 버튼 클릭."""
        await ack()

        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="재시작을 취소했습니다. :no_entry_sign:",
            blocks=[],
        )

    @app.action("confirm_install_claude")
    async def handle_confirm_install_claude(ack, body, client):
        """Claude CLI 설치 확인 버튼 클릭."""
        await ack()

        channel_id = body["channel"]["id"]
        msg_ts = body["message"]["ts"]

        await client.chat_update(
            channel=channel_id,
            ts=msg_ts,
            text=":hourglass_flowing_sand: Claude CLI 설치 중...",
            blocks=[],
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                "npm",
                "install",
                "-g",
                "@anthropic-ai/claude-code",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=180,
            )
            output = (stdout or stderr or b"").decode(errors="replace").strip()
            if proc.returncode == 0:
                await client.chat_update(
                    channel=channel_id,
                    ts=msg_ts,
                    text=f":white_check_mark: Claude CLI 설치 완료!\n```\n{output[:3000]}\n```",
                    blocks=[],
                )
            else:
                await client.chat_update(
                    channel=channel_id,
                    ts=msg_ts,
                    text=f":x: Claude CLI 설치 실패 (exit {proc.returncode}):\n```\n{output[:3000]}\n```",
                    blocks=[],
                )
        except FileNotFoundError:
            await client.chat_update(
                channel=channel_id,
                ts=msg_ts,
                text=":x: `npm`이 설치되어 있지 않습니다. 먼저 Node.js를 설치해주세요.",
                blocks=[],
            )
        except asyncio.TimeoutError:
            await client.chat_update(
                channel=channel_id,
                ts=msg_ts,
                text=":x: Claude CLI 설치 시간이 초과되었습니다 (180초).",
                blocks=[],
            )
        except Exception as e:
            await client.chat_update(
                channel=channel_id,
                ts=msg_ts,
                text=f":x: Claude CLI 설치 중 에러: {e}",
                blocks=[],
            )

    @app.action("cancel_install_claude")
    async def handle_cancel_install_claude(ack, body, client):
        """Claude CLI 설치 취소 버튼 클릭."""
        await ack()

        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="Claude CLI 설치를 취소했습니다. :no_entry_sign:",
            blocks=[],
        )

    @app.action("confirm_auth_login")
    async def handle_confirm_auth_login(ack, body, client):
        """Claude CLI 인증 확인 버튼 클릭.

        `claude auth login`은 인증 URL을 출력한 뒤 stdin으로 코드 입력을 기다린다.
        Slack 메시지로 받은 코드를 stdin에 그대로 적어 인증을 마무리한다.
        """
        await ack()

        channel_id = body["channel"]["id"]
        msg_ts = body["message"]["ts"]
        user_id = body.get("user", {}).get("id", "") or body.get("user_id", "")

        # 동일 채널/메시지의 기존 세션이 살아 있으면 정리
        session_key = f"{channel_id}:{msg_ts}"
        old = _pending_auth_sessions.pop(session_key, None)
        if old and old.proc.returncode is None:
            try:
                old.proc.kill()
            except ProcessLookupError:
                pass

        await client.chat_update(
            channel=channel_id,
            ts=msg_ts,
            text=":hourglass_flowing_sand: Claude CLI 인증 프로세스를 시작합니다...",
            blocks=[],
        )

        URL_WAIT_TIMEOUT = 60   # URL이 출력될 때까지의 대기 (초)
        CODE_WAIT_TIMEOUT = 900  # URL 전달 후 사용자가 코드를 붙여넣을 때까지 (15분)
        FINALIZE_TIMEOUT = 60   # stdin 전달 후 프로세스 종료 대기 (초)

        proc: asyncio.subprocess.Process | None = None
        url_event = asyncio.Event()
        collected_output: list[str] = []
        url_re = re.compile(r"https?://\S+")
        first_url: list[str] = []

        async def _read_stream(stream: asyncio.StreamReader) -> None:
            async for line_bytes in stream:
                line = line_bytes.decode(errors="replace").strip()
                if not line:
                    continue
                collected_output.append(line)
                logger.info("claude auth login: %s", line)
                if not url_event.is_set():
                    url_match = url_re.search(line)
                    if url_match:
                        first_url.append(url_match.group(0))
                        url_event.set()

        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "claude",
                    "auth",
                    "login",
                    "--claudeai",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=msg_ts,
                    text=":x: `claude` CLI가 설치되어 있지 않습니다. 먼저 Claude CLI를 설치해주세요.",
                )
                return

            stdout_task = asyncio.create_task(_read_stream(proc.stdout))
            stderr_task = asyncio.create_task(_read_stream(proc.stderr))

            # 1) URL 출력 대기
            try:
                await asyncio.wait_for(url_event.wait(), timeout=URL_WAIT_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                output = "\n".join(collected_output[-20:])
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=msg_ts,
                    text=(
                        f":x: 인증 URL이 출력되지 않았습니다 ({URL_WAIT_TIMEOUT}초). "
                        f"`claude` CLI 설정을 확인해주세요.\n```\n{output[:2000]}\n```"
                    ),
                )
                return

            url = first_url[0]
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=msg_ts,
                text=(
                    ":link: 아래 URL을 브라우저에서 열어 인증을 진행하세요:\n"
                    f"```\n{url}\n```\n"
                    "인증 후 발급된 *코드를 이 스레드(또는 DM)에 그대로 붙여넣어* 주세요.\n"
                    f"_{CODE_WAIT_TIMEOUT // 60}분 안에 코드를 입력하지 않으면 취소됩니다._"
                ),
            )

            # 2) Slack에서 코드 입력 대기 (메시지 핸들러가 future를 resolve)
            loop = asyncio.get_running_loop()
            code_future: asyncio.Future = loop.create_future()
            session = AuthSession(
                proc=proc,
                user_id=user_id,
                channel=channel_id,
                thread_ts=msg_ts,
                msg_ts=msg_ts,
                code_future=code_future,
                created_at=time.time(),
            )
            _pending_auth_sessions[session_key] = session

            try:
                code = await asyncio.wait_for(code_future, timeout=CODE_WAIT_TIMEOUT)
            except asyncio.TimeoutError:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=msg_ts,
                    text=f":x: 인증 코드 입력 시간이 초과되었습니다 ({CODE_WAIT_TIMEOUT}초). 다시 시도해주세요.",
                )
                return
            except asyncio.CancelledError:
                # 사용자가 "취소"를 입력한 경로 — 안내 메시지는 메시지 핸들러에서 이미 보냈음
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()
                return
            finally:
                _pending_auth_sessions.pop(session_key, None)

            # 3) stdin에 코드 전달 후 프로세스 종료 대기
            try:
                proc.stdin.write(code.encode() + b"\n")
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                # 프로세스가 이미 종료된 경우
                pass

            try:
                await asyncio.wait_for(proc.wait(), timeout=FINALIZE_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=msg_ts,
                    text=f":x: 인증 완료 처리 중 시간이 초과되었습니다 ({FINALIZE_TIMEOUT}초).",
                )
                return

            # stdout/stderr 수집 마무리 (best-effort)
            for t in (stdout_task, stderr_task):
                try:
                    await asyncio.wait_for(t, timeout=2)
                except (asyncio.TimeoutError, Exception):
                    pass

            if proc.returncode == 0:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=msg_ts,
                    text=":white_check_mark: Claude CLI 인증이 완료되었습니다!",
                )
            else:
                output = "\n".join(collected_output[-15:])
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=msg_ts,
                    text=f":x: Claude CLI 인증 실패 (exit {proc.returncode}):\n```\n{output[:2000]}\n```",
                )

        except Exception as e:
            logger.exception("claude auth login 처리 중 예외")
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=msg_ts,
                text=f":x: Claude CLI 인증 중 에러: {e}",
            )
        finally:
            _pending_auth_sessions.pop(session_key, None)

    @app.action("cancel_auth_login")
    async def handle_cancel_auth_login(ack, body, client):
        """Claude CLI 인증 취소 버튼 클릭."""
        await ack()

        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="Claude CLI 인증을 취소했습니다. :no_entry_sign:",
            blocks=[],
        )


# ----------------------------------------------------------------
# 백그라운드 실행 + 결과 보고
# ----------------------------------------------------------------


async def _run_chat_question_and_report(
    app: AsyncApp,
    task_manager: TaskManager,
    task,
    question: str,
    tasks: list,
    thread_history: list[dict],
    projects: dict[str, ProjectConfig],
    target_project: ProjectConfig | None,
    channel: str,
    thread_ts: str,
    semaphore: asyncio.Semaphore,
    progress_ts: str | None = None,
) -> None:
    """answer_question을 백그라운드에서 실행하고 결과를 스레드에 보고한다.

    progress_ts가 주어지면 결과/취소/에러 메시지를 보낸 직후 해당 시작 알림 메시지를
    삭제한다. 시작 알림이 답변 옆에 남아 있으면 채널이 지저분해 보이기 때문이다.
    삭제는 best-effort — 실패해도 사용자 흐름에는 영향이 없어야 한다.
    """

    async def _delete_progress() -> None:
        if not progress_ts:
            return
        try:
            await app.client.chat_delete(channel=channel, ts=progress_ts)
        except Exception:
            logger.warning("진행 메시지 삭제 실패 (ts=%s)", progress_ts, exc_info=True)

    try:
        async with semaphore:
            answer = await answer_question(
                question,
                tasks,
                thread_history,
                projects=projects,
                target_project=target_project,
                on_progress=None,
                task=task,
            )

        if task.status == "stopped":
            await app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=":octagonal_sign: 질문 처리가 취소되었습니다.",
            )
            await _delete_progress()
            return

        task_manager.complete_task(task.task_id, True)

        if answer:
            answer, was_redacted = redact_output(answer)
            # Slack chat.postMessage의 text 필드는 최대 4000자
            if len(answer) > 3900:
                answer = answer[:3900] + "\n\n... (truncated)"
            if was_redacted:
                answer += (
                    "\n\n:lock: 일부 민감 정보가 보안 정책에 의해 마스킹되었습니다."
                )

        await app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=answer or "_출력 없음_",
        )
        await _delete_progress()
    except Exception:
        logger.exception("질문 답변 처리 중 에러")
        task_manager.complete_task(task.task_id, False)
        await app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":warning: 질문 처리 중 오류가 발생했습니다.",
        )
        await _delete_progress()


async def _run_shell_and_report(
    app: AsyncApp,
    task_manager: TaskManager,
    project: ProjectConfig,
    task,
    log_path: str,
    semaphore: asyncio.Semaphore,
) -> None:
    """셸 명령을 직접 실행하고 완료 시 결과를 보고한다."""
    try:
        async with semaphore:
            with open(log_path, "w") as log_file:
                env = make_safe_env()
                # systemd 환경에서 누락되는 일반적인 바이너리 경로 보충
                home = env.get("HOME", os.path.expanduser("~"))
                extra_paths = [
                    f"{home}/.local/bin",
                    f"{home}/.cargo/bin",
                    "/usr/local/bin",
                ]
                current_path = env.get("PATH", "")
                for p in extra_paths:
                    if p not in current_path:
                        current_path = f"{p}:{current_path}"
                env["PATH"] = current_path

                proc = await asyncio.create_subprocess_shell(
                    task.args,
                    cwd=project.path,
                    stdout=log_file,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                )
                task.process = proc
                await proc.wait()

        success = proc.returncode == 0
        task_manager.complete_task(task.task_id, success)

        status = "완료" if success else "실패"
        emoji = ":white_check_mark:" if success else ":x:"

        # 로그 마지막 20줄 읽기
        tail = ""
        try:
            with open(log_path) as f:
                lines = f.readlines()
                tail = "".join(lines[-20:])
        except Exception:
            pass

        output = tail[:3900] if tail else "_출력 없음_"

        msg_kwargs: dict = dict(
            channel=task.channel,
            text=(
                f"{emoji} *{task.project_name}* `{task.args}` {status} "
                f"(ID: {task.task_id}, {task.elapsed_display})\n"
                f"로그: `{log_path}`\n"
                f"```\n{output}\n```"
            ),
        )
        if task.thread_ts:
            msg_kwargs["thread_ts"] = task.thread_ts
        await app.client.chat_postMessage(**msg_kwargs)

    except Exception:
        logger.exception("셸 명령 실행 중 에러 발생")
        task_manager.complete_task(task.task_id, False)
        err_kwargs: dict = dict(
            channel=task.channel,
            text=f":warning: `{task.args}` 실행 중 에러가 발생했습니다. 로그: `{log_path}`",
        )
        if task.thread_ts:
            err_kwargs["thread_ts"] = task.thread_ts
        await app.client.chat_postMessage(**err_kwargs)


async def _run_and_report(
    app: AsyncApp,
    task_manager: TaskManager,
    project: ProjectConfig,
    task,
    prompt_display: str,
    semaphore: asyncio.Semaphore,
) -> None:
    try:
        async with semaphore:
            result = await run_claude(project, task.command, task.args, task)
        task_manager.complete_task(task.task_id, result.success)

        status = "완료" if result.success else "실패"
        emoji = ":white_check_mark:" if result.success else ":x:"

        output = result.output
        if output:
            output, was_redacted = redact_output(output)
            if was_redacted:
                output += (
                    "\n\n:lock: 일부 민감 정보가 보안 정책에 의해 마스킹되었습니다."
                )

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{emoji} *{task.project_name}* `{prompt_display}` {status} "
                        f"(ID: {task.task_id}, {task.elapsed_display})\n"
                        f"실행자: <@{task.user}>"
                    ),
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"```\n{output}\n```" if output else "_출력 없음_",
                },
            },
        ]

        msg_kwargs: dict = dict(
            channel=task.channel, blocks=blocks, text=f"{status}: {prompt_display}"
        )
        if task.thread_ts:
            msg_kwargs["thread_ts"] = task.thread_ts
        await app.client.chat_postMessage(**msg_kwargs)

    except Exception:
        logger.exception("Claude 실행 중 에러 발생")
        task_manager.complete_task(task.task_id, False)
        err_kwargs: dict = dict(
            channel=task.channel,
            text=(
                f":warning: *{task.project_name}* `{prompt_display}` 실행 중 에러가 발생했습니다. "
                f"로그를 확인해주세요."
            ),
        )
        if task.thread_ts:
            err_kwargs["thread_ts"] = task.thread_ts
        await app.client.chat_postMessage(**err_kwargs)


async def _run_db_query_export_and_report(
    app: AsyncApp,
    task_manager: TaskManager,
    task,
    question: str,
    channel: str,
    thread_ts: str,
    user_id: str,
    db_project: ProjectConfig,
    wiki_path: str | None,
    semaphore: asyncio.Semaphore,
) -> None:
    """DB 조회 → CSV → Excel → Slack 파일 업로드."""
    try:
        async with semaphore:
            result = await run_db_query_export(question, db_project, wiki_path, task=task)

        if task.status == "stopped":
            await app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=":octagonal_sign: DB 조회가 취소되었습니다.",
            )
            return

        if result.error or result.excel_path is None:
            # 파일 생성 실패 시 텍스트로 폴백
            error_msg = result.error or "파일 생성에 실패했습니다."
            if result.summary:
                text = f"{result.summary}\n\n:warning: {error_msg}"
            else:
                text = f":warning: {error_msg}"
            task_manager.complete_task(task.task_id, False)
            await app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=text,
            )
            return

        # 요약 텍스트 마스킹
        summary = result.summary or "DB 조회 결과"
        summary, was_redacted = redact_output(summary)
        if was_redacted:
            summary += "\n:lock: 일부 민감 정보가 마스킹되었습니다."

        # Excel 파일 업로드
        try:
            await app.client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=str(result.excel_path),
                filename="query_result.xlsx",
                title="DB 조회 결과",
                initial_comment=summary,
            )
            task_manager.complete_task(task.task_id, True)
        finally:
            # 임시 파일 정리
            result.excel_path.unlink(missing_ok=True)

    except Exception:
        logger.exception("DB 조회 엑셀 내보내기 중 에러")
        task_manager.complete_task(task.task_id, False)
        await app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":warning: DB 조회 결과 파일 생성 중 에러가 발생했습니다.",
        )


async def _run_db_query_and_report(
    app: AsyncApp,
    task_manager: TaskManager,
    task,
    question: str,
    channel: str,
    thread_ts: str,
    user_id: str,
    db_project: ProjectConfig,
    wiki_path: str | None,
    semaphore: asyncio.Semaphore,
) -> None:
    try:
        async with semaphore:
            answer = await run_db_query(question, db_project, wiki_path, task=task)

        if task.status == "stopped":
            await app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=":octagonal_sign: DB 조회가 취소되었습니다.",
            )
            return

        if answer:
            answer, was_redacted = redact_output(answer)
            if was_redacted:
                answer += (
                    "\n\n:lock: 일부 민감 정보가 보안 정책에 의해 마스킹되었습니다."
                )

        task_manager.complete_task(task.task_id, True)
        await app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=answer or "_출력 없음_",
        )
    except Exception:
        logger.exception("DB 조회 중 에러 발생")
        task_manager.complete_task(task.task_id, False)
        await app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":warning: DB 조회 중 에러가 발생했습니다. 로그를 확인해주세요.",
        )
