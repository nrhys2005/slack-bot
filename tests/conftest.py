from __future__ import annotations

import pytest

from slack_bot.intent import parse_intent


@pytest.fixture(autouse=True)
def _rule_based_router_in_handlers(monkeypatch):
    """핸들러 테스트가 실제 claude(haiku)를 호출하지 않도록 라우터를 규칙 파서로 대체한다.

    라우터 자체는 tests/test_intent_router.py 에서 가짜 모델로 검증한다.
    """

    async def _rule_route(text, projects, **_kwargs):
        return parse_intent(text, projects)

    monkeypatch.setattr("slack_bot.handlers.route_intent", _rule_route)
