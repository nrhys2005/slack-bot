# CLAUDE.md

자연어 채팅으로 프로젝트별 Claude Code 명령어를 실행하고, @멘션/DM으로 상태 파악·위키 검색·DB 조회까지 할 수 있는 프로젝트 관리 Slack 봇.

## 기술 스택

- Python 3.11+, 패키지 관리: `uv`
- slack-bolt (AsyncApp) + Socket Mode
- Claude CLI (`claude -p`) — 단건 호출 기반 명령 실행 및 질문 답변
- PyYAML, python-dotenv
- 엔트리포인트: `slack_bot.main:main` (`uv run slack-bot`)

## 디렉터리 구조

```
slack_bot/
├── main.py            # 엔트리포인트. AsyncApp, TaskManager 생성, Socket Mode 시작
├── config.py          # ProjectConfig/DBConfig 데이터클래스, projects.yaml 로드
├── intent.py          # 자연어 인텐트 파싱 (command, status, question, task_control, db_query)
├── runner.py          # run_claude() — claude -p 비동기 서브프로세스 실행 (스트리밍)
├── handlers.py        # @멘션, DM 통합 메시지 핸들러 (즉시 실행 + 백그라운드)
├── chat.py            # Claude CLI로 태스크 출력 분석, 프로젝트 상태 파악, 질문 답변
├── db_query.py        # DB 프로젝트 모델 기반 자연어→SQL→psql 실행
├── task_manager.py    # TaskInfo/TaskManager — 실행 중 태스크 추적, 출력 누적
└── security.py        # 환경변수 화이트리스트, 출력 마스킹, 인증, rate limit, 감사 로깅
projects.yaml          # 프로젝트 → 경로/명령어/capabilities 매핑
.env                   # SLACK_BOT_TOKEN, SLACK_APP_TOKEN
pyproject.toml         # 의존성 및 스크립트 정의
```

## 아키텍처 흐름

### 채팅 기반 명령 실행 (즉시 백그라운드 실행)

```
"moment-some 하네스 MOM-43 돌려줘"
  → handlers.py: 메시지 수신 (app_mention 또는 DM)
  → intent.py: parse_intent() — type="command", project="moment-some", command="harness", args="MOM-43"
  → 즉시 시작 알림: ":rocket: moment-some /harness MOM-43 실행을 시작합니다. (태스크 ID: 001)"
  → TaskManager.create_task() — 태스크 ID 부여, 추적 시작
  → asyncio.create_task()로 백그라운드 실행
    → runner.py: claude -p "/harness MOM-43" (프로젝트별 MCP 도구 동적 구성)
    → stdout 라인별 스트리밍 → TaskInfo.output_lines에 누적
    → 완료 시 결과를 같은 스레드에 Block Kit 메��지로 전송
```

### 프로젝트 상태 파악

```
"ra-backend 상태 어때?"
  → intent.py: type="status", project="ra-backend"
  → chat.py: answer_question() — target_project의 코드/로그/설정을 Read/Glob/Grep으로 읽어 상태 파악
  → 답변 전송
```

### 질문 답변 (위키 검색 + 태스크 컨텍스트)

```
@bot 온보딩 절차 알려줘
  → intent.py: type="question"
  → chat.py: wiki 프로젝트 마크다운 검색 → Notion MCP 폴백 → 답변 전송
```

### 태스크 제어

```
"실행중인 태스크 보여줘" → task_control/list (자연어 허용)
"/stop"                  → task_control/list
"/stop 003"              → task_control/stop, args="003"
```

- 중단은 자연어("중단", "멈춰", "stop") 오매칭이 잦아 슬래시 `/stop <ID>` 전용
- 목록 조회는 자연어("태스크"/"task")로도 가능

- 명령 실행(`command`/`shell_exec`)뿐 아니라 질문 답변(`question`/`status`)과 DB 조회/추출(`db_query`)도 TaskManager에 등록되어 자연어로 중단 가능
- `TaskManager.stop_task()` → 백그라운드 `claude -p` 프로세스에 `terminate()` + `task.status="stopped"`
- 완료 후 `task.status == "stopped"`이면 결과 대신 ":octagonal_sign: 취소되었습니다." 응답
- `complete_task()`는 `stopped` 상태를 덮어쓰지 않음 (terminate→subprocess 자연 종료의 늦은 도착 보호)

