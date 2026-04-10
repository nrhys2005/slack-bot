from __future__ import annotations

import asyncio
import logging
import re

from slack_bolt.async_app import AsyncApp

from slack_bot.chat import answer_question
from slack_bot.config import load_projects
from slack_bot.runner import run_claude
from slack_bot.task_manager import TaskManager

logger = logging.getLogger(__name__)


def register_handlers(app: AsyncApp, task_manager: TaskManager) -> None:
    projects = load_projects()

    @app.command("/claude")
    async def handle_claude_command(ack, command, respond):
        """
        /claude <project> <command> [args]

        예시:
          /claude moment-some harness MOM-43
          /claude moment-some plan MOM-43
          /claude moment-some develop MOM-43 --auto
        """
        await ack()

        text = (command.get("text") or "").strip()
        parts = text.split(None, 2)

        # 입력 검증: 프로젝트명 + 명령어 최소 필요
        if len(parts) < 2:
            project_list = ", ".join(f"`{p}`" for p in projects)
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
            await respond(f"알 수 없는 프로젝트: `{project_name}`\n등록된 프로젝트: {project_list}")
            return

        # 명령어 검증
        if cmd not in project.commands:
            cmd_list = ", ".join(f"`{c}`" for c in project.commands)
            await respond(
                f"`{project_name}`에서 허용된 명령어: {cmd_list}"
            )
            return

        # 태스크 생성
        user = command.get("user_name", "unknown")
        channel = command["channel_id"]
        task = task_manager.create_task(project_name, cmd, args, user, channel)
        prompt_display = f"/{cmd} {args}".strip()

        await respond(
            f"*{project_name}* 프로젝트에서 `{prompt_display}` 실행 중... (ID: {task.task_id})\n"
            f"완료되면 이 채널에 결과를 알려드립니다. "
            f"`@bot 지금 어디까지 됐어?` 로 진행상황을 확인할 수 있습니다."
        )

        asyncio.create_task(
            _run_and_report(app, task_manager, project, task, prompt_display)
        )

    @app.command("/claude-projects")
    async def handle_projects_command(ack, respond):
        """/claude-projects — 등록된 프로젝트 목록 조회"""
        await ack()
        lines = []
        for name, cfg in projects.items():
            cmds = ", ".join(f"`{c}`" for c in cfg.commands)
            lines.append(f"*{name}*: {cmds}")
        await respond("등록된 프로젝트:\n" + "\n".join(lines))

    @app.command("/claude-stop")
    async def handle_stop_command(ack, command, respond):
        """/claude-stop <task_id> — 실행 중인 태스크 중단"""
        await ack()
        task_id = (command.get("text") or "").strip()

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
                "중단할 태스크 ID를 입력해주세요: `/claude-stop <ID>`\n\n"
                "실행 중인 태스크:\n" + "\n".join(lines)
            )
            return

        if task_manager.stop_task(task_id):
            await respond(f"태스크 {task_id} 중단됨 :octagonal_sign:")
        else:
            await respond(f"태스크 `{task_id}`를 찾을 수 없거나 이미 종료되었습니다.")

    @app.event("app_mention")
    async def handle_mention(event, say):
        """@봇 멘션 시 Claude API로 질문에 답변"""
        raw_text = event.get("text", "")
        # @멘션 부분 제거
        question = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()

        if not question:
            await say(
                "무엇이 궁금하신가요? 실행 중인 태스크에 대해 질문해주세요.",
                thread_ts=event.get("thread_ts") or event["ts"],
            )
            return

        channel = event["channel"]
        tasks = task_manager.get_tasks_for_channel(channel)

        # 태스크 정리
        task_manager.cleanup_old()

        answer = await answer_question(question, tasks)
        await say(
            answer,
            thread_ts=event.get("thread_ts") or event["ts"],
        )


async def _run_and_report(
    app: AsyncApp,
    task_manager: TaskManager,
    project,
    task,
    prompt_display: str,
) -> None:
    try:
        result = await run_claude(project, task.command, task.args, task)
        task_manager.complete_task(task.task_id, result.success)

        status = "완료" if result.success else "실패"
        emoji = ":white_check_mark:" if result.success else ":x:"

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
                    "text": f"```\n{result.output}\n```" if result.output else "_출력 없음_",
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
            text=f":warning: *{task.project_name}* `{prompt_display}` 실행 중 에러가 발생했습니다. 로그를 확인해주세요.",
        )
