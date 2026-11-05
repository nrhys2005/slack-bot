"""LLM 기반 인텐트 라우터.

명령어 텍스트 규칙(`intent.parse_intent`)은 "하네스" 같은 단어가 설명 속에 들어 있어도
`/harness` 실행으로 분류하는 오라우팅을 만든다. 슬래시 명령, 관리 명령, 태스크 제어처럼
형식이 명확한 것만 규칙으로 처리하고, 나머지 자연어는 하위 모델(haiku)이 문맥을 보고
"어느 프로젝트에 무엇을 시키는지" 판단한다.

안전 원칙: 라우터가 실패하거나 확신하지 못하면 실행 경로(command, project_prompt,
shell_exec)로 가지 않고 질문 답변(question)으로 낮춘다. 단 question 경로의 claude 도
bypassPermissions 로 돌기 때문에 쓰기가 기술적으로 막혀 있지는 않다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from collections.abc import Awaitable, Callable

from slack_bot.config import ProjectConfig
from slack_bot.intent import (
    _COMMAND_ALIASES,
    _ISSUE_ID_RE,
    Intent,
    _extract_remaining_args,
    _extract_shell_command,
    parse_intent,
)
from slack_bot.security import make_safe_env

logger = logging.getLogger(__name__)

ROUTER_MODEL = "haiku"
ROUTER_TIMEOUT = 30  # 초. 분류 한 번에 이 이상 걸리면 규칙 폴백 대신 안전한 질문으로 낮춘다
MAX_CONCURRENT_ROUTER = 3  # 메시지가 몰려도 분류용 claude 프로세스가 무제한으로 뜨지 않게 한다

_router_semaphore = asyncio.Semaphore(MAX_CONCURRENT_ROUTER)

_ALLOWED_COMMANDS = frozenset(_COMMAND_ALIASES.values())
_ACTIONS = frozenset(
    {"command", "shell", "prompt", "status", "db_query", "db_export", "question"}
)

# 호출부가 바꿔 끼울 수 있게 둔 타입 (테스트에서 가짜 모델을 주입한다)
ModelCaller = Callable[[str, str], Awaitable[str]]

_ROUTER_SYSTEM_PROMPT = """너는 Slack 메시지를 어느 프로젝트에 무엇을 시키는 말인지 분류하는 라우터다.
설명하지 말고 JSON 한 개만 출력한다.

## 프로젝트 목록
{project_lines}

## action 값
- command: 사용자가 프로젝트에서 슬래시 명령({commands})을 직접 실행하라고 요청함.
  단어가 언급됐다는 이유만으로 고르지 않는다. "하네스로 안 돌려도 되는지 확인해줘",
  "하네스 구조를 설명해줘" 는 command 가 아니라 question 이다.
- shell: 프로젝트에서 셸 명령(uv, git, ls 등)을 그대로 실행하라고 요청함.
- prompt: 프로젝트 코드나 파일을 직접 고치거나 작업을 시키는 자유 요청 (쓰기 가능).
- status: 프로젝트의 현재 상태, 진행 상황, 로그를 묻는 질문.
- db_query: DB 조회 요청. 파일로 받기를 원하면 db_export.
- question: 읽기만 하면 되는 질문, 조사, 설계 논의, 확인 요청. 애매하면 이것을 고른다.

## 규칙
- 프로젝트는 위 목록의 name 중 하나만 쓴다. 특정되지 않거나 여러 프로젝트에 걸치면 빈 문자열.
- 여러 프로젝트에 걸친 요청, 봇 자체나 도구 설계에 관한 논의는 project 를 비우고 question.
- 프로젝트 이름이 문장에 나와도 그 프로젝트에서 실행하라는 뜻이 아닐 수 있다. 문맥으로 판단한다.
- command 일 때만 command 에 {commands} 중 하나를 쓴다.
- 실행(command, shell, prompt)은 사용자가 분명히 시켰을 때만. 조금이라도 의심되면 question.

