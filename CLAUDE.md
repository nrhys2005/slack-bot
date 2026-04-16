# CLAUDE.md

Slack에서 프로젝트별 Claude Code 하네스 명령어를 실행하고, @멘션으로 진행상황 질문·Notion 위키 검색, `/db`로 `db_backend: true` 프로젝트의 SQLAlchemy 모델 기반 자연어 DB 조회까지 할 수 있는 대화형 봇.

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
├── handlers.py        # /dev, /claude, /projects, /stop, /db, @멘션 핸들러
├── chat.py            # Claude CLI로 태스크 출력 분석 및 질문 답변
├── db_query.py        # /db — db_backend 프로젝트 모델 기반 자연어→SQL→psql 실행
└── task_manager.py    # TaskInfo/TaskManager — 실행 중 태스크 추적, 출력 누적
projects.yaml          # 프로젝트 → 경로/허용 명령어 매핑
.env                   # SLACK_BOT_TOKEN, SLACK_APP_TOKEN
pyproject.toml         # 의존성 및 스크립트 정의
```

## 아키텍처 흐름

### harness 단축 실행

```
/dev <project> <issue>
  → handlers.py: harness 명령어 고정, 프로젝트 검증 (harness 허용 여부)
  → TaskManager.create_task() — 태스크 ID 부여, 추적 시작
  → ack() 즉시 응답 (태스크 ID 포함)
  → asyncio.create_task()로 백그라운드 실행
    → runner.py: asyncio.create_subprocess_exec("claude", "-p", "/harness <issue>", ...)
    → stdout 라인별 스트리밍 읽기 → TaskInfo.output_lines에 누적
    → 완료 시 결과를 Slack Block Kit 메시지로 채널에 전송
```

### 범용 명령어 실행

```
/claude <project> <command> [args]
  → handlers.py: 입력 파싱 & 검증 (프로젝트 존재, 명령어 허용 여부)
  → 이하 동일한 태스크 생성 및 백그라운드 실행 흐름
```

### @멘션 질문 (진행상황 파악 + 위키 검색)

```
@bot 지금 어디까지 됐어?
@bot 온보딩 절차가 어떻게 돼?
  → handlers.py: app_mention 이벤트 수신
  → 👀 리액션 추가 (읽었다는 즉시 피드백)
  → TaskManager.get_tasks_for_channel() — 해당 채널의 태스크 조회
  → 스레드 내 메시지면 conversations.replies로 이전 대화 이력 조회
  → chat.py: 항상 위키 도구 허용 + 태스크 컨텍스트(있으면) 포함
    → Claude가 질문/대화 내용을 보고 태스크 분석 or 위키 검색 자동 판단
    → 위키 검색 시: 로컬 마크다운(Glob/Grep/Read) 우선 → 못 찾으면 Notion MCP 폴백
  → 👀 리액션 제거, 스레드로 답변 전송
```

### 태스크 중단

```
/stop <task_id>
  → TaskManager.stop_task() — process.terminate() 호출
