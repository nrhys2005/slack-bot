from __future__ import annotations

import re
from dataclasses import dataclass

from slack_bot.config import ProjectConfig


@dataclass
class Intent:
    """사용자 메시지에서 파싱된 의도."""

    type: str  # "command" | "status" | "question" | "task_control" | "db_query"
    project: str = ""  # 식별된 프로젝트명
    command: str = ""  # 실행할 명령어 또는 task_control 동작 (list/stop)
    args: str = ""  # 명령어 인자
    raw_text: str = ""  # 원본 메시지


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

_TASK_CONTROL_KEYWORDS: dict[str, str] = {
    "중단": "stop",
    "멈춰": "stop",
    "stop": "stop",
    "태스크": "list",
    "task": "list",
    "실행중": "list",
    "실행 중": "list",
}

_STATUS_KEYWORDS = frozenset({
    "상태", "status", "어때", "어떻게", "현황", "돌아가",
})

DB_KEYWORDS = frozenset({
    "조회", "쿼리", "query", "몇 건", "몇건", "통계",
    "테이블", "스키마", "DB", "db", "수치",
})

# 이슈 ID (e.g. MOM-43, PROJ-123)
_ISSUE_ID_RE = re.compile(r"\b([A-Z]+-\d+)\b")
# 태스크 ID (e.g. 003, 012) — 한국어 접미사("번") 허용
_TASK_ID_RE = re.compile(r"(\d{3})(?:번)?")



def parse_intent(
    text: str,
    projects: dict[str, ProjectConfig],
) -> Intent:
    """사용자 메시지에서 의도를 규칙 기반으로 파싱한다."""
    normalized = text.strip()
    lower = normalized.lower()

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

    # 5. DB 조회 감지 (DB 프로젝트가 존재할 때만)
    has_db_project = any(p.db is not None for p in projects.values())
    if has_db_project and any(kw in lower for kw in DB_KEYWORDS):
        return Intent(
            type="db_query",
            project=matched_project or "",
            raw_text=normalized,
        )

    # 6. 상태 조회 감지 (프로젝트 특정됨)
    if matched_project and any(kw in lower for kw in _STATUS_KEYWORDS):
        return Intent(
            type="status",
            project=matched_project,
            raw_text=normalized,
        )

    # 7. 기본: 일반 질문
    return Intent(
        type="question",
        project=matched_project or "",
        raw_text=normalized,
    )


def _detect_task_control(text: str, lower: str) -> Intent | None:
    """태스크 제어(중단/목록) 인텐트 감지."""
    for keyword, action in _TASK_CONTROL_KEYWORDS.items():
        if keyword in lower:
            if action == "stop":
                # 태스크 ID 추출
                task_id_match = _TASK_ID_RE.search(text)
                return Intent(
                    type="task_control",
                    command="stop",
                    args=task_id_match.group(1) if task_id_match else "",
                    raw_text=text,
                )
            else:
                return Intent(
                    type="task_control",
                    command="list",
                    raw_text=text,
                )
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

    # description 키워드 매칭 (3글자 이상 단어만, 짧은 단어의 오매칭 방지)
    # 괄호, 특수문자 제거 후 매칭
    for name, cfg in projects.items():
        if cfg.description:
            clean_desc = re.sub(r"[()（）\[\]「」]", " ", cfg.description)
            desc_words = [w for w in clean_desc.split() if len(w) >= 2]
            for word in desc_words:
                if word.lower() in lower:
                    return name

    return ""


def _detect_command(
    lower: str,
    project_name: str,
    projects: dict[str, ProjectConfig],
) -> str:
    """메시지에서 명령어 키워드를 감지하고, 프로젝트의 허용 명령어와 매칭."""
    for alias, command in _COMMAND_ALIASES.items():
        if alias in lower:
            # 프로젝트가 식별되었으면 해당 프로젝트의 허용 명령어 확인
            if project_name and project_name in projects:
                if command in projects[project_name].commands:
                    return command
            elif not project_name:
                # 프로젝트 미식별이면 명령어만 반환
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
                remaining = re.sub(
                    re.escape(word), "", remaining, flags=re.IGNORECASE
                )

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
        r"(돌려줘|실행해줘|실행해|해줘|해 줘|시작해|시작해줘|부탁해|좀)$", "",
        remaining,
    )

    return remaining.strip().strip(",.!? ")
