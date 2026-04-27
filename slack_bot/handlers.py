from __future__ import annotations

import asyncio
import logging
import re

from slack_bolt.async_app import AsyncApp

from slack_bot.chat import answer_question
from slack_bot.config import load_projects
from slack_bot.db_query import run_db_query
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
    _task_limiter = RateLimiter(max_calls=3, window_seconds=600)    # /dev, /claude
    _db_limiter = RateLimiter(max_calls=5, window_seconds=300)      # /db
    _chat_limiter = RateLimiter(max_calls=10, window_seconds=300)   # @mention

    app_config = load_projects()
    projects = app_config.projects
    security = app_config.security

    wiki_project = next((p for p in projects.values() if p.wiki), None)
    wiki_path = wiki_project.path if wiki_project else None
    db_backend_project = next((p for p in projects.values() if p.db_backend), None)
    db_backend_path = db_backend_project.path if db_backend_project else None

    @app.command("/dev")
    async def handle_dev_command(ack, command, respond):
        """
        /dev <project> <issue> — harness 단축 명령어

        Slack은 비대화형이라 harness 파이프라인이 승인 프롬프트에서 멈추면
        안 되고, 서브에이전트/Jira/Git 도구 권한이 필요하다.
        따라서 항상 `--auto`를 강제한다.

        예시:
          /dev moment-some MOM-43
        """
        await ack()

        user_id = command.get("user_id", "")
        user_name = command.get("user_name", "unknown")
        channel = command["channel_id"]
        text = (command.get("text") or "").strip()

        # 감사 로그 + 인증 + rate limit
        authorized = check_auth(user_id, "dev", security.allowed_users)
        log_command(user_id, user_name, channel, "/dev", text, authorized)
        if not authorized:
            await respond(_AUTH_DENIED)
            return
        if not _task_limiter.check(user_id):
            await respond(_RATE_LIMITED)
            return

        parts = text.split(None, 1)

        if len(parts) < 2:
            harness_projects = [
                f"`{p}`" for p, cfg in projects.items() if "harness" in cfg.commands
            ]
            await respond(
                f"사용법: `/dev <project> <issue>`\n"
                f"harness 가능한 프로젝트: {', '.join(harness_projects) or '없음'}"
            )
            return

        project_name = parts[0]
        args = parts[1]

        if "--auto" not in args.split():
            args = f"{args} --auto"

        # 프로젝트 검증
        project = projects.get(project_name)
        if project is None:
            project_list = ", ".join(f"`{p}`" for p in projects)
            await respond(
                f"알 수 없는 프로젝트: `{project_name}`\n등록된 프로젝트: {project_list}"
            )
            return

        if "harness" not in project.commands:
            await respond(
                f"`{project_name}` 프로젝트에 harness 명령어가 등록되어 있지 않습니다."
            )
            return

        # 태스크 정리 & 생성
        task_manager.cleanup_old()
        user = user_name
        task = await task_manager.create_task(
            project_name, "harness", args, user, channel
        )
        prompt_display = f"/harness {args}".strip()

        await respond(
            f"*{project_name}* 프로젝트에서 `{prompt_display}` 실행 중... (ID: {task.task_id})\n"
            f"완료되면 이 채널에 결과를 알려드립니다. "
            f"`@bot 지금 어디까지 됐어?` 로 진행상황을 확인할 수 있습니다."
        )

        slash_command = f"/dev {text}"
        bg_task = asyncio.create_task(
            _run_and_report(
                app,
                task_manager,
                project,
                task,
                prompt_display,
                slash_command,
                _task_semaphore,
            )
        )
        _background_tasks.add(bg_task)
        bg_task.add_done_callback(_background_tasks.discard)
        bg_task.add_done_callback(_log_task_exception)

    @app.command("/claude")
    async def handle_claude_command(ack, command, respond):
        """
        /claude <project> <command> [args]

        예시:
          /claude moment-some plan MOM-43
          /claude moment-some develop MOM-43 --auto
        """
        await ack()

        user_id = command.get("user_id", "")
        user_name = command.get("user_name", "unknown")
        channel = command["channel_id"]
        text = (command.get("text") or "").strip()

        # 감사 로그 + 인증 + rate limit
        authorized = check_auth(user_id, "admin", security.allowed_users)
        log_command(user_id, user_name, channel, "/claude", text, authorized)
        if not authorized:
            await respond(_AUTH_DENIED)
            return
        if not _task_limiter.check(user_id):
            await respond(_RATE_LIMITED)
            return

        parts = text.split(None, 2)

        # 입력 검증: 프로젝트명 + 명령어 최소 필요
        if len(parts) < 2:
            project_list = ", ".join(
                f"`{p}`" for p, cfg in projects.items() if cfg.commands
            )
            await respond(
                f"사용법: `/claude <project> <command> [args]`\n"
                f"등록된 프로젝트: {project_list}"
            )
            return

        project_name = parts[0]
        cmd = parts[1]
        args = parts[2] if len(parts) > 2 else ""

        # 프로젝트 검증
        project = projects.get(project_name)
        if project is None:
            project_list = ", ".join(f"`{p}`" for p in projects)
            await respond(
                f"알 수 없는 프로젝트: `{project_name}`\n등록된 프로젝트: {project_list}"
            )
            return

        # 명령어 검증
        if cmd not in project.commands:
            cmd_list = ", ".join(f"`{c}`" for c in project.commands)
            await respond(f"`{project_name}`에서 허용된 명령어: {cmd_list}")
            return

        # 태스크 정리 & 생성
        task_manager.cleanup_old()
        user = user_name
        task = await task_manager.create_task(project_name, cmd, args, user, channel)
        prompt_display = f"/{cmd} {args}".strip()

        await respond(
            f"*{project_name}* 프로젝트에서 `{prompt_display}` 실행 중... (ID: {task.task_id})\n"
            f"완료되면 이 채널에 결과를 알려드립니다. "
            f"`@bot 지금 어디까지 됐어?` 로 진행상황을 확인할 수 있습니다."
        )

        slash_command = f"/claude {text}"
        bg_task = asyncio.create_task(
            _run_and_report(
                app,
                task_manager,
                project,
                task,
                prompt_display,
                slash_command,
                _task_semaphore,
            )
        )
        _background_tasks.add(bg_task)
        bg_task.add_done_callback(_background_tasks.discard)
        bg_task.add_done_callback(_log_task_exception)

    @app.command("/projects")
    async def handle_projects_command(ack, respond):
        """/projects — 등록된 프로젝트 목록 조회"""
        await ack()
        lines = []
        for name, cfg in projects.items():
            if not cfg.commands:
                continue
            cmds = ", ".join(f"`{c}`" for c in cfg.commands)
            lines.append(f"*{name}*: {cmds}")
        await respond("등록된 프로젝트:\n" + "\n".join(lines))

    @app.command("/stop")
    async def handle_stop_command(ack, command, respond):
        """/stop <task_id> — 실행 중인 태스크 중단"""
        await ack()

        user_id = command.get("user_id", "")
        user_name = command.get("user_name", "unknown")
        channel = command["channel_id"]
        task_id = (command.get("text") or "").strip()

        # 목록 조회는 누구나, 실행 중단은 admin만
        if task_id:
            authorized = check_auth(user_id, "admin", security.allowed_users)
            log_command(user_id, user_name, channel, "/stop", task_id, authorized)
            if not authorized:
                await respond(_AUTH_DENIED)
                return

        if not task_id:
            running = task_manager.get_running_tasks()
            if not running:
                await respond("실행 중인 태스크가 없습니다.")
                return
            lines = []
            for t in running:
                lines.append(
                    f"*{t.task_id}* | {t.project_name} `/{t.command} {t.args}` | {t.elapsed_display} 경과"
                )
            await respond(
                "중단할 태스크 ID를 입력해주세요: `/stop <ID>`\n\n"
                "실행 중인 태스크:\n" + "\n".join(lines)
            )
            return

        if task_manager.stop_task(task_id):
            await respond(f"태스크 {task_id} 중단됨 :octagonal_sign:")
        else:
            await respond(f"태스크 `{task_id}`를 찾을 수 없거나 이미 종료되었습니다.")

    @app.command("/db")
    async def handle_db_command(ack, command, respond):
        """
        /db <자연어 질문> — db_backend 프로젝트(ra_backend) 모델을 참고해 DB를 psql로 조회

        예시:
          /db 지난주 신규 가입한 유저 수
          /db ra_v2 스키마 테이블 목록 보여줘
        """
        await ack()

        user_id = command.get("user_id", "")
        user_name = command.get("user_name", "unknown")
        channel = command["channel_id"]
        question = (command.get("text") or "").strip()

        # 감사 로그 + 인증 + rate limit
        authorized = check_auth(user_id, "db", security.allowed_users)
        log_command(user_id, user_name, channel, "/db", question, authorized)
        if not authorized:
            await respond(_AUTH_DENIED)
            return
        if not _db_limiter.check(user_id):
            await respond(_RATE_LIMITED)
            return

        if not question:
            await respond(
                "사용법: `/db <자연어 질문>`\n"
                "예시: `/db 지난주 가입한 유저 수`, `/db 최근 등록된 건축인허가 10건`"
            )
            return

        if db_backend_path is None:
            await respond(
                "`projects.yaml` 에 `db_backend: true` 로 표시된 프로젝트가 없습니다. "
                "DB 모델·자격증명 참조를 위해 ra_backend 같은 FastAPI 프로젝트를 "
                "`db_backend: true` 옵션과 함께 등록해주세요."
            )
            return

        task_manager.cleanup_old()
        user = user_name
        slash_command = f"/db {question}"

        await respond(f":mag: `{question}` 조회 중...")

        bg_task = asyncio.create_task(
            _run_db_query_and_report(
                app,
                question=question,
                channel=channel,
                user=user,
                db_backend_path=db_backend_path,
                wiki_path=wiki_path,
                slash_command=slash_command,
                semaphore=_chat_semaphore,
            )
        )
        _background_tasks.add(bg_task)
        bg_task.add_done_callback(_background_tasks.discard)
        bg_task.add_done_callback(_log_task_exception)

    @app.event("app_mention")
    async def handle_mention(event, say, client):
        """@봇 멘션 시 Claude API로 질문에 답변"""
        raw_text = event.get("text", "")
        # @멘션 부분 제거
        question = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()

        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = event.get("user", "")
        channel = event["channel"]

        # 감사 로그 + 인증 + rate limit
        authorized = check_auth(user_id, "chat", security.allowed_users)
        log_command(user_id, "", channel, "@mention", question[:80], authorized)
        if not authorized:
            await say(_AUTH_DENIED, thread_ts=thread_ts)
            return
        if not _chat_limiter.check(user_id):
            await say(_RATE_LIMITED, thread_ts=thread_ts)
            return

        if not question:
            await say(
                "무엇이 궁금하신가요? 태스크 진행상황이나 위키 관련 질문을 해주세요.",
                thread_ts=thread_ts,
            )
            return

        # 즉시 리액션으로 "읽었다" 신호
        try:
            await client.reactions_add(
                channel=channel, timestamp=event["ts"], name="eyes"
            )
        except Exception:
            logger.warning("리액션 추가 실패", exc_info=True)

        tasks = task_manager.get_tasks_for_channel(channel)

        # 스레드 대화 이력 조회 (현재 메시지 제외)
        thread_history: list[dict] = []
        if event.get("thread_ts"):
            try:
                result = await client.conversations_replies(
                    channel=channel,
                    ts=event["thread_ts"],
                    limit=20,
                )
                messages = result.get("messages", [])
                # 현재 메시지 제외, 최근 20개만 유지
                thread_history = [m for m in messages if m["ts"] != event["ts"]][-20:]
            except Exception:
                logger.warning("스레드 이력 조회 실패", exc_info=True)

        # 태스크 정리
        task_manager.cleanup_old()

        # 위키 검색 + DB 조회 + 태스크 컨텍스트 포함
        try:
            async with _chat_semaphore:
                answer = await answer_question(
                    question,
                    tasks,
                    thread_history,
                    wiki_project_path=wiki_path,
                    db_backend_path=db_backend_path,
                )
        except Exception:
            logger.exception("질문 답변 처리 중 에러")
            answer = ":warning: 질문 처리 중 오류가 발생했습니다."

        # 출력 마스킹
        answer, was_redacted = redact_output(answer)
        if was_redacted:
            answer += "\n\n:lock: 일부 민감 정보가 보안 정책에 의해 마스킹되었습니다."

        # 리액션 제거
        try:
            await client.reactions_remove(
                channel=channel, timestamp=event["ts"], name="eyes"
            )
        except Exception:
            logger.warning("리액션 제거 실패", exc_info=True)

        await say(
            answer,
            thread_ts=thread_ts,
        )

    @app.event("message")
    async def handle_dm(event, say, client):
        """1:1 DM 메시지 처리. @멘션과 동일하게 동작."""
        # DM(im)만 처리, 채널 메시지는 무시
        if event.get("channel_type") != "im":
            return
        # 봇 자신의 메시지, 서브타입(join/leave 등) 무시
        if event.get("bot_id") or event.get("subtype"):
            return

        raw_text = event.get("text", "")
        # @멘션 부분이 있으면 제거
        question = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()

        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = event.get("user", "")
        channel = event["channel"]

        # 감사 로그 + 인증 + rate limit
        authorized = check_auth(user_id, "chat", security.allowed_users)
        log_command(user_id, "", channel, "dm", question[:80], authorized)
        if not authorized:
            await say(_AUTH_DENIED, thread_ts=thread_ts)
            return
        if not _chat_limiter.check(user_id):
            await say(_RATE_LIMITED, thread_ts=thread_ts)
            return

        if not question:
            return

        # 즉시 리액션
        try:
            await client.reactions_add(
                channel=channel, timestamp=event["ts"], name="eyes"
            )
        except Exception:
            logger.warning("리액션 추가 실패", exc_info=True)

        tasks = task_manager.get_tasks_for_channel(channel)

        # 대화 이력 조회
        thread_history: list[dict] = []
        try:
            if event.get("thread_ts"):
                # 스레드 내 메시지
                result = await client.conversations_replies(
                    channel=channel,
                    ts=event["thread_ts"],
                    limit=20,
                )
            else:
                # 최상위 메시지 — DM 채널의 최근 대화
                result = await client.conversations_history(
                    channel=channel,
                    limit=20,
                )
            messages = result.get("messages", [])
            if not event.get("thread_ts"):
                messages.reverse()
            thread_history = [m for m in messages if m["ts"] != event["ts"]][-20:]
        except Exception:
            logger.warning("DM 이력 조회 실패", exc_info=True)

        task_manager.cleanup_old()

        try:
            async with _chat_semaphore:
                answer = await answer_question(
                    question,
                    tasks,
                    thread_history,
                    wiki_project_path=wiki_path,
                    db_backend_path=db_backend_path,
                )
        except Exception:
            logger.exception("DM 답변 처리 중 에러")
            answer = ":warning: 질문 처리 중 오류가 발생했습니다."

        # 출력 마스킹
        answer, was_redacted = redact_output(answer)
        if was_redacted:
            answer += "\n\n:lock: 일부 민감 정보가 보안 정책에 의해 마스킹되었습니다."

        # 리액션 제거
        try:
            await client.reactions_remove(
                channel=channel, timestamp=event["ts"], name="eyes"
            )
        except Exception:
            logger.warning("리액션 제거 실패", exc_info=True)

        await say(
            answer,
            thread_ts=thread_ts,
        )