```

### 자연어 DB 조회

```
/db <자연어 질문>
  → handlers.py: 질문 파싱, projects.yaml에서 `db_backend: true` 프로젝트 탐색
  → ack() 즉시 응답 + `:mag: 조회 중...` 안내
  → asyncio.create_task()로 백그라운드 실행
    → db_query.run_db_query(question, db_backend_path, wiki_path)
      → {db_backend_path}/app/.env 에서 POSTGRESQL_* 자격증명 로드
      → 시스템 프롬프트에 접속 정보 + SELECT-only + LIMIT 100 + read-only 계정 권장 주입
      → claude -p 서브프로세스 실행 (cwd=db_backend_path)
        - Claude가 app/models/{ra,core}/*.py 를 Read/Grep해 스키마 파악
        - 필요 시 위키 디렉토리에서 도메인 용어 탐색
        - BEGIN; SET TRANSACTION READ ONLY; <SELECT ... LIMIT 100>; ROLLBACK; 로 래핑해 psql 실행
      → stdout을 라인 단위 스트리밍으로 읽으며 256KB 상한 감시 (초과 시 프로세스 kill)
      → MAX_OUTPUT_LENGTH 로 truncate
    → 결과를 Block Kit 메시지로 채널에 전송
```

## 주요 모듈 상세

### main.py
- `load_dotenv()`로 `.env` 로드
- `TaskManager()` 인스턴스 생성
- `AsyncApp(token=SLACK_BOT_TOKEN)` 생성
- `register_handlers(app, task_manager)` 호출 후 `AsyncSocketModeHandler`로 시작

### config.py
- `ProjectConfig(name, path, commands, wiki, db_backend)` 데이터클래스
- `load_projects()`: `projects.yaml` 읽어서 `dict[str, ProjectConfig]` 반환
- 환경변수 `PROJECTS_CONFIG`로 설정 파일 경로 오버라이드 가능
- 플래그 의미:
  - `wiki: true` — @멘션 질문 답변 시 Notion/로컬 위키 검색 소스 (전체 중 1개만)
  - `db_backend: true` — `/db` 커맨드에서 SQLAlchemy 모델·`app/.env` 자격증명 소스 (전체 중 1개만)

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
- Slack은 비대화형이므로 `--allowedTools`를 항상 자동 적용 (사용자 `--auto` 플래그 불필요)
- **stdout 라인별 스트리밍**: `async for line in proc.stdout` → `task.output_lines`에 누적
- 출력 초과 시 truncate 처리
- `ANTHROPIC_API_KEY`를 서브프로세스 환경에서 제거하여 Claude Code OAuth 인증 사용

### handlers.py
- `register_handlers(app, task_manager)`: 앱 시작 시 `load_projects()` 1회 호출
- `/dev`: harness 단축 명령어. 프로젝트 + 이슈명만 받아 harness 실행
- `/claude`: 범용 명령어. 프로젝트 + 명령어 + args 받아 실행
- `/projects`: 등록된 프로젝트 및 허용 명령어 목록 반환
- `/stop`: 태스크 ID로 프로세스 중단, ID 없으면 실행 중 태스크 목록 표시
- `app_mention`: @멘션 텍스트에서 질문 추출 → 스레드 대화 이력 조회 → `chat.answer_question()` 호출 → 스레드 답변
- `_run_and_report()`: 실행 결과를 Block Kit으로 포맷하여 채널에 전송. 실패 시 재실행 편의를 위해 원본 슬래시 명령어(`/dev ...` 또는 `/claude ...`)를 함께 표시

### chat.py
- `answer_question(question, tasks, thread_history, wiki_project_path)`: 태스크 출력 최근 100줄 + 스레드 대화 이력을 컨텍스트로 Claude CLI 호출
- `wiki_project_path` 설정 시 위키 프로젝트 디렉토리에서 실행, Notion MCP 도구(`--allowedTools`) 허용
- 항상 위키 도구 허용 (로컬 Glob/Grep/Read 우선, Notion MCP 폴백), 태스크 컨텍스트는 있을 때만 포함
- `claude -p` 서브프로세스로 실행 (OAuth 인증 사용)

### db_query.py
- `run_db_query(question, db_backend_path, wiki_path)`: 자연어 질문을 받아 Claude CLI로 SQL 생성·실행
- `_load_db_env(db_backend_path)`: `{db_backend_path}/app/.env` 에서 `POSTGRESQL_RA_*`/`POSTGRESQL_CORE_*` 키만 추출, 누락 시 `DBEnvError`
- `_build_system_prompt(db_env, wiki_path)`: ra/core 접속 정보·스키마·SELECT-only 규칙·psql 사용 예시를 포함한 시스템 프롬프트 생성. read-only 계정 사용을 근본 방어로 명시
- 서브프로세스 환경에 `PGPASSWORD_RA`, `PGPASSWORD_CORE` 를 주입해 Claude가 psql 실행 시 참조
- `--allowedTools "Read,Glob,Grep,Bash(psql:*)"` 로 도구 범위를 DB 조회 용도로 한정
- stdout은 라인 단위로 스트리밍하며 누적 바이트가 `_MAX_STDOUT_BYTES` (256KB) 를 넘으면 프로세스를 `kill()` 해 OOM 방어
- 안전장치 (다층 방어):
  1. (근본) DB 유저 자체를 DB 레벨 read-only 계정으로 쓰도록 문서에 강력 권장
  2. 프롬프트에서 SELECT 외 금지 명시
  3. 실행 SQL을 `BEGIN; SET TRANSACTION READ ONLY; ... ROLLBACK;` 으로 래핑
  4. 모든 SELECT에 `LIMIT 100` 강제 (결과량 제한)
  5. stdout 256KB 상한 (OOM 방어)

## 개발 참고사항

- 전체 비동기 구조: `AsyncApp`, `asyncio.create_task()`, `asyncio.create_subprocess_exec()`
- Slack 응답 타임아웃 회피를 위해 `ack()` 즉시 호출 후 백그라운드 태스크로 처리
- 프로젝트 목록은 앱 시작 시 1회 로드됨 (런타임 중 projects.yaml 변경 반영 안 됨)
- 태스크는 in-memory 관리 — 봇 재시작 시 초기화됨
- 각 대상 프로젝트에 `.claude/` 하네스 설정과 `.claude/settings.local.json` 도구 권한 필요

## Slack App 필요 설정

- **Socket Mode** 활성화
- **Slash Commands**: `/dev`, `/claude`, `/projects`, `/stop`, `/db`
- **Event Subscriptions** → Subscribe to bot events: `app_mention`
- **Bot Token Scopes**: `commands`, `chat:write`, `app_mentions:read`, `channels:history` (public 채널), `groups:history` (private 채널), `mpim:history` (그룹 DM), `im:history` (1:1 DM), `reactions:write` (응답 중 리액션 표시용) — 스코프 추가 후 앱 재설치 필요
