from __future__ import annotations

import html
import re
from dataclasses import dataclass

from slack_bot.config import ProjectConfig


@dataclass
class Intent:
    """사용자 메시지에서 파싱된 의도."""

    type: (
        # "command" | "shell_exec" | "status" | "question" | "task_control"
        # | "db_query" | "admin" | "unknown_shell"
        str
    )
    project: str = ""  # 식별된 프로젝트명
    command: str = ""  # 실행할 명령어 또는 task_control 동작 (list/stop)
    args: str = ""  # 명령어 인자
    raw_text: str = ""  # 원본 메시지
    export: bool = False  # DB 조회 결과를 CSV/Excel 파일로 내보내기


# 한국어 → 명령어 매핑
_COMMAND_ALIASES: dict[str, str] = {
    "하네스": "harness",
    "harness": "harness",
    "파이프라인": "harness",
    "플랜": "plan",
    "plan": "plan",
    "계획": "plan",
    "개발": "develop",
    "develop": "develop",
    "구현": "develop",
    "리뷰": "review",
    "review": "review",
    "검토": "review",
}

# 자연어 목록 조회 — 오매칭 우려가 적어 그대로 둠
_TASK_LIST_KEYWORDS: frozenset[str] = frozenset({"태스크", "task"})

_ADMIN_KEYWORDS: dict[str, str] = {
    "claude 로그인": "auth_login",
    "클로드 로그인": "auth_login",
    "claude login": "auth_login",
    "claude auth": "auth_login",
    "claude 설치": "install_claude",
    "클로드 설치": "install_claude",
    "install claude": "install_claude",
}

# 슬래시 전용 admin 명령 — 자연어 오매칭 방지 (예: "재시작" 단어가 일반 대화에 자주 등장)
_SLASH_ADMIN_COMMANDS: frozenset[str] = frozenset({"restart"})

_STATUS_KEYWORDS = frozenset(
    {
        "상태",
        "status",
        "어때",
        "어떻게",
        "현황",
        "돌아가",
    }
)

DB_KEYWORDS = frozenset(
    {
        "조회",
        "쿼리",
        "query",
        "몇 건",
        "몇건",
        "통계",
        "테이블",
        "스키마",
        "DB",
        "db",
        "수치",
    }
)

_EXPORT_KEYWORDS = frozenset(
    {
        "추출",
        "다운로드",
        "엑셀",
        "excel",
        "csv",
        "파일로",
        "뽑아",
        "내보내",
        "export",
    }
)

# 이슈 ID (e.g. MOM-43, PROJ-123)
_ISSUE_ID_RE = re.compile(r"\b([A-Z]+-\d+)\b")
# 태스크 ID (e.g. 003, 012) — 한국어 접미사("번") 허용
_TASK_ID_RE = re.compile(r"(\d{3})(?:번)?")

# 명령 실행 의도를 나타내는 트리거 동사 (문장 끝에 위치, 뒤 문장부호 허용)
_COMMAND_TRIGGER_RE = re.compile(
    r"(돌려줘|실행해줘|실행해|해줘|해 줘|해줘요|해주세요|시작해|시작해줘|부탁해|부탁해요|부탁드립니다|좀)[.,!?]*\s*$"
)
# 슬래시 명령 직접 입력 (e.g. /review MOM-43, /harness)
_SLASH_COMMAND_RE = re.compile(r"^/(\w+)(?:\s+(.*))?$")


