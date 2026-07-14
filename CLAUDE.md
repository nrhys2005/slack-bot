# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

자연어 채팅으로 프로젝트별 Claude Code 명령어를 실행하고, @멘션/DM으로 상태 파악·위키 검색·DB 조회까지 할 수 있는 프로젝트 관리 Slack 봇.

## 기술 스택

- Python 3.11+, 패키지 관리: `uv`
- slack-bolt (AsyncApp) + Socket Mode
- Claude CLI (`claude -p`) — 단건 호출 기반 명령 실행 및 질문 답변
- PyYAML, python-dotenv
- 엔트리포인트: `slack_bot.main:main` (`uv run slack-bot`)

## 개발 명령어

```bash
uv sync                                          # 의존성 설치 (dev 그룹 포함)
uv run slack-bot                                 # 봇 실행 (Socket Mode 연결)
uv run pytest                                    # 전체 테스트
uv run pytest tests/test_intent_routing.py       # 파일 단위 테스트
uv run pytest tests/test_task_manager.py -k stop # 단일 테스트 (-k 패턴)
```

- 비동기 테스트는 pytest-asyncio strict 모드 — 각 테스트에 `@pytest.mark.asyncio` 데코레이터 필요
- 테스트는 Slack/Claude CLI 실제 연결 없이 동작 (모킹 기반, 전체 1초 미만)
- 실행에는 `.env`(Slack 토큰)와 `projects.yaml`이 필요 — 각각 `.env.example`, `projects.yaml.example` 참고

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
└── security.py        # 서브프로세스 env 생성, 출력 마스킹, 인증, rate limit, 감사 로깅
tests/                 # pytest 테스트 (인텐트 라우팅, 태스크 매니저, 핸들러/서브프로세스 안정성, 인증 로그인)
.claude/               # 이 저장소 자체의 하네스 설정 (skills: harness/plan/develop/review, agents)
projects.yaml          # 프로젝트 → 경로/명령어/capabilities 매핑 (gitignore — example 참고)
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
    → 완료 시 결과를 같은 스레드에 Block Kit 메시지로 전송
```

### 프로젝트 상태 파악 / 질문 답변 (항상 백그라운드)

질문/상태 인텐트는 명령 실행과 동일하게 즉시 시작 알림을 보내고 백그라운드 태스크로 처리된다. 안전 한계 1시간(`CHAT_SAFETY_TIMEOUT = 3600`) 안에서 동작하며 `/stop {ID}`로 언제든 취소 가능.

```
"ra-backend 상태 어때?"  /  @bot 온보딩 절차 알려줘
  → intent.py: type="status" 또는 type="question"
  → handlers._handle_question_intent
    → TaskManager.create_task("chat") — 추적 시작
    → 즉시 시작 알림: ":mag: 질문 처리를 시작합니다. (ID: NNN, 취소: `/stop NNN`)"
      — 알림 메시지의 ts를 보관해두고, 답변/취소/에러 메시지를 보낸 직후
        `chat.chat_delete`로 삭제해 채널에 흔적을 남기지 않는다 (best-effort).
    → asyncio.create_task()로 _run_chat_question_and_report 백그라운드 실행
      → chat.answer_question() — target_project 코드/로그/위키/DB를 읽어 답변 생성
      → 완료 시 같은 스레드에 결과 메시지 전송 후 시작 알림 삭제
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
- 태스크 타입(`create_task` 2번째 인자): 명령 실행은 커맨드명(`harness` 등), 그 외 `"shell"`, `"chat"`, `"db"`, `"db_export"`
- `cleanup_old(max_age=1800)` — 완료 후 30분 지난 태스크는 목록에서 제거

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
- 진입 즉시 `html.unescape()`로 정규화: Slack은 `&`, `<`, `>`를 HTML 엔티티(`&amp;` `&lt;` `&gt;`)로 이스케이프해서 보낸다. 원복하지 않으면 `git pull ... && uv sync`가 셸에 `&amp;&amp;`로 전달돼 `/bin/sh: Syntax error: "&" unexpected`로 실패하고, 리다이렉션(`>`,`<`)이 든 명령도 깨진다 (멘션 `<@U...>`는 리터럴 꺾쇠라 영향 없음)
- Intent 타입: command, shell_exec, status, question, task_control, db_query (export 플래그 포함), admin, unknown_shell
- `unknown_shell`: 트리거 동사 + 셸 hint는 있는데 프로젝트가 매칭 안 된 케이스. 곧바로 에러 응답으로 차단하지 않으면 question으로 흘러가 `claude -p`가 다중행 셸 명령을 "질문"으로 받아 1시간 안전 한계까지 헛돈다
  - 셸 hint 목록 `_SHELL_CMD_HINTS`: `uv `, `python `, `npm `, `git `, `docker `, `make `, `ls `, `./` 등 (전체는 intent.py 참조)
