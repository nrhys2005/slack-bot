from __future__ import annotations

import asyncio
import json
import logging
import re

from slack_bolt.async_app import AsyncApp

from slack_bot.chat import answer_question
from slack_bot.config import ProjectConfig, load_projects
from slack_bot.db_query import run_db_query
from slack_bot.intent import Intent, parse_intent
from slack_bot.runner import run_claude
from slack_bot.security import (
    RateLimiter,
    check_auth,
    log_command,
    redact_output,
)
from slack_bot.task_manager import TaskManager

logger = logging.getLogger(__name__)

MAX_CONCURRENT_CHAT = 3
MAX_CONCURRENT_TASK = 3


def _log_task_exception(t: asyncio.Task) -> None:
    """백그라운드 태스크의 미처리 예외를 로깅."""
    if t.cancelled():
        return
    exc = t.exception()
    if exc is not None:
        logger.error("백그라운드 태스크 예외: %s", exc, exc_info=exc)


_AUTH_DENIED = ":lock: 이 명령어를 사용할 권한이 없습니다."
_RATE_LIMITED = ":hourglass: 요청이 너무 많습니다. 잠시 후 다시 시도해주세요."


def register_handlers(app: AsyncApp, task_manager: TaskManager) -> None:
    _chat_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHAT)
    _task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASK)
    _background_tasks: set[asyncio.Task] = set()

    # Rate limiters
    _task_limiter = RateLimiter(max_calls=3, window_seconds=600)
    _chat_limiter = RateLimiter(max_calls=10, window_seconds=300)

    app_config = load_projects()
    projects = app_config.projects
    security = app_config.security

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
        # 인증 + rate limit
        authorized = check_auth(user_id, "chat", security.allowed_users)
        log_command(user_id, "", channel, "chat", question[:80], authorized)
        if not authorized:
            await say(_AUTH_DENIED, thread_ts=thread_ts)
            return
        if not _chat_limiter.check(user_id):
            await say(_RATE_LIMITED, thread_ts=thread_ts)
            return

        if not question:
            await say(
                "무엇을 도와드릴까요? 프로젝트 명령 실행, 상태 확인, 질문 등을 할 수 있습니다.",
                thread_ts=thread_ts,
            )
            return

        # 즉시 리액션
        try:
            await client.reactions_add(
                channel=channel, timestamp=event_ts, name="eyes"
            )
        except Exception:
            logger.warning("리액션 추가 실패", exc_info=True)

        # 인텐트 파싱
        intent = parse_intent(question, projects)

        try:
            if intent.type == "command":
                await _handle_command_intent(
                    intent, user_id, channel, thread_ts, say, client
                )
            elif intent.type == "task_control":
                await _handle_task_control(
                    intent, user_id, channel, thread_ts, say
                )
            elif intent.type == "db_query":
                await _handle_db_query_intent(
                    intent, user_id, channel, thread_ts, event_ts, say, client
                )
            elif intent.type in ("status", "question"):
                await _handle_question_intent(
                    intent, question, channel, thread_ts, event_ts, say, client,
                    is_thread=is_thread, channel_type=channel_type,
                )
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

    async def _handle_command_intent(
        intent: Intent,
        user_id: str,
        channel: str,
        thread_ts: str,
        say,
        client,
    ) -> None:
        """명령 실행 요청 → 확인 메시지 → 버튼 클릭 시 실행."""
        # 명령 실행은 admin 권한 필요
        if not check_auth(user_id, "admin", security.allowed_users):
            await say(_AUTH_DENIED, thread_ts=thread_ts)
            return
        if not _task_limiter.check(user_id):
            await say(_RATE_LIMITED, thread_ts=thread_ts)
            return

        project = projects.get(intent.project)
        if not project:
            project_list = ", ".join(
                f"`{n}`" for n, p in projects.items() if p.commands
            )
            await say(
                f"프로젝트를 식별하지 못했습니다. 등록된 프로젝트: {project_list}",
                thread_ts=thread_ts,
            )
            return

        if intent.command not in project.commands:
            cmd_list = ", ".join(f"`{c}`" for c in project.commands)
            await say(
                f"`{project.name}`에서 허용된 명령어: {cmd_list}",
                thread_ts=thread_ts,
            )
            return

        # 확인 메시지 (Block Kit + 버튼)
        prompt_display = f"/{intent.command} {intent.args}".strip()
        # Block Kit value 필드는 2000자 제한 — args를 truncate
        safe_args = intent.args[:1500] if len(intent.args) > 1500 else intent.args
        action_data = json.dumps({
            "project": intent.project,
            "command": intent.command,
            "args": safe_args,
            "user_id": user_id,
            "channel": channel,
        })

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{intent.project}* 에서 `{prompt_display}` 을 실행할까요?"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "실행"},
                        "style": "primary",
                        "action_id": "confirm_execute",
                        "value": action_data,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "취소"},
                        "action_id": "cancel_execute",
                        "value": action_data,
                    },
                ],
            },
        ]

        await say(blocks=blocks, text=f"{intent.project}에서 {prompt_display} 실행 확인", thread_ts=thread_ts)

    async def _handle_task_control(
        intent: Intent,
        user_id: str,
        channel: str,
        thread_ts: str,
        say,
    ) -> None:
        """태스크 목록 조회 또는 중단."""
        if intent.command == "stop" and intent.args:
            # 중단은 admin 권한 필요
            if not check_auth(user_id, "admin", security.allowed_users):
                await say(_AUTH_DENIED, thread_ts=thread_ts)
                return
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
        if not check_auth(user_id, "db", security.allowed_users):
            await say(_AUTH_DENIED, thread_ts=thread_ts)
            return

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

        await say(f":mag: `{intent.raw_text}` 조회 중...", thread_ts=thread_ts)

        bg_task = asyncio.create_task(
            _run_db_query_and_report(
                app,
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
        """일반 질문 / 상태 조회 처리."""
        tasks = task_manager.get_tasks_for_channel(channel)

        # 대화 이력 조회
        thread_history: list[dict] = []
        try:
            if is_thread:
                # 스레드 내 메시지 — replies로 이력 조회
                result = await client.conversations_replies(
                    channel=channel,
                    ts=thread_ts,
                    limit=20,
                )
                messages = result.get("messages", [])
                thread_history = [m for m in messages if m["ts"] != event_ts][-20:]
            elif channel_type == "im":
                # DM 최상위 메시지 — 최근 대화 이력 조회
                result = await client.conversations_history(
                    channel=channel,
                    limit=20,
                )
                messages = result.get("messages", [])
                messages.reverse()  # 시간순 정렬
                thread_history = [m for m in messages if m["ts"] != event_ts][-20:]
        except Exception:
            logger.warning("대화 이력 조회 실패", exc_info=True)

        task_manager.cleanup_old()

        # target_project 결정
        target_project = projects.get(intent.project) if intent.project else None

        try:
            async with _chat_semaphore:
                answer = await answer_question(
                    question,
                    tasks,
                    thread_history,
                    projects=projects,
                    target_project=target_project,
                )
        except Exception:
            logger.exception("질문 답변 처리 중 에러")
            answer = ":warning: 질문 처리 중 오류가 발생했습니다."

        # 출력 마스킹
        answer, was_redacted = redact_output(answer)
        if was_redacted:
            answer += "\n\n:lock: 일부 민감 정보가 보안 정책에 의해 마스킹되었습니다."

        await say(answer, thread_ts=thread_ts)

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
            question, user_id, channel, thread_ts, event["ts"], say, client,
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
            question, user_id, channel, thread_ts, event["ts"], say, client,
            is_thread=bool(event.get("thread_ts")),
            channel_type="im",
        )

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

        # 클릭자 vs 요청자 검증
        clicker_id = body.get("user", {}).get("id", "")
        if clicker_id != user_id:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=clicker_id,
                text="요청자만 실행 버튼을 누를 수 있습니다.",
            )
            return

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
        user_name = body.get("user", {}).get("username", user_id)
        task = await task_manager.create_task(
            project_name, command, args, user_name, channel
        )

        log_command(user_id, user_name, channel, "execute", f"{project_name} {command} {args}", True)

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

        action = body["actions"][0]
        data = json.loads(action["value"])
        clicker_id = body.get("user", {}).get("id", "")
        if clicker_id != data.get("user_id", ""):
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=clicker_id,
                text="요청자만 취소 버튼을 누를 수 있습니다.",
            )
            return

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