### 자연어 DB 조회

```
"지난주 가입한 유저 수 조회해줘"
  → intent.py: type="db_query"
  → db_query.run_db_query(question, project) — DBConfig 기반 credentials 로드
  → claude -p로 SQL 생성·psql(또는 sqlite3) 실행
  → 답변 전송
```

### DB 조회 결과 CSV/Excel 내보내기

```
"지난주 가입한 유저 목록 추출해줘"
  → intent.py: type="db_query", export=True (추출/엑셀/csv/다운로드 등 키워드)
  → db_query.run_db_query_export(question, project)
    → Claude CLI가 psql/sqlite3 결과를 임시 CSV 파일로 저장
    → CSV → Excel 변환 (openpyxl)
  → handlers.py: files_upload_v2로 Excel 파일 업로드
  → 임시 파일 정리
```

## 주요 모듈 상세

### config.py
- `DBConfig(db_type, env_file, env_prefix, model_paths, db_path)` — 프로젝트별 DB 접속 설정
  - `db_type`: "postgresql" (기본) 또는 "sqlite"
  - `db_path`: SQLite DB 파일 경로 (프로젝트 루트 기준 상대경로)
- `ProjectConfig(name, path, commands, description, wiki, db, mcp_tools, status_paths)` — 프로젝트 설정
- `load_projects()`: `projects.yaml` 읽어서 `AppConfig(projects, security)` 반환
- 하위호환: `db_backend: true` → `DBConfig` 자동 변환, `mcp_tools` 미설정 시 기본 MCP 제공
- 프로젝트별 capabilities:
  - `wiki: true` — 위키 검색 소스 (복수 가능)
  - `db: {...}` — DB 조회 설정 (db_type, env_file, env_prefix, model_paths, db_path)
  - `mcp_tools: [jira_*, ...]` — Claude CLI에 전달할 MCP 도구 패턴
  - `status_paths: [logs/, ...]` — 상태 파악 시 읽을 경로
  - `description` — 채팅에서 프로젝트 식별용 키워드

### intent.py
- `parse_intent(text, projects) -> Intent` — 규칙 기반 인텐트 파싱
- Intent 타입: command, status, question, task_control, db_query (export 플래그 포함), admin
- 프로젝트명 매칭: 이름 직접 매칭 → description 키워드 매칭
- 명령어 매핑: 한국어 ("하네스", "리뷰") → 영문 ("harness", "review")
- 이슈 ID (`[A-Z]+-\d+`), 태스크 ID (`\d{3}`) 자동 추출
- admin 명령:
  - `_ADMIN_KEYWORDS` — 자연어 매칭 (`claude 로그인`, `claude 설치` 등)
  - `_SLASH_ADMIN_COMMANDS` — 슬래시 전용 매칭 (`/restart`). "재시작"은 일상 대화 오매칭이 잦아 슬래시로만 트리거
- task_control:
  - `/stop [task_id]` — 슬래시 전용 중단/목록 (자연어 "중단/멈춰/stop" 매칭 제거)
  - `_TASK_LIST_KEYWORDS` (`태스크`, `task`) — 자연어 목록 조회만 허용

### runner.py
- `_build_allowed_tools(project)` — 프로젝트 mcp_tools 기반 동적 도구 목록 생성
- `run_claude(project, command, args, task) -> RunResult`
- `claude -p "/<command> <args>" --output-format text --allowedTools <동적>` 실행
- stdout 라인별 스트리밍 → `task.output_lines`에 누적
- 타임아웃 3600초, 출력 3900자 제한

### handlers.py
- `register_handlers(app, task_manager)`: 앱 시작 시 프로젝트 로드
- `app_mention` / `message(DM)` → 통합 `_handle_message()` → 인텐트별 분기
- 인텐트별 처리:
  - command → 즉시 백그라운드 실행 + 시작 알림 → 완료 시 같은 스레드에 결과 전송
  - task_control → 태스크 목록/중단
  - db_query → db_query.py로 위임
  - status/question → chat.py로 위임
