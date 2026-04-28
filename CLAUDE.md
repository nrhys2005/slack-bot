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
├── handlers.py        # @멘션, DM 통합 메시지 핸들러 + 확인 버튼 액션
├── chat.py            # Claude CLI로 태스크 출력 분석, 프로젝트 상태 파악, 질문 답변
├── db_query.py        # DB 프로젝트 모델 기반 자연어→SQL→psql 실행
├── task_manager.py    # TaskInfo/TaskManager — 실행 중 태스크 추적, 출력 누적
└── security.py        # 환경변수 화이트리스트, 출력 마스킹, 인증, rate limit, 감사 로깅
projects.yaml          # 프로젝트 → 경로/명령어/capabilities 매핑
.env                   # SLACK_BOT_TOKEN, SLACK_APP_TOKEN
pyproject.toml         # 의존성 및 스크립트 정의
```

## 아키텍처 흐름

### 채팅 기반 명령 실행 (확인 후 실행)

```
"moment-some 하네스 MOM-43 돌려줘"
  → handlers.py: 메시지 수신 (app_mention 또는 DM)
  → intent.py: parse_intent() — type="command", project="moment-some", command="harness", args="MOM-43"
  → 확인 메시지 전송: "moment-some에서 /harness MOM-43 실행할까요?" + [실행] [취소] 버튼
  → [실행] 클릭 → action 핸들러
    → TaskManager.create_task() — 태스크 ID 부여, 추적 시작
    → asyncio.create_task()로 백그라운드 실행
      → runner.py: claude -p "/harness MOM-43" (프로젝트별 MCP 도구 동적 구성)
      → stdout 라인별 스트리밍 → TaskInfo.output_lines에 누적
      → 완료 시 결과를 Block Kit 메시지로 전송
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
"실행중인 태스크 보여줘" → task_control/list
"003번 중단해줘" → task_control/stop, args="003"
```

### 자연어 DB 조회

```
"지난주 가입한 유저 수 조회해줘"
  → intent.py: type="db_query"
  → db_query.run_db_query(question, project) — DBConfig 기반 credentials 로드
  → claude -p로 SQL 생성·psql 실행
  → 답변 전송
```

## 주요 모듈 상세

### config.py
- `DBConfig(env_file, env_prefix, model_paths)` — 프로젝트별 DB 접속 설정
- `ProjectConfig(name, path, commands, description, wiki, db, mcp_tools, status_paths)` — 프로젝트 설정
- `load_projects()`: `projects.yaml` 읽어서 `AppConfig(projects, security)` 반환
- 하위호환: `db_backend: true` → `DBConfig` 자동 변환, `mcp_tools` 미설정 시 기본 MCP 제공
- 프로젝트별 capabilities:
  - `wiki: true` — 위키 검색 소스 (복수 가능)
  - `db: {...}` — DB 조회 설정 (env_file, env_prefix, model_paths)
  - `mcp_tools: [jira_*, ...]` — Claude CLI에 전달할 MCP 도구 패턴
  - `status_paths: [logs/, ...]` — 상태 파악 시 읽을 경로
  - `description` — 채팅에서 프로젝트 식별용 키워드

### intent.py
- `parse_intent(text, projects) -> Intent` — 규칙 기반 인텐트 파싱
- Intent 타입: command, status, question, task_control, db_query
- 프로젝트명 매칭: 이름 직접 매칭 → description 키워드 매칭
- 명령어 매핑: 한국어 ("하네스", "리뷰") → 영문 ("harness", "review")
- 이슈 ID (`[A-Z]+-\d+`), 태스크 ID (`\d{3}`) 자동 추출

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
  - command → 확인 메시지(Block Kit 버튼) → 승인 시 runner.py 실행
  - task_control → 태스크 목록/중단
  - db_query → db_query.py로 위임
  - status/question → chat.py로 위임
- `confirm_execute` / `cancel_execute` 액션 핸들러 (버튼 클릭)

### chat.py
- `answer_question(question, tasks, thread_history, projects, target_project)` — 질문 답변
- `_build_system_prompt(target_project, wiki_projects, db_instructions)` — 동적 시스템 프롬프트
- target_project에 따라 CWD, 도구, 프롬프트가 달라짐
- status_paths 설정된 프로젝트는 해당 경로의 코드/로그 읽기

### db_query.py
- `run_db_query(question, project, wiki_path)` — 자연어 DB 조회
- `_load_db_env(project)` — DBConfig.env_prefix 기반 credentials 로드
- `build_db_instructions(db_envs, model_paths)` — 동적 DB 접속정보·규칙 생성
- PGPASSWORD 환경변수: `PGPASSWORD_{논리명.upper()}` 패턴
- 안전장치 (다층 방어):
  1. (근본) DB 유저 자체를 read-only 계정으로 사용 권장
  2. 프롬프트에서 SELECT 외 금지 명시
  3. `BEGIN; SET TRANSACTION READ ONLY; ... ROLLBACK;` 래핑
  4. `LIMIT 100` 강제
  5. stdout 256KB 상한 (OOM 방어)

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
- **Bot Token Scopes**: `chat:write`, `app_mentions:read`, `channels:history`, `groups:history`, `mpim:history`, `im:history`, `reactions:write` — 스코프 추가 후 앱 재설치 필요