## 출력 형식
{{"project": "", "action": "question", "command": "", "reason": "한 문장"}}
"""


def _build_router_prompt(projects: dict[str, ProjectConfig]) -> str:
    lines = [f"- {name}: {cfg.description or '(설명 없음)'}" for name, cfg in projects.items()]
    return _ROUTER_SYSTEM_PROMPT.format(
        project_lines="\n".join(lines) or "(없음)",
        commands=", ".join(sorted(_ALLOWED_COMMANDS)),
    )


async def _call_haiku(system_prompt: str, user_text: str) -> str:
    """claude -p 로 haiku 를 한 번 호출하고 stdout 을 돌려준다.

    --tools "" 만으로는 MCP 도구가 남고 CLAUDE.md 도 읽히므로, 설정 소스와 MCP 까지 끊어
    메시지를 지시가 아니라 분류 대상으로만 다루게 한다.
    """
    async with _router_semaphore:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            user_text,
            "--model",
            ROUTER_MODEL,
            "--tools",
            "",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--output-format",
            "text",
            "--system-prompt",
            system_prompt,
            cwd=tempfile.gettempdir(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=make_safe_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=ROUTER_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace")[:300]
        raise RuntimeError(f"라우터 claude 종료 코드 {proc.returncode}: {detail}")
    return stdout.decode(errors="replace")


def _extract_json(raw: str) -> dict | None:
    """모델 출력에서 첫 JSON 객체를 꺼낸다. 코드펜스나 앞뒤 설명이 붙어도 허용한다."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _intent_from_decision(
    decision: dict, text: str, projects: dict[str, ProjectConfig]
) -> Intent:
    """모델 판단을 검증해 Intent 로 바꾼다. 검증을 통과하지 못하면 question 으로 낮춘다."""
    project = str(decision.get("project") or "")
    if project not in projects:
        project = ""
    action = str(decision.get("action") or "question")
    if action not in _ACTIONS:
        action = "question"

    # 프로젝트가 필요한 실행 계열은 프로젝트가 없으면 실행하지 않는다.
    needs_project = {"command", "shell", "prompt", "status"}
    if action in needs_project and not project:
        return Intent(type="question", raw_text=text)

    if action == "command":
        command = str(decision.get("command") or "")
        if command not in _ALLOWED_COMMANDS:
            return Intent(type="question", project=project, raw_text=text)
        # 인자는 원문에서 결정적으로 뽑는다. 모델에 맡기면 호출마다 달라져 중복 실행 가드를 통과한다.
        issue_match = _ISSUE_ID_RE.search(text)
        issue_id = issue_match.group(1) if issue_match else ""
        remaining = _extract_remaining_args(text, project, command, issue_id, projects)
        args = f"{issue_id} {remaining}".strip()
        return Intent(
            type="command", project=project, command=command, args=args, raw_text=text
        )
    if action == "shell":
        shell_cmd = _extract_shell_command(text, project, projects)
        if not shell_cmd:
            return Intent(type="question", project=project, raw_text=text)
        return Intent(type="shell_exec", project=project, command=shell_cmd, raw_text=text)
    if action == "prompt":
        return Intent(type="project_prompt", project=project, args=text, raw_text=text)
    if action == "status":
        return Intent(type="status", project=project, raw_text=text)
    if action in ("db_query", "db_export"):
        if not any(p.db is not None for p in projects.values()):
            return Intent(type="question", project=project, raw_text=text)
        return Intent(
            type="db_query",
            project=project,
            raw_text=text,
            export=action == "db_export",
        )
    return Intent(type="question", project=project, raw_text=text)


async def route_intent(
    text: str,
    projects: dict[str, ProjectConfig],
    *,
    call_model: ModelCaller = _call_haiku,
) -> Intent:
    """메시지를 분류한다. 규칙으로 확정되는 형식은 규칙이, 나머지는 하위 모델이 정한다."""
    rule_intent = parse_intent(text, projects)

    # 관리 명령, 태스크 제어는 형식이 명확해서 모델에 맡기지 않는다. 프로젝트 없는 셸 명령도
    # 모델에 넘기면 question 으로 1시간 헛돌거나 지정하지 않은 프로젝트에서 실행되므로 그대로 막는다.
    if rule_intent.type in ("admin", "task_control", "unknown_shell"):
        return rule_intent
    # 슬래시로 직접 입력한 명령은 사용자가 의도를 명시한 것이다.
    if rule_intent.raw_text.startswith("/") and rule_intent.type == "command":
        return rule_intent

    try:
        raw = await call_model(_build_router_prompt(projects), rule_intent.raw_text)
        decision = _extract_json(raw)
    except Exception:
        logger.warning("인텐트 라우터 호출 실패, 질문으로 낮춘다", exc_info=True)
        decision = None

    if decision is None:
        # 규칙이 매긴 프로젝트만 살리고 실행 계열은 버린다.
        return Intent(
            type="question", project=rule_intent.project, raw_text=rule_intent.raw_text
        )

    logger.info(
        "인텐트 라우팅: project=%s action=%s reason=%s",
        decision.get("project"),
        decision.get("action"),
        decision.get("reason"),
    )
    return _intent_from_decision(decision, rule_intent.raw_text, projects)
