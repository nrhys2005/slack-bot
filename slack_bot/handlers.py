from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys

from slack_bolt.async_app import AsyncApp

from slack_bot.chat import answer_question
from slack_bot.config import ProjectConfig, load_projects
from slack_bot.db_query import run_db_query, run_db_query_export
from slack_bot.intent import Intent, parse_intent
from slack_bot.runner import run_claude
from slack_bot.security import check_auth, redact_output
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


def register_handlers(app: AsyncApp, task_manager: TaskManager) -> None:
    _chat_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHAT)
    _task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASK)
    _background_tasks: set[asyncio.Task] = set()

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

        if intent.export:
            await say(
                f":outbox_tray: `{intent.raw_text}` 데이터 추출 중...",
                thread_ts=thread_ts,
            )
            bg_task = asyncio.create_task(
                _run_db_query_export_and_report(
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
        else:
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

        # 진행 상태 메시지 전송
        progress_msg = await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":hourglass_flowing_sand: 처리 중...",
        )
        progress_ts = progress_msg["ts"]

        async def _on_progress(status: str) -> None:
            try:
                await client.chat_update(
                    channel=channel,
                    ts=progress_ts,
                    text=status,
                )
            except Exception:
                pass

        try:
            async with _chat_semaphore:
                answer = await answer_question(
                    question,
                    tasks,
                    thread_history,
                    projects=projects,
                    target_project=target_project,
                    on_progress=_on_progress,
                )
        except Exception:
            logger.exception("질문 답변 처리 중 에러")
            answer = ":warning: 질문 처리 중 오류가 발생했습니다."

        # 진행 상태 메시지 삭제
        try:
            await client.chat_delete(channel=channel, ts=progress_ts)
        except Exception:
            pass

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
        """Claude CLI 인증 확인 버튼 클릭."""
        await ack()

        channel_id = body["channel"]["id"]
        msg_ts = body["message"]["ts"]

        await client.chat_update(
            channel=channel_id,
            ts=msg_ts,
            text=":hourglass_flowing_sand: Claude CLI 인증 프로세스를 시작합니다...",
            blocks=[],
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "auth",
                "login",
                "--claudeai",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            AUTH_TIMEOUT = 300  # 5분

            url_sent = False
            collected_output: list[str] = []

            url_re = re.compile(r"https?://\S+")

            async def _read_stream(stream: asyncio.StreamReader) -> None:
                nonlocal url_sent
                async for line_bytes in stream:
                    line = line_bytes.decode(errors="replace").strip()
                    if not line:
                        continue
                    collected_output.append(line)
                    logger.info("claude auth login: %s", line)

                    if not url_sent:
                        url_match = url_re.search(line)
                        if url_match:
                            url = url_match.group(0)
                            url_sent = True
                            asyncio.create_task(
                                client.chat_postMessage(
                                    channel=channel_id,
                                    thread_ts=msg_ts,
                                    text=(
                                        ":link: 아래 URL을 브라우저에서 열어 인증을 완료하세요:\n"
                                        f"```\n{url}\n```\n"
                                        f"_{AUTH_TIMEOUT}초 내에 인증을 완료해주세요._"
                                    ),
                                )
                            )

            await asyncio.wait_for(
                asyncio.gather(
                    _read_stream(proc.stdout),
                    _read_stream(proc.stderr),
                    proc.wait(),
                ),
                timeout=AUTH_TIMEOUT,
            )

            if proc.returncode == 0:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=msg_ts,
                    text=":white_check_mark: Claude CLI 인증이 완료되었습니다!",
                )
            else:
                output = "\n".join(collected_output[-10:])
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=msg_ts,
                    text=f":x: Claude CLI 인증 실패 (exit {proc.returncode}):\n```\n{output[:2000]}\n```",
                )

        except FileNotFoundError:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=msg_ts,
                text=":x: `claude` CLI가 설치되어 있지 않습니다. 먼저 Claude CLI를 설치해주세요.",
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=msg_ts,
                text=f":x: 인증 시간이 초과되었습니다 ({AUTH_TIMEOUT}초). 다시 시도해주세요.",
            )
        except Exception as e:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=msg_ts,
                text=f":x: Claude CLI 인증 중 에러: {e}",
            )

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
            result = await run_db_query_export(question, db_project, wiki_path)

        if result.error or result.excel_path is None:
            # 파일 생성 실패 시 텍스트로 폴백
            error_msg = result.error or "파일 생성에 실패했습니다."
            if result.summary:
                text = f"{result.summary}\n\n:warning: {error_msg}"
            else:
                text = f":warning: {error_msg}"
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
        finally:
            # 임시 파일 정리
            result.excel_path.unlink(missing_ok=True)

    except Exception:
        logger.exception("DB 조회 엑셀 내보내기 중 에러")
        await app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":warning: DB 조회 결과 파일 생성 중 에러가 발생했습니다.",
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
                answer += (
                    "\n\n:lock: 일부 민감 정보가 보안 정책에 의해 마스킹되었습니다."
                )

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
