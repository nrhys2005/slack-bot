"""LLM 인텐트 라우터 테스트. 실제 claude 호출 없이 가짜 모델 응답을 주입한다."""

from __future__ import annotations

import asyncio
import json

import pytest

from slack_bot.config import DBConfig, ProjectConfig
from slack_bot.intent_router import _call_haiku, _extract_json, route_intent

INCIDENT_TEXT = (
    "trader, cow-alert 프로젝트 관련해서 하네스나 이런거 실행할때 orca 세션 만들어서 "
    "실행 및 관리도록 하면 좋을것같은데 확인해줘."
)


def _projects() -> dict[str, ProjectConfig]:
    return {
        "trader": ProjectConfig(name="trader", path="/tmp/trader", description="자동매매"),
        "cow-alert": ProjectConfig(name="cow-alert", path="/tmp/cow", description="알림"),
        "ra-backend": ProjectConfig(
            name="ra-backend",
            path="/tmp/ra",
            description="RA 백엔드",
            db=DBConfig(env_file=".env", env_prefix={"main": "DB"}),
        ),
    }


def _fake(decision: dict | str):
    calls: list[tuple[str, str]] = []

    async def call(system_prompt: str, text: str) -> str:
        calls.append((system_prompt, text))
        return decision if isinstance(decision, str) else json.dumps(decision)

    call.calls = calls  # type: ignore[attr-defined]
    return call


@pytest.mark.asyncio
async def test_incident_message_is_question_not_harness():
    """설명 속 '하네스' 단어가 /harness 실행으로 가지 않는다."""
    model = _fake({"project": "", "action": "question", "reason": "설계 논의"})
    i = await route_intent(INCIDENT_TEXT, _projects(), call_model=model)
    assert i.type == "question"
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_explicit_harness_request_becomes_command():
    model = _fake(
        {"project": "trader", "action": "command", "command": "harness", "args": "TRD-1"}
    )
    i = await route_intent("trader 하네스 TRD-1 돌려줘", _projects(), call_model=model)
    assert (i.type, i.project, i.command, i.args) == ("command", "trader", "harness", "TRD-1")


@pytest.mark.asyncio
async def test_model_failure_downgrades_to_question():
    async def boom(system_prompt: str, text: str) -> str:
        raise RuntimeError("claude 없음")

    i = await route_intent("trader 하네스 TRD-1 돌려줘", _projects(), call_model=boom)
    assert i.type == "question"
    assert i.project == "trader"


@pytest.mark.asyncio
async def test_garbage_output_downgrades_to_question():
    i = await route_intent("trader 하네스 돌려줘", _projects(), call_model=_fake("몰라요"))
    assert i.type == "question"


@pytest.mark.asyncio
async def test_unknown_project_or_command_is_not_executed():
    bad_project = _fake({"project": "nope", "action": "prompt"})
    assert (await route_intent("뭔가 해줘", _projects(), call_model=bad_project)).type == "question"
    bad_cmd = _fake({"project": "trader", "action": "command", "command": "rm -rf"})
    assert (await route_intent("trader 뭔가 해줘", _projects(), call_model=bad_cmd)).type == "question"


@pytest.mark.asyncio
async def test_slash_and_control_commands_skip_model():
    model = _fake({"project": "trader", "action": "prompt"})
    p = _projects()
    assert (await route_intent("/stop 003", p, call_model=model)).type == "task_control"
    assert (await route_intent("/restart", p, call_model=model)).type == "admin"
    assert (await route_intent("/harness trader TRD-9", p, call_model=model)).type == "command"
    assert model.calls == []


@pytest.mark.asyncio
async def test_db_export_action_sets_export_flag():
    model = _fake({"project": "ra-backend", "action": "db_export"})
    i = await route_intent("지난주 가입자 뽑아줘", _projects(), call_model=model)
    assert (i.type, i.export) == ("db_query", True)


def test_extract_json_handles_code_fence():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json("없음") is None


@pytest.mark.asyncio
async def test_unknown_shell_stays_blocked_without_model():
    """프로젝트 없는 셸 명령은 모델을 거치지 않고 unknown_shell 로 막힌다."""
    model = _fake({"project": "trader", "action": "shell"})
    i = await route_intent("uv run pytest 실행해줘", _projects(), call_model=model)
    assert i.type == "unknown_shell"
    assert model.calls == []


@pytest.mark.asyncio
async def test_command_args_come_from_text_not_model():
    """모델이 args 를 비우거나 지어내도 이슈 ID 는 원문에서 뽑는다."""
    model = _fake(
        {"project": "trader", "action": "command", "command": "review", "args": "지어낸 값"}
    )
    i = await route_intent("trader TRD-1 리뷰해줘", _projects(), call_model=model)
    assert i.type == "command"
    assert i.args.startswith("TRD-1")
    assert "지어낸" not in i.args


class _FakeProc:
    def __init__(self, returncode=0, stdout=b"{}", stderr=b"", hang=False):
        self.returncode = returncode
        self._out = (stdout, stderr)
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(60)
        return self._out

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _patch_exec(monkeypatch, proc):
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


@pytest.mark.asyncio
async def test_call_haiku_cuts_off_mcp_and_settings(monkeypatch):
    """분류 호출에는 MCP 도구와 CLAUDE.md 가 들어오지 않아야 한다."""
    seen = _patch_exec(monkeypatch, _FakeProc())
    await _call_haiku("system", "text")
    args = seen["args"]
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in args
    assert seen["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL
    assert seen["kwargs"]["cwd"]


@pytest.mark.asyncio
async def test_call_haiku_kills_process_on_timeout(monkeypatch):
    proc = _FakeProc(hang=True)
    _patch_exec(monkeypatch, proc)
    monkeypatch.setattr("slack_bot.intent_router.ROUTER_TIMEOUT", 0.01)
    with pytest.raises(asyncio.TimeoutError):
        await _call_haiku("system", "text")
    assert proc.killed


@pytest.mark.asyncio
async def test_call_haiku_raises_on_nonzero_exit(monkeypatch):
    _patch_exec(monkeypatch, _FakeProc(returncode=1, stderr=b"boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await _call_haiku("system", "text")
