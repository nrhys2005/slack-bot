"""인텐트 라우팅 테스트 — 특히 셸 명령 오라우팅으로 인한 1시간 안전망 도달 방지."""

from __future__ import annotations

from slack_bot.config import DBConfig, ProjectConfig
from slack_bot.intent import _looks_like_shell_attempt, parse_intent


def _projects() -> dict[str, ProjectConfig]:
    """실서비스 projects.yaml의 description 구조를 모사한 픽스처."""
    return {
        "wiki": ProjectConfig(
            name="wiki",
            path="/tmp/wiki",
            wiki=True,
            description="RA 위키 (Notion 미러)",
        ),
        "moment-some": ProjectConfig(
            name="moment-some",
            path="/tmp/moment-some",
            description="모멘트섬 백엔드",
            commands=["harness", "review"],
        ),
        "ra-backend": ProjectConfig(
            name="ra-backend",
            path="/tmp/ra-backend",
            description="RA 백엔드",
            db=DBConfig(env_file=".env", env_prefix={"main": "DB"}),
        ),
    }


class TestAsciiDescriptionWordBoundary:
    """ASCII description 키워드는 단어 경계를 요구해야 한다 — 약어 오매칭 방지."""

    def test_trader_does_not_match_wiki_via_ra_substring(self):
        """`trader`의 부분 문자열 `ra`가 wiki의 description `RA`에 매칭되면 안 된다."""
        i = parse_intent("trader 에서 뭐가 있나?", _projects())
        assert i.project != "wiki"

    def test_korean_description_keeps_substring_match(self):
        """한국어 키워드는 단어 경계가 모호하므로 부분 문자열 매칭을 유지한다."""
        # "모멘트섬" description 키워드가 본문에 그대로 등장
        i = parse_intent("모멘트섬 상태 어때?", _projects())
        assert i.project == "moment-some"

    def test_exact_ascii_word_still_matches(self):
        """단어 경계가 있으면 ASCII 키워드도 정상 매칭."""
        i = parse_intent("RA 위키에서 온보딩 찾아줘", _projects())
        # "RA" 또는 "위키"로 wiki가 잡혀야 함
        assert i.project == "wiki"

    def test_keyword_with_non_word_boundary_chars_matches(self):
        """`C#`, `.NET`처럼 비단어 문자로 시작/끝나는 키워드도 매칭되어야 한다.

        \\b는 \\w와 \\W 사이만 잡으므로 양 끝에 무조건 \\b를 붙이면 매칭 자체가
        실패한다 — 끝 문자가 단어 문자일 때만 선택적으로 경계를 부여한다.
        """
        projects = {
            "csharp-app": ProjectConfig(
                name="csharp-app",
                path="/tmp/csharp",
                description="C# 백엔드",
            ),
        }
        # "C#" 키워드는 # 가 비단어 문자라 우측 \b가 붙으면 안 됨
        i = parse_intent("C# 백엔드 상태 어때?", projects)
        assert i.project == "csharp-app"


class TestUnknownShellGuard:
    """셸 명령 모양인데 프로젝트 매칭 실패 시 unknown_shell로 빠르게 차단."""

    def test_user_multiline_shell_command_with_unknown_project(self):
        """실제 사용자 케이스 재현 — trader는 등록되지 않은 프로젝트."""
        text = (
            "trader 에서\n"
            "  uv run python -m scripts.run_qvi_filter_relaxation_backtest \\\n"
            "      --start 20220101 --end 20251231 \\\n"
            "      --output output/trd065실행해"
        )
        i = parse_intent(text, _projects())
        # question으로 흘러가 1시간 안전망 도달하면 안 됨
        assert i.type == "unknown_shell"
        assert i.project == ""

    def test_python_command_unknown_project(self):
        text = "foobar에서 python -m pytest 실행해"
        i = parse_intent(text, _projects())
        assert i.type == "unknown_shell"

    def test_known_project_shell_still_routes_to_shell_exec(self):
        """프로젝트가 매칭되면 unknown_shell이 아닌 shell_exec로 가야 한다."""
        text = "moment-some uv pip list 실행해"
        i = parse_intent(text, _projects())
        assert i.type == "shell_exec"

    def test_natural_language_question_does_not_trigger(self):
        """일반 질문(셸 hint 없음)은 영향 받지 않음."""
        i = parse_intent("온보딩 절차 알려줘", _projects())
        assert i.type == "question"


class TestCodeFencedShellCommand:
    """Slack 코드블록(``` ```)으로 감싼 셸 명령도 shell_exec로 라우팅돼야 한다."""

    def test_triple_backtick_inline_routes_to_shell_exec(self):
        """실제 사용자 케이스 재현 — 백틱 코드펜스가 question으로 새면 1시간 안전망 도달."""
        text = (
            "moment-some 에서\n"
            "```uv run python -m scripts.run_4h_experiments```\n"
            "실행해"
        )
        i = parse_intent(text, _projects())
        assert i.type == "shell_exec"
        assert i.command == "uv run python -m scripts.run_4h_experiments"

    def test_triple_backtick_with_language_identifier(self):
        """여는 펜스 뒤 언어 식별자(```bash\\n)는 제거하고 명령만 남긴다."""
        text = "moment-some ```bash\nuv run python foo.py\n``` 실행해"
        i = parse_intent(text, _projects())
        assert i.type == "shell_exec"
        assert i.command == "uv run python foo.py"

    def test_fenced_command_unknown_project_routes_to_unknown_shell(self):
        """프로젝트 미매칭 + 코드펜스 셸 명령은 unknown_shell로 차단돼야 한다."""
        text = "trader 에서\n```uv run python -m scripts.run_4h_experiments```\n실행해"
        i = parse_intent(text, _projects())
        assert i.type == "unknown_shell"


class TestLooksLikeShellAttempt:
    """_looks_like_shell_attempt 헬퍼는 다중행에서도 동작해야 한다."""

    def test_detects_shell_hint_on_first_line(self):
        assert _looks_like_shell_attempt("uv run python foo.py")

    def test_detects_shell_hint_on_later_line(self):
        text = "trader 에서\n  uv run python -m scripts.foo"
        assert _looks_like_shell_attempt(text)

    def test_rejects_pure_natural_language(self):
        assert not _looks_like_shell_attempt("온보딩 절차 좀 알려줘")

    def test_does_not_match_slash_commands(self):
        """`/`로 시작해도 슬래시 커맨드와 충돌하지 않도록 hint에서 제외."""
        assert not _looks_like_shell_attempt("/stop 003")
        assert not _looks_like_shell_attempt("/restart")
