"""SSH로 원격 운영 서버를 관리하기 위한 명령 조립과 프롬프트 안내."""

from __future__ import annotations

import shlex

from slack_bot.config import ProjectConfig, RemoteConfig

SSH_CONNECT_TIMEOUT = 10  # 초. 서버가 꺼져 있을 때 오래 매달리지 않게 한다

# 비대화형 ssh 세션은 로그인 셸 설정(.zprofile 등)을 읽지 않아 PATH 가 짧다.
# macOS(Homebrew)와 리눅스에서 흔한 경로를 앞에 보충한다.
_REMOTE_PATH_PREFIX = "/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin"


def ssh_base_args(remote: RemoteConfig) -> list[str]:
    """비밀번호 프롬프트 없이 실패하도록 BatchMode 를 건 ssh 기본 인자."""
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        remote.host,
    ]


def build_remote_shell_args(remote: RemoteConfig, command: str) -> list[str]:
    """원격 서버의 프로젝트 경로에서 command 를 실행하는 ssh argv 를 만든다."""
    script = f'export PATH="{_REMOTE_PATH_PREFIX}:$PATH"; '
    if remote.path:
        script += f"cd {shlex.quote(remote.path)} && "
    script += command
    return [*ssh_base_args(remote), script]


def build_remote_instructions(project: ProjectConfig) -> str:
    """claude -p 에게 원격 운영 서버 접근 방법을 알려 주는 안내문. remote 가 없으면 빈 문자열."""
    remote = project.remote
    if remote is None:
        return ""
    ssh_cmd = " ".join(ssh_base_args(remote))
    lines = [
        f"## 원격 운영 서버 ({project.name})",
        f"- 이 프로젝트의 운영 서버는 원격 호스트 `{remote.host}` 에서 돈다. "
        f"로컬 경로 `{project.path}` 는 코드 사본이고, 실제 서비스 상태, 로그, 프로세스는 원격에 있다.",
        f"- 원격 명령은 Bash 로 `{ssh_cmd} '<명령>'` 형태로 실행한다. "
        "BatchMode 라 비밀번호를 묻지 않고 실패하니, 인증 실패면 그대로 보고한다.",
    ]
    if remote.path:
        lines.append(f"- 원격 프로젝트 경로: `{remote.path}`")
    if remote.notes:
        lines.append(f"- 운영 메모: {remote.notes.strip()}")
    lines.append(
        "- 서비스 재시작, 중지, 배포, 파일 삭제처럼 운영에 영향을 주는 명령은 "
        "사용자가 명시적으로 요청했을 때만 실행하고, 실행한 명령과 결과를 답변에 적는다."
    )
    return "\n".join(lines) + "\n"