async def _run_and_report(
    app: AsyncApp,
    task_manager: TaskManager,
    project,
    task,
    prompt_display: str,
    slash_command: str,
    semaphore: asyncio.Semaphore,
) -> None:
    try:
        async with semaphore:
            result = await run_claude(project, task.command, task.args, task)
        task_manager.complete_task(task.task_id, result.success)

        status = "완료" if result.success else "실패"
        emoji = ":white_check_mark:" if result.success else ":x:"

        # 출력 마스킹
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
                        f"실행자: <@{task.user}>\n"
                        f"원본 명령어: `{slash_command}`"
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
                f"로그를 확인해주세요.\n원본 명령어: `{slash_command}`"
            ),
        )


async def _run_db_query_and_report(
    app: AsyncApp,
    question: str,
    channel: str,
    user: str,
    db_backend_path: str,
    wiki_path: str | None,
    slash_command: str,
    semaphore: asyncio.Semaphore,
) -> None:
    try:
        async with semaphore:
            answer = await run_db_query(question, db_backend_path, wiki_path)
        # 출력 마스킹
        if answer:
            answer, was_redacted = redact_output(answer)
            if was_redacted:
                answer += "\n\n:lock: 일부 민감 정보가 보안 정책에 의해 마스킹되었습니다."
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":mag: *DB 조회 결과* (요청: <@{user}>)\n"
                        f"원본 명령어: `{slash_command}`"
                    ),
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": answer or "_출력 없음_"},
            },
        ]
        await app.client.chat_postMessage(
            channel=channel, blocks=blocks, text=f"DB 조회 결과: {question}"
        )
    except Exception:
        logger.exception("DB 조회 중 에러 발생")
        await app.client.chat_postMessage(
            channel=channel,
            text=(
                f":warning: DB 조회 중 에러가 발생했습니다. 로그를 확인해주세요.\n"
                f"원본 명령어: `{slash_command}`"
            ),
        )