- 코드펜스 처리: `_looks_like_shell_attempt`는 백틱을 벗겨낸 뒤 hint 판정, `_extract_shell_command`는 코드펜스 제거(여는 펜스 뒤 "언어\n"만 언어 식별자로 제거, `` ```uv run `` 처럼 개행 없이 붙으면 명령 보존) 후 앞쪽 빈 줄/`#` 주석 라인을 건너뛰고 명령 추출 — 코드블록으로 감싼 셸 명령이 question으로 새는 오라우팅 방지
- description 키워드 매칭: ASCII 단어는 단어 경계(`\b`) 요구. `"RA"` 같은 2글자 약어가 `"trader"`의 부분 문자열에 매칭되어 엉뚱한 프로젝트로 라우팅되는 사고 방지. 한국어/CJK는 단어 경계 개념이 모호하므로 부분 문자열 매칭 유지
- 프로젝트명 매칭: 이름 직접 매칭 → description 키워드 매칭
- 명령어 매핑: 한국어 ("하네스", "리뷰") → 영문 ("harness", "review")
- 이슈 ID (`[A-Z]+-\d+`), 태스크 ID (`\d{3}`) 자동 추출
- admin 명령:
  - `_ADMIN_KEYWORDS` — 자연어 매칭. `auth_login`(`claude 로그인`/`클로드 로그인`/`claude login`/`claude auth`), `install_claude`(`claude 설치`/`클로드 설치`/`install claude`)
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
- 수신 즉시 :eyes: 리액션 추가 ('읽는 중' 표시). 동기 응답 인텐트는 처리 후 바로 제거하고, 백그라운드로 위임된 인텐트는 해당 백그라운드 태스크가 완료/실패/취소 시점에 `_remove_eyes_reaction()`으로 제거 (best-effort)
- **슬래시 커맨드** `/restart`, `/stop`: Slack에 정식 등록된 슬래시 커맨드. `app_mention`/`message`가 아닌 `slash_commands` 페이로드로 도착하므로 별도 `@app.command(...)` 핸들러로 처리한다. 내부에서 `_handle_admin_intent`/`_handle_task_control`로 위임
- 인텐트별 처리(모두 즉시 시작 알림 + 백그라운드 실행):
  - command → `_run_and_report` (`run_claude`)
  - shell_exec → `_run_shell_and_report`
  - status/question → `_run_chat_question_and_report` (`chat.answer_question`)
  - db_query → `_run_db_query_and_report` 또는 `_run_db_query_export_and_report`
  - task_control → 태스크 목록/중단 (동기 응답)
- 동시성 제한 세마포어 (각 3개, `MAX_CONCURRENT_CHAT`/`MAX_CONCURRENT_TASK`):
  - `_task_semaphore` — command/shell_exec 실행
  - `_chat_semaphore` — status/question 답변, db_query 조회/내보내기
- `confirm_execute` / `cancel_execute` 액션 핸들러 (레거시 호환용)
- `confirm_install_claude` 액션 (claude 설치 확인 버튼) — `npm install -g @anthropic-ai/claude-code` 실행 (타임아웃 180초, 출력 3000자 제한)
- `confirm_auth_login` 액션 (claude 로그인 확인 버튼) — 인터랙티브 인증:
  - `claude auth login --claudeai`를 `stdin=PIPE`로 실행 → stdout에서 URL 추출 → 스레드에 URL + "코드를 붙여넣어 주세요" 안내 게시
  - `_pending_auth_sessions[f"{channel}:{msg_ts}"]`에 세션 등록 (`AuthSession`은 proc, user_id, channel, thread_ts, msg_ts, code_future, created_at 보유)
  - `_handle_message` 진입 시 intent 파싱보다 먼저 `_find_auth_session_for_message`로 진행 중 세션 매칭 검사. 매칭되면 메시지 텍스트를 `code_future`로 resolve해 stdin에 기록 (`취소`/`cancel`/`stop`은 CancelledError로 닫고 프로세스 kill). 채널 메시지는 같은 스레드일 때만, DM은 동일 사용자의 진행 중 세션 아무거나 매칭
  - 타임아웃: URL 출력 60초, 코드 입력 15분, stdin 전달 후 완료 대기 60초
  - 테스트가 진행 중 세션을 들여다볼 수 있도록 `app._pending_auth_sessions`로도 노출

### chat.py
- `answer_question(question, tasks, thread_history, projects, target_project, on_progress, task)` — 질문 답변
  - 호출자(`handlers._run_chat_question_and_report`)가 백그라운드 태스크로 실행. 안전 한계 `CHAT_SAFETY_TIMEOUT = 3600`초 — 사용자는 `/stop {ID}`로 언제든 취소
  - `task` 인자: TaskInfo 전달 시 서브프로세스 핸들을 등록해 `stop_task()`로 중단 가능
- `_is_status_query()` — 실행 중 태스크 상태 확인성 질문 감지 시 `--model sonnet`으로 빠르게 응답
- `_build_system_prompt(target_project, wiki_projects, db_instructions)` — 동적 시스템 프롬프트
- target_project에 따라 CWD, 도구, 프롬프트가 달라짐
- status_paths 설정된 프로젝트는 해당 경로의 코드/로그 읽기

### db_query.py
- `run_db_query(question, project, wiki_path, task=None)` — 자연어 DB 조회 (PostgreSQL + SQLite, 타임아웃 `DB_QUERY_TIMEOUT = 120`초)
- `run_db_query_export(question, project, wiki_path, task=None)` — CSV/Excel 내보내기 (타임아웃 `DB_EXPORT_TIMEOUT = 180`초)
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

### security.py
- `make_safe_env(extra)` — `os.environ` 전체를 복사하고 `extra`를 병합해 서브프로세스 env 생성. `_ENV_WHITELIST`(PATH/HOME, XDG, Git/SSH 등) 상수는 정의만 되어 있고 현재 필터링에는 사용되지 않음
- `redact_output(text) -> (masked, found)` — 출력 마스킹(`_REDACT_PATTERNS`): Slack 토큰(xox*-, xapp-), AWS Access Key, JWT, PostgreSQL 연결 문자열, URI 내 자격증명(`://user:pass@`), `password=`/`secret=`/`api_key=` 류 key=value 시크릿
- `check_auth(user_id, role, allowed_users)` — role별 허용 유저 목록 검사 (`"*"`은 전체 허용), `RateLimiter(max_calls, window_seconds)` — 인메모리 per-user rate limit, `log_command()` — 감사 로깅

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
- **Slash Commands**: `/restart`, `/stop` 등록 (Socket Mode에서는 Request URL 불필요)
- **Bot Token Scopes**: `chat:write`, `commands`, `files:write`, `app_mentions:read`, `channels:history`, `groups:history`, `mpim:history`, `im:history`, `reactions:write` — 스코프/슬래시 커맨드 추가 후 앱 재설치 필요
  - `commands` 스코프가 누락되거나 재설치를 빠뜨리면 `/restart`/`/stop` DM 입력이 무반응이 된다 — Slack이 슬래시로 가로채지만 봇에 페이로드가 도달하지 않음