def parse_intent(
    text: str,
    projects: dict[str, ProjectConfig],
) -> Intent:
    """사용자 메시지에서 의도를 규칙 기반으로 파싱한다."""
    # Slack은 메시지의 &, <, >를 HTML 엔티티(&amp; &lt; &gt;)로 이스케이프해서
    # 보낸다. 원복하지 않으면 `git pull ... && uv sync`가 셸에 `&amp;&amp;`로
    # 전달돼 `/bin/sh: Syntax error: "&" unexpected`로 실패한다. 리다이렉션
    # (>, <)이 든 셸 명령도 같은 이유로 깨진다. 파싱·실행 전에 먼저 원복한다.
    normalized = html.unescape(text).strip()
    lower = normalized.lower()

    # 0. 관리 명령 감지 (재시작/업데이트)
    admin_intent = _detect_admin(normalized, lower)
    if admin_intent:
        return admin_intent

    # 1. 태스크 제어 감지 (중단/목록)
    task_intent = _detect_task_control(normalized, lower)
    if task_intent:
        return task_intent

    # 2. 프로젝트명 감지
    matched_project = _detect_project(normalized, lower, projects)

    # 3. 명령어 감지
    matched_command = _detect_command(lower, matched_project, projects)

    # 4. 이슈 ID 감지
    issue_match = _ISSUE_ID_RE.search(normalized)
    issue_id = issue_match.group(1) if issue_match else ""

    # 명령어 + 프로젝트가 있으면 command 인텐트
    if matched_command and matched_project:
        args = issue_id
        # 원본 텍스트에서 프로젝트명, 명령어 키워드, 이슈ID를 제거한 나머지를 args에 추가
        remaining = _extract_remaining_args(
            normalized, matched_project, matched_command, issue_id, projects
        )
        if remaining:
            args = f"{issue_id} {remaining}".strip() if issue_id else remaining
        elif not args:
            args = ""
        return Intent(
            type="command",
            project=matched_project,
            command=matched_command,
            args=args,
            raw_text=normalized,
        )

    # 4.5 셸 명령 실행 감지: 프로젝트 + 트리거 동사 + 알려진 명령어 아님
    # 문장 끝이 아니어도 트리거 동사가 있으면 매칭 (예: "실행해줘.. 결과는 나중에")
    _has_trigger = bool(
        _COMMAND_TRIGGER_RE.search(lower)
        or re.search(
            r"(돌려줘|실행해줘|실행해|해줘|해 줘|해줘요|해주세요|시작해|시작해줘|부탁해)",
            lower,
        )
    )
    if matched_project and not matched_command and _has_trigger:
        shell_cmd = _extract_shell_command(normalized, matched_project, projects)
        if shell_cmd:
            return Intent(
                type="shell_exec",
                project=matched_project,
                command=shell_cmd,
                raw_text=normalized,
            )

    # 5. DB 조회 감지 (DB 프로젝트가 존재할 때만)
    has_db_project = any(p.db is not None for p in projects.values())
    has_db_keyword = any(kw in lower for kw in DB_KEYWORDS)
    has_export_keyword = any(kw in lower for kw in _EXPORT_KEYWORDS)
    # export 키워드만으로는 db_query 발동 안 됨 — DB 키워드도 함께 있어야 함
    if has_db_project and has_db_keyword:
        return Intent(
            type="db_query",
            project=matched_project or "",
            raw_text=normalized,
            export=has_export_keyword,
        )

    # 6. 상태 조회 감지 (프로젝트 특정됨)
    if matched_project and any(kw in lower for kw in _STATUS_KEYWORDS):
        return Intent(
            type="status",
            project=matched_project,
            raw_text=normalized,
        )

    # 7. 셸 명령처럼 보이는데 프로젝트가 매칭되지 않은 경우 — 명확한 에러로 차단.
    #    트리거 동사 + 어떤 줄이라도 알려진 셸 hint 접두사(`uv `, `python ` 등)로
    #    시작하면 사용자는 셸 실행을 시도한 것. 이대로 question으로 흘리면
    #    claude -p가 다중행 셸 명령을 "질문"으로 받아 1시간 안전 한계에 도달한다.
    if _has_trigger and not matched_project and _looks_like_shell_attempt(normalized):
        return Intent(
            type="unknown_shell",
            raw_text=normalized,
        )

    # 8. 기본: 일반 질문
    return Intent(
        type="question",
        project=matched_project or "",
        raw_text=normalized,
    )