- `confirm_execute` / `cancel_execute` 액션 핸들러 (레거시 호환용)

### chat.py
- `answer_question(question, tasks, thread_history, projects, target_project, on_progress, task)` — 질문 답변
  - `task` 인자: TaskInfo 전달 시 서브프로세스 핸들을 등록해 `stop_task()`로 중단 가능
- `_build_system_prompt(target_project, wiki_projects, db_instructions)` — 동적 시스템 프롬프트
- target_project에 따라 CWD, 도구, 프롬프트가 달라짐
- status_paths 설정된 프로젝트는 해당 경로의 코드/로그 읽기

### db_query.py
- `run_db_query(question, project, wiki_path, task=None)` — 자연어 DB 조회 (PostgreSQL + SQLite)
- `run_db_query_export(question, project, wiki_path, task=None)` — CSV/Excel 내보내기
  - 두 함수 모두 `task` 인자로 TaskInfo를 받으면 서브프로세스를 등록해 자연어 중단(`{ID}번 중단`)을 지원
  - Claude CLI가 psql/sqlite3 결과를 임시 CSV로 저장 → `_csv_to_excel()`로 Excel 변환
  - `ExportResult(summary, excel_path, error)` 반환
- `_load_db_env(project)` — DBConfig.env_prefix 기반 credentials 로드 (PostgreSQL 전용)
- `build_db_instructions(db_envs, model_paths)` — 동적 DB 접속정보·규칙 생성
- `build_sqlite_db_instructions(project)` — SQLite용 DB 접속정보 생성
- PGPASSWORD 환경변수: `PGPASSWORD_{논리명.upper()}` 패턴
- 안전장치 (다층 방어):
  1. (근본) DB 유저 자체를 read-only 계정으로 사용 권장
  2. 프롬프트에서 SELECT 외 금지 명시
  3. `BEGIN; SET TRANSACTION READ ONLY; ... ROLLBACK;` 래핑 (PostgreSQL)
  4. `LIMIT 100` 강제 (내보내기는 `LIMIT 10000`)
  5. stdout 256KB 상한 (OOM 방어)

## Goal-Driven Execution

태스크를 구현 전에 검증 가능한 목표로 변환한다.

- "버그 수정" → 재현하는 테스트 작성 → 테스트 통과시키기
- "기능 추가" → 성공 기준 정의 → 구현 → 테스트/린트 통과 확인
- "리팩터링" → 기존 테스트 통과 확인 → 변경 → 테스트 재확인

다단계 작업 시 간단한 계획을 먼저 제시한다:
1. [단계] → verify: [확인 방법]
2. [단계] → verify: [확인 방법]
3. [단계] → verify: [확인 방법]

명확한 성공 기준이 있어야 독립적으로 진행할 수 있다. 모호한 기준("되게 해줘")은 지속적인 확인이 필요하다.

## 개발 참고사항

- 전체 비동기 구조: `AsyncApp`, `asyncio.create_task()`, `asyncio.create_subprocess_exec()`
- 내부 실행은 `claude -p` 단건 호출 (대화형 세션 아님)
- 프로젝트 목록은 앱 시작 시 1회 로드됨 (런타임 중 projects.yaml 변경 반영 안 됨)
- 태스크는 in-memory 관리 — 봇 재시작 시 초기화됨
- 각 대상 프로젝트에 `.claude/` 하네스 설정과 `.claude/settings.local.json` 도구 권한 필요

## Slack App 필요 설정

- **Socket Mode** 활성화
- **Interactivity** 활성화 (Socket Mode에서 자동, 확인 버튼용)
- **Event Subscriptions** → Subscribe to bot events: `app_mention`, `message.im`
- **Bot Token Scopes**: `chat:write`, `files:write`, `app_mentions:read`, `channels:history`, `groups:history`, `mpim:history`, `im:history`, `reactions:write` — 스코프 추가 후 앱 재설치 필요
