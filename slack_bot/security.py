"""보안 유틸리티 — 환경변수 필터링, 출력 마스킹, 인증, rate limit, 감사 로깅."""

from __future__ import annotations

import logging
import os
import re
import time
from collections import defaultdict

audit_logger = logging.getLogger("slack_bot.audit")

# ---------------------------------------------------------------------------
# 1. 환경변수 화이트리스트
# ---------------------------------------------------------------------------

_ENV_WHITELIST = frozenset({
    # 시스템 필수
    "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "TMPDIR",
    # XDG (Claude CLI 설정 경로)
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "CLAUDE_CONFIG_DIR",
    # Node/Python 런타임
    "NODE_PATH", "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    # Git (harness에서 커밋/push 필요)
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    "GIT_SSH_COMMAND", "SSH_AUTH_SOCK",
    # macOS 관련
    "__CF_USER_TEXT_ENCODING",
})


def make_safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """os.environ에서 화이트리스트 키만 추출하고, extra를 병합해 반환한다."""
    env = {k: v for k, v in os.environ.items() if k in _ENV_WHITELIST}
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# 2. 출력 필터링 (자격증명 패턴 마스킹)
# ---------------------------------------------------------------------------

_REDACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Slack 토큰 (xoxb-, xoxp-, xoxa-, xoxs-, xapp-)
    (re.compile(r"(?:xox[bpsa]|xapp)-[0-9A-Za-z\-]+"), "***SLACK_TOKEN_REDACTED***"),
    # AWS Access Key
    (re.compile(r"AKIA[0-9A-Z]{16}"), "***AWS_KEY_REDACTED***"),
    # JWT 토큰
    (re.compile(
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    ), "***JWT_REDACTED***"),
    # PostgreSQL 연결 문자열
    (re.compile(r"postgresql://[^\s]+"), "***PG_CONN_REDACTED***"),
    # URI 내 자격증명 (://user:pass@)
    (re.compile(r"://[^:]+:[^@]+@"), "://***:***@"),
    # key=value 형태의 시크릿 (password=xxx, secret=xxx 등)
    (re.compile(
        r"(?i)(password|passwd|secret|token|api[_-]?key|auth[_-]?token|"
        r"access[_-]?key|private[_-]?key)\s*[=:]\s*\S+"
    ), r"\1=***REDACTED***"),
]


def redact_output(text: str) -> tuple[str, bool]:
    """텍스트에서 자격증명 패턴을 마스킹한다. (마스킹된 텍스트, 마스킹 여부) 반환."""
    redacted = False
    for pattern, replacement in _REDACT_PATTERNS:
        new_text = pattern.sub(replacement, text)
        if new_text != text:
            redacted = True
            text = new_text
    return text, redacted


# ---------------------------------------------------------------------------
# 3. 사용자 인증
# ---------------------------------------------------------------------------


def check_auth(
    user_id: str, role: str, allowed_users: dict[str, list[str]]
) -> bool:
    """role에 해당하는 허용 유저 목록에 user_id가 있는지 확인. '*'은 전체 허용."""
    users = allowed_users.get(role, [])
    return "*" in users or user_id in users


# ---------------------------------------------------------------------------
# 4. Rate Limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """인메모리 per-user rate limiter. 봇 재시작 시 초기화."""

    def __init__(self, max_calls: int, window_seconds: int) -> None:
        self._max = max_calls
        self._window = window_seconds
        self._calls: dict[str, list[float]] = defaultdict(list)

    def check(self, user_id: str) -> bool:
        """허용이면 True, rate limit 초과면 False."""
        now = time.time()
        calls = self._calls[user_id]
        self._calls[user_id] = [t for t in calls if now - t < self._window]
        if len(self._calls[user_id]) >= self._max:
            return False
        self._calls[user_id].append(now)
        return True


# ---------------------------------------------------------------------------
# 5. 감사 로깅
# ---------------------------------------------------------------------------


def log_command(
    user_id: str,
    user_name: str,
    channel: str,
    command: str,
    args: str,
    authorized: bool,
) -> None:
    """커맨드 실행 감사 로그."""
    audit_logger.info(
        "COMMAND user_id=%s user_name=%s channel=%s command=%s args=%s authorized=%s",
        user_id,
        user_name,
        channel,
        command,
        args[:100],
        authorized,
    )