def _detect_admin(text: str, lower: str) -> Intent | None:
    """관리 명령(재시작/업데이트) 인텐트 감지.

    재시작은 자연어 오매칭이 잦아 슬래시 명령(`/restart`)으로만 트리거된다.
    """
    # 슬래시 전용 admin 명령 우선 매칭
    slash_match = _SLASH_COMMAND_RE.match(text.strip())
    if slash_match:
        cmd = slash_match.group(1).lower()
        if cmd in _SLASH_ADMIN_COMMANDS:
            return Intent(type="admin", command=cmd, raw_text=text)

    for keyword, action in _ADMIN_KEYWORDS.items():
        if keyword in lower:
            return Intent(type="admin", command=action, raw_text=text)
    return None


def _detect_task_control(text: str, lower: str) -> Intent | None:
    """태스크 제어(중단/목록) 인텐트 감지.

    중단은 자연어("중단", "멈춰") 오매칭이 잦아 슬래시 명령으로만 트리거된다.
    - `/stop 003` → 003번 태스크 중단
    - `/stop` → 실행 중인 태스크 목록
    - "태스크 보여줘" 같은 자연어 목록 조회는 유지
    """
    # 슬래시 /stop — 인자 있으면 중단, 없으면 목록
    slash_match = _SLASH_COMMAND_RE.match(text.strip())
    if slash_match and slash_match.group(1).lower() == "stop":
        args = (slash_match.group(2) or "").strip()
        task_id_match = _TASK_ID_RE.search(args)
        if task_id_match:
            return Intent(
                type="task_control",
                command="stop",
                args=task_id_match.group(1),
                raw_text=text,
            )
        return Intent(type="task_control", command="list", raw_text=text)

    # 자연어 목록 조회 — "태스크 보여줘"
    if any(kw in lower for kw in _TASK_LIST_KEYWORDS):
        return Intent(type="task_control", command="list", raw_text=text)

    return None


def _detect_project(
    text: str,
    lower: str,
    projects: dict[str, ProjectConfig],
) -> str:
    """메시지에서 프로젝트명 또는 description 키워드로 프로젝트를 식별."""
    # 정확한 프로젝트명 매칭 (긴 이름 우선)
    for name in sorted(projects.keys(), key=len, reverse=True):
        if name.lower() in lower:
            return name

    # description 키워드 매칭. 괄호/특수문자 제거 후 단어 단위 비교.
    # - ASCII 단어: 단어 경계(\b) 요구. "RA"가 "trader"의 부분 문자열에
    #   매칭되어 엉뚱한 프로젝트로 라우팅되는 사고 방지.
    # - 한국어/CJK 단어: 단어 경계 개념이 모호하므로 부분 문자열 매칭 유지.
    for name, cfg in projects.items():
        if cfg.description:
            clean_desc = re.sub(r"[()（）\[\]「」]", " ", cfg.description)
            desc_words = [w for w in clean_desc.split() if len(w) >= 2]
            for word in desc_words:
                lower_word = word.lower()
                if word.isascii():
                    # \b는 \w(영숫자/언더스코어)와 \W 사이 경계만 인식하므로
                    # "C#" / ".NET"처럼 비단어 문자로 시작·끝나는 키워드 양 끝에
                    # 무조건 \b를 붙이면 매칭 자체가 실패한다. 각 끝의 문자가
                    # 단어 문자일 때만 선택적으로 경계를 부여한다.
                    left = r"\b" if _is_word_char(lower_word[0]) else ""
                    right = r"\b" if _is_word_char(lower_word[-1]) else ""
                    pattern = f"{left}{re.escape(lower_word)}{right}"
                    if re.search(pattern, lower):
                        return name
                else:
                    if lower_word in lower:
                        return name

    return ""


