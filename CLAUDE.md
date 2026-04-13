# CLAUDE.md

Slack에서 프로젝트별 Claude Code 하네스 명령어를 실행하고, @멘션으로 진행상황을 질문할 수 있는 대화형 봇.

## 기술 스택

- Python 3.11+, 패키지 관리: `uv`
- slack-bolt (AsyncApp) + Socket Mode
- Claude CLI (`claude -p`) — @멘션 질문 답변용
- PyYAML, python-dotenv
- 엔트리포인트: `slack_bot.main:main` (`uv run slack-bot`)

## 디렉터리 구조

```
slack_bot/
├── main.py            # 엔트리포인트. AsyncApp, TaskManager 생성, Socket Mode 시작
├── config.py          # ProjectConfig 데이터클래스, projects.yaml 로드
├── runner.py          # run_claude() — claude -p 비동기 서브프로세스 실행 (스트리밍)
├── handlers.py        # /claude, /claude-projects, /claude-stop, @멘션 핸들러
├── chat.py            # Claude CLI로 태스크 출력 분석 및 질문 답변
└── task_manager.py    # TaskInfo/TaskManager — 실행 중 태스크 추적, 출력 누적
projects.yaml          # 프로젝트 → 경로/허용 명령어 매핑
.env                   # SLACK_BOT_TOKEN, SLACK_APP_TOKEN
pyproject.toml         # 의존성 및 스크립트 정의
```

## 아키텍처 흐름

### 명령어 실행

```
/claude <project> <command> [args]
  → handlers.py: 입력 파싱 & 검증 (프로젝트 존재, 명령어 허용 여부)
  → TaskManager.create_task() — 태스크 ID 부여, 추적 시작
  → ack() 즉시 응답 (태스크 ID 포함)
  → asyncio.create_task()로 백그라운드 실행
    → runner.py: asyncio.create_subprocess_exec("claude", "-p", ...)
    → stdout 라인별 스트리밍 읽기 → TaskInfo.output_lines에 누적
    → 완료 시 결과를 Slack Block Kit 메시지로 채널에 전송
```

### @멘션 질문 (진행상황 파악)

```
@bot 지금 어디까지 됐어?
  → handlers.py: app_mention 이벤트 수신
  → TaskManager.get_tasks_for_channel() — 해당 채널의 태스크 조회
  → chat.py: 누적 출력을 Claude CLI에 전달, 자연어 답변 생성
  → 스레드로 답변 전송
```

### 태스크 중단

```
/claude-stop <task_id>
  → TaskManager.stop_task() — process.terminate() 호출
```

## 주요 모듈 상세

### main.py
- `load_dotenv()`로 `.env` 로드
- `TaskManager()` 인스턴스 생성
- `AsyncApp(token=SLACK_BOT_TOKEN)` 생성
- `register_handlers(app, task_manager)` 호출 후 `AsyncSocketModeHandler`로 시작

### config.py
- `ProjectConfig(name, path, commands)` 데이터클래스
- `load_projects()`: `projects.yaml` 읽어서 `dict[str, ProjectConfig]` 반환
- 환경변수 `PROJECTS_CONFIG`로 설정 파일 경로 오버라이드 가능

### task_manager.py
- `TaskInfo` 데이터클래스: task_id, project_name, command, args, user, channel, status, output_lines, process
  - `elapsed_display` 프로퍼티: 경과시간 한국어 표시
  - `output_text` 프로퍼티: 누적 출력 합치기
- `TaskManager`: in-memory 태스크 저장소
  - `create_task()` → 자동 ID 부여 (001, 002, ...)
  - `get_tasks_for_channel()` → 실행 중 태스크 우선, 없으면 최근 10분 내 완료 태스크
  - `stop_task()` → process.terminate()
  - `cleanup_old()` → 완료 후 30분 경과 태스크 제거

### runner.py
- `MAX_OUTPUT_LENGTH = 3900` (Slack 메시지 제한 ~4000자)
- `run_claude(project, command, args, task) -> RunResult`
- `claude -p "/<command> <args>" --output-format text` 실행
- `--auto` 플래그 지원: args에서 분리하여 `--allowedTools`로 변환
- **stdout 라인별 스트리밍**: `async for line in proc.stdout` → `task.output_lines`에 누적
- 출력 초과 시 truncate 처리
- `ANTHROPIC_API_KEY`를 서브프로세스 환경에서 제거하여 Claude Code OAuth 인증 사용

### handlers.py
- `register_handlers(app, task_manager)`: 앱 시작 시 `load_projects()` 1회 호출
- `/claude`: 입력 파싱 → 검증 → TaskManager에 등록 → 백그라운드 실행
- `/claude-projects`: 등록된 프로젝트 및 허용 명령어 목록 반환
- `/claude-stop`: 태스크 ID로 프로세스 중단, ID 없으면 실행 중 태스크 목록 표시
- `app_mention`: @멘션 텍스트에서 질문 추출 → `chat.answer_question()` 호출 → 스레드 답변
- `_run_and_report()`: 실행 결과를 Block Kit으로 포맷하여 채널에 전송

### chat.py
- `answer_question(question, tasks)`: 태스크 출력 최근 100줄을 컨텍스트로 Claude CLI 호출
- `claude -p` 서브프로세스로 실행 (OAuth 인증 사용)
- 시스템 프롬프트로 간결한 한국어 답변 유도

## 개발 참고사항

- 전체 비동기 구조: `AsyncApp`, `asyncio.create_task()`, `asyncio.create_subprocess_exec()`
- Slack 응답 타임아웃 회피를 위해 `ack()` 즉시 호출 후 백그라운드 태스크로 처리
- 프로젝트 목록은 앱 시작 시 1회 로드됨 (런타임 중 projects.yaml 변경 반영 안 됨)
- 태스크는 in-memory 관리 — 봇 재시작 시 초기화됨
- 각 대상 프로젝트에 `.claude/` 하네스 설정과 `.claude/settings.local.json` 도구 권한 필요

## Slack App 필요 설정

- **Socket Mode** 활성화
- **Slash Commands**: `/claude`, `/claude-projects`, `/claude-stop`
- **Event Subscriptions** → Subscribe to bot events: `app_mention`
- **Bot Token Scopes**: `commands`, `chat:write`, `app_mentions:read`
