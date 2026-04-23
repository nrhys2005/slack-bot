# slack-bot

Slack에서 프로젝트별 Claude Code 하네스 명령어를 실행하고, @멘션으로 진행상황 질문·Notion 위키 검색, `/db`로 자연어 DB 조회까지 할 수 있는 대화형 봇.

## 사용법

### 슬래시 커맨드

#### `/dev <project> <issue>` — harness 단축 실행

Jira 이슈 기반 개발 파이프라인(Planner → Developer → Reviewer → PR)을 한 번에 실행한다.

```
/dev moment-some MOM-43
```

- `--auto` 플래그가 자동으로 추가됨 (비대화형 환경)
- 완료되면 실행 결과가 채널에 Block Kit 메시지로 전송됨
- 실행 중 `@bot 지금 어디까지 됐어?`로 진행상황 확인 가능

#### `/claude <project> <command> [args]` — 범용 명령어 실행

프로젝트에 등록된 Claude Code 명령어를 실행한다.

```
/claude moment-some plan MOM-43
/claude moment-some develop MOM-43
/claude moment-some review MOM-43
```

#### `/projects` — 등록된 프로젝트 목록 조회

```
/projects
```

프로젝트별 허용된 명령어 목록을 보여준다.

#### `/stop [ID]` — 태스크 중단

```
/stop          # 실행 중인 태스크 목록 표시
/stop 001      # ID로 특정 태스크 중단
```

#### `/db <자연어 질문>` — DB 조회

`db_backend: true`로 설정된 프로젝트의 SQLAlchemy 모델을 참고하여 자연어 질문을 SQL로 변환, psql로 실행한다.

```
/db 지난주 신규 가입한 유저 수
/db 최근 등록된 건축인허가 10건
/db ra_v2 스키마 테이블 목록 보여줘
```

- 읽기 전용: `BEGIN; SET TRANSACTION READ ONLY; ... ROLLBACK;`으로 래핑
- 결과 최대 100행, stdout 256KB 상한
- 실행한 SQL 전문이 결과에 포함됨

### @멘션 질문

채널에서 봇을 멘션하면 다음 유형의 질문에 답변한다.

```
@bot 지금 어디까지 됐어?          # 실행 중 태스크 진행상황 분석
@bot 온보딩 절차가 어떻게 돼?     # 위키/Notion 검색
@bot 이번 달 활성 유저 수 알려줘  # DB 조회 (db_backend 설정 시)
```

- 멘션 수신 시 👀 리액션으로 즉시 피드백, 답변 완료 후 제거
- 스레드 내 대화 이력을 유지하여 후속 질문 가능
- 위키 검색: 로컬 마크다운 우선 → Notion MCP 폴백

## 설치

```bash
uv sync
cp .env.example .env
cp projects.yaml.example projects.yaml
```

`.env`에 Slack 토큰을 입력한다:

```
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
```

## 프로젝트 설정

`projects.yaml`에 프로젝트를 등록한다 (`projects.yaml.example` 참고):

```yaml
projects:
  # Claude Code 명령어를 실행할 프로젝트
  my-project:
    path: /path/to/my-project
    commands:
      - harness
      - plan
      - develop
      - review

  # @멘션 질문 시 위키 검색 소스 (1개만 설정)
  my-wiki:
    path: /path/to/my-wiki
    commands: []
    wiki: true

  # /db 커맨드의 DB 모델·자격증명 소스 (1개만 설정)
  my-backend:
    path: /path/to/my_backend
    commands: []
    db_backend: true
```

| 플래그 | 의미 | 제한 |
|--------|------|------|
| `wiki: true` | @멘션 시 위키/Notion 검색 소스 | 전체 중 1개만 |
| `db_backend: true` | `/db` 및 @멘션 DB 조회의 모델·자격증명 소스 | 전체 중 1개만 |

## 실행

```bash
uv run slack-bot
```

## Slack App 설정

1. [api.slack.com/apps](https://api.slack.com/apps)에서 앱 생성
2. **Socket Mode** 활성화 → App-Level Token 발급 (`xapp-...`)
3. **Slash Commands** 추가: `/dev`, `/claude`, `/projects`, `/stop`, `/db`
4. **Event Subscriptions** → Subscribe to bot events: `app_mention`
5. **OAuth & Permissions** → Bot Token Scopes:

| Scope | 용도 |
|-------|------|
| `commands` | 슬래시 커맨드 |
| `chat:write` | 메시지 전송 |
| `app_mentions:read` | @멘션 이벤트 수신 |
| `channels:history` | public 채널 스레드 이력 |
| `groups:history` | private 채널 스레드 이력 |
| `mpim:history` | 그룹 DM 스레드 이력 |
| `im:history` | 1:1 DM 스레드 이력 |
| `reactions:write` | 응답 중 👀 리액션 표시 |

> 스코프 추가 후 워크스페이스에 앱을 **재설치**해야 새 권한이 토큰에 반영됩니다.

## 구조

```
slack_bot/
├── main.py            # 엔트리포인트 (Slack Bolt + Socket Mode)
├── config.py          # projects.yaml 로드
├── runner.py          # claude -p 비동기 실행 (timeout, deadlock 방지)
├── handlers.py        # 슬래시 커맨드 & @멘션 핸들러 (Semaphore 동시실행 제한)
├── chat.py            # @멘션 질문 → Claude CLI로 답변 생성
├── db_query.py        # /db — 자연어→SQL→psql 실행 (읽기 전용, 256KB 상한)
└── task_manager.py    # 실행 중 태스크 추적 및 출력 누적 (asyncio.Lock)
projects.yaml              # 프로젝트 매핑 설정 (gitignore, example 참고)
```

## 요구사항

- Python 3.11+
- `claude` CLI가 PATH에 설치되어 있어야 함
- 각 프로젝트에 `.claude/` 하네스 설정과 `.claude/settings.local.json` 도구 권한 필요
- `/db` 사용 시:
  - `psql` CLI가 PATH에 있어야 함
  - `projects.yaml`에 `db_backend: true`가 설정된 프로젝트 1개 필요
  - 해당 프로젝트의 `app/.env`에서 `POSTGRESQL_RA_*` / `POSTGRESQL_CORE_*` 환경변수를 읽어 psql 접속에 사용
  - **보안 권장**: DB 유저는 DB 레벨에서 read-only 권한만 갖는 계정을 사용할 것