def _is_word_char(ch: str) -> bool:
    """파이썬 정규식 \\w가 매칭하는 ASCII 문자(영숫자/언더스코어)인지."""
    return ch.isalnum() or ch == "_"


def _detect_command(
    lower: str,
    project_name: str,
    projects: dict[str, ProjectConfig],
) -> str:
    """트리거 동사(해줘/돌려줘 등) 또는 슬래시 직접 입력이 있을 때만 command로 분류."""
    # 슬래시 명령 직접 입력: /command [args] (한국어 별칭도 지원)
    slash_match = _SLASH_COMMAND_RE.match(lower.strip())
    if slash_match:
        cmd = slash_match.group(1)
        if cmd in _COMMAND_ALIASES:
            return _COMMAND_ALIASES[cmd]
        if cmd in set(_COMMAND_ALIASES.values()):
            return cmd

    # 트리거 동사가 없으면 command 아님
    if not _COMMAND_TRIGGER_RE.search(lower):
        return ""

    for alias, command in _COMMAND_ALIASES.items():
        if alias in lower:
            return command
    return ""


def _extract_remaining_args(
    text: str,
    project_name: str,
    command: str,
    issue_id: str,
    projects: dict[str, ProjectConfig],
) -> str:
    """원본 텍스트에서 프로젝트명, 명령어, 이슈ID, 액션 키워드를 제거한 나머지."""
    remaining = text

    # 프로젝트명 제거
    remaining = re.sub(re.escape(project_name), "", remaining, flags=re.IGNORECASE)

    # description 키워드도 제거
    project_cfg = projects.get(project_name)
    if project_cfg and project_cfg.description:
        clean_desc = re.sub(r"[()（）\[\]「」]", " ", project_cfg.description)
        for word in clean_desc.split():
            if len(word) >= 2:
                remaining = re.sub(re.escape(word), "", remaining, flags=re.IGNORECASE)

    # 슬래시 명령어 prefix 제거 (/review, /harness 등)
    remaining = re.sub(r"/\w+", "", remaining)

    # 명령어 관련 키워드 제거
    for alias, cmd in _COMMAND_ALIASES.items():
        if cmd == command:
            remaining = re.sub(
                r"\b" + re.escape(alias) + r"\b", "", remaining, flags=re.IGNORECASE
            )

    # 이슈 ID 제거
    if issue_id:
        remaining = remaining.replace(issue_id, "")

    # 액션 키워드 제거 ("돌려줘", "실행해줘", "해줘" 등)
    remaining = re.sub(
        r"(돌려줘|실행해줘|실행해|해줘|해 줘|시작해|시작해줘|부탁해|좀)$",
        "",
        remaining,
    )

    return remaining.strip().strip(",.!? ")


# 셸 명령 실행 시 제거할 필러 키워드
_SHELL_FILLER_RE = re.compile(
    r"(프로젝트에서|에서|명령어|명령|백그라운드로|백그라운드|돌려줘|실행해줘|실행해|해줘|해 줘|해줘요|해주세요|시작해|시작해줘|부탁해|부탁해요|부탁드립니다|좀)"
)
# "결과는 ..." 이후 문장 제거 (마침표, 쉼표, ..  등 구분자 포함)
_TRAILING_COMMENT_RE = re.compile(r"[.。,，]+\s*결과는.*$")
# 여는 코드펜스에 이어 "언어\n"이 올 때만 언어 식별자로 간주해 제거.
# 개행이 없으면(```uv run ...) 첫 단어는 명령의 일부이므로 남긴다.
_CODE_FENCE_LANG_RE = re.compile(r"```[ \t]*\w+[ \t]*\r?\n")