# ----------------------------------------------------------------
# 백그라운드 실행 + 결과 보고
# ----------------------------------------------------------------


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
                output += "\n\n:lock: 일부 민감 정보가 보안 정책에 의해 마스킹되었습니다."

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
                    "text": f"```\n{output}\n```"
                    if output
                    else "_출력 없음_",
                },
            },
        ]

        await app.client.chat_postMessage(
            channel=task.channel, blocks=blocks, text=f"{status}: {prompt_display}"
        )

    except Exception:
        logger.exception("Claude 실행 중 에러 발생")
        task_manager.complete_task(task.task_id, False)
        await app.client.chat_postMessage(
            channel=task.channel,
            text=(
                f":warning: *{task.project_name}* `{prompt_display}` 실행 중 에러가 발생했습니다. "
                f"로그를 확인해주세요."
            ),
        )


async def _run_db_query_and_report(
    app: AsyncApp,
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
            answer = await run_db_query(question, db_project, wiki_path)
        if answer:
            answer, was_redacted = redact_output(answer)
            if was_redacted:
                answer += "\n\n:lock: 일부 민감 정보가 보안 정책에 의해 마스킹되었습니다."

        await app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=answer or "_출력 없음_",
        )
    except Exception:
        logger.exception("DB 조회 중 에러 발생")
        await app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":warning: DB 조회 중 에러가 발생했습니다. 로그를 확인해주세요.",
        )