# 셸 명령 hint — 명령 추출과 unknown_shell 감지에서 공통으로 사용한다.
# 슬래시 커맨드(`/stop` 등)와 충돌하지 않도록 `/`는 hint에 포함시키지 않는다.
_SHELL_CMD_HINTS = (
    "uv ", "python ", "python3 ", "npm ", "node ", "bash ", "sh ", "pip ",
    "pip3 ", "git ", "docker ", "make ", "cargo ", "go ", "java ",
    "cat ", "ls ", "echo ", "cd ", "mkdir ", "rm ", "cp ", "mv ",
    "./",
)


def _looks_like_shell_attempt(text: str) -> bool:
    """텍스트가 셸 명령 실행 시도로 보이면 True.

    감지 조건(둘 중 하나라도):
    - 어떤 줄이라도 셸 hint(`uv `, `python ` 등)로 시작 — 다중행 입력 대응
    - 어떤 줄에서 공백 뒤에 hint가 등장 — `"foobar에서 python -m pytest"`처럼
      프로젝트명이 앞에 오고 셸 명령이 뒤에 오는 단행 입력 대응

    hint는 모두 끝에 공백(`"python "`)을 포함하므로 `"python으로"` 같은 자연어
    어미와 충돌하지 않는다.
    """
    for line in text.split("\n"):
        # 코드펜스/인라인 백틱은 hint 매칭을 방해하므로 제거 후 판정한다.
        stripped = line.strip().strip("`").strip().lower()
        if any(stripped.startswith(hint) for hint in _SHELL_CMD_HINTS):
            return True
        if any(f" {hint}" in stripped for hint in _SHELL_CMD_HINTS):
            return True
    return False


def _extract_shell_command(
    text: str,
    project_name: str,
    projects: dict[str, ProjectConfig],
) -> str:
    """메시지에서 프로젝트명·필러를 제거하고 셸 명령 문자열을 추출."""
    remaining = text

    # 프로젝트명 제거
    remaining = re.sub(re.escape(project_name), "", remaining, flags=re.IGNORECASE)

    # description 키워드 제거
    project_cfg = projects.get(project_name)
    if project_cfg and project_cfg.description:
        clean_desc = re.sub(r"[()（）\[\]「」]", " ", project_cfg.description)
        for word in clean_desc.split():
            if len(word) >= 2:
                remaining = re.sub(
                    re.escape(word), "", remaining, flags=re.IGNORECASE
                )

    # "결과는 ..." 이후 문장 제거
    remaining = _TRAILING_COMMENT_RE.sub("", remaining)

    # 필러 키워드 제거
    remaining = _SHELL_FILLER_RE.sub("", remaining)

    # 코드블록/인라인 코드 펜스 제거.
    # 여는 펜스 뒤 "언어\n"이 오면 언어 식별자로 보고 함께 제거하지만,
    # 개행 없이 명령이 바로 붙는 경우(```uv run ...)는 명령을 보존한다.
    remaining = _CODE_FENCE_LANG_RE.sub("", remaining)
    remaining = remaining.replace("```", "").replace("`", "")

    # 셸 명령 판정을 방해하는 앞부분의 빈 줄이나 주석(#) 라인 제거.
    # 코드블록 첫 줄이 주석/빈 줄이면 아래 hint 검사(startswith)를 통과하지
    # 못해 question으로 오라우팅되어 1시간 안전 한계까지 헛도는 사고를 막는다.
    lines = remaining.split("\n")
    start_idx = len(lines)
    for i, line in enumerate(lines):
        stripped_line = line.strip()
        if stripped_line and not stripped_line.startswith("#"):
            start_idx = i
            break
    remaining = "\n".join(lines[start_idx:])

    remaining = remaining.strip().strip(",.!? ")

    # 너무 짧으면 셸 명령이 아님
    if len(remaining) < 3:
        return ""

    # 셸 명령처럼 보이는지 확인 — 알려진 커맨드 접두사 또는 경로 포함
    lower_remaining = remaining.lower()
    if not any(lower_remaining.startswith(hint) for hint in _SHELL_CMD_HINTS):
        return ""

    return remaining
