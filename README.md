# slack-bot

자연어 채팅으로 프로젝트별 Claude Code 명령어를 실행하고, @멘션/DM으로 상태 파악·위키 검색·DB 조회까지 할 수 있는 프로젝트 관리 Slack 봇.

## 사용법

슬래시 명령어 없이 **자연어 채팅** (DM 또는 @멘션)으로 모든 기능을 사용한다.

### 명령어 실행

봇에게 프로젝트명 + 명령어를 자연어로 요청하면, 확인 메시지를 보낸 뒤 승인 시 실행한다.

```
moment-some 하네스 MOM-43 돌려줘
모멘트섬 리뷰 MOM-55 해줘
ra-backend lgbm 실행해줘
```

- 확인 메시지: "moment-some에서 `/harness MOM-43` 실행할까요?" + [실행] [취소] 버튼
- [실행] 클릭 시 백그라운드에서 `claude -p` 실행, 완료 시 결과 전송
- 실행 중 "지금 어디까지 됐어?"로 진행상황 확인 가능

### 프로젝트 상태 파악

```
ra-backend 상태 어때?
자동매매 시스템 현황 알려줘
```

프로젝트의 코드, 로그, 설정 파일을 Claude가 읽어서 현재 상태를 분석한다.

### 질문 답변 (위키 검색)

```
@bot 온보딩 절차가 어떻게 돼?
```

- 위키 프로젝트의 마크다운 파일 검색 → Notion MCP 폴백
- 스레드 내 대화 이력 유지, 후속 질문 가능

### DB 조회

```
지난주 가입한 유저 수 조회해줘
최근 등록된 건축인허가 10건
```

- DB 설정(`db:`)이 있는 프로젝트의 모델을 참고해 자연어 → SQL 변환, psql 실행
- 읽기 전용: `BEGIN; SET TRANSACTION READ ONLY; ... ROLLBACK;`
- 결과 최대 100행, stdout 256KB 상한

### 태스크 제어

```
실행중인 태스크 보여줘
003번 중단해줘
```

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

`projects.yaml`에 프로젝트를 등록한다:

```yaml
projects:
  # Claude Code 명령어를 실행할 프로젝트
  my-project:
    path: /path/to/my-project
    description: "프로젝트 설명 (채팅에서 식별용)"
    commands: [harness, plan, develop, review]
    mcp_tools: [jira_*, linear_*, notion_*, slack_*]

  # 위키 검색 소스 (복수 가능)
  my-wiki:
    path: /path/to/my-wiki
    wiki: true
    description: "위키"

  # DB 조회 프로젝트 (복수 가능)
  my-backend:
    path: /path/to/my-backend
    description: "백엔드"
    db:
      env_file: app/.env
      env_prefix:
        main: POSTGRESQL_MAIN
      model_paths: [app/models/main]

  # 상태 파악용 프로젝트
  my-trading:
    path: /path/to/my-trading
    description: "자동매매 시스템"
    status_paths: [logs/, config/, src/strategy/]
```

| 설정 | 의미 |
|------|------|
| `description` | 채팅에서 프로젝트 식별용 키워드 |
| `commands` | 허용할 Claude Code 명령어 |
| `mcp_tools` | 프로젝트별 MCP 도구 패턴 (생략 시 commands가 있으면 기본 MCP 제공) |
| `wiki: true` | 위키/Notion 검색 소스 |
| `db: {...}` | DB 조회 설정 (env_file, env_prefix, model_paths) |
| `status_paths` | 상태 파악 시 읽을 경로 |

## 실행

```bash
uv run slack-bot
```

## Slack App 설정

1. [api.slack.com/apps](https://api.slack.com/apps)에서 앱 생성
2. **Socket Mode** 활성화 → App-Level Token 발급 (`xapp-...`)
3. **Interactivity** 활성화 (Socket Mode에서 자동, 확인 버튼용)
4. **Event Subscriptions** → Subscribe to bot events: `app_mention`, `message.im`
5. **OAuth & Permissions** → Bot Token Scopes:

| Scope | 용도 |
|-------|------|
| `chat:write` | 메시지 전송 |
| `app_mentions:read` | @멘션 이벤트 수신 |
| `channels:history` | public 채널 스레드 이력 |
| `groups:history` | private 채널 스레드 이력 |
| `mpim:history` | 그룹 DM 스레드 이력 |
| `im:history` | 1:1 DM 스레드 이력 |
| `reactions:write` | 응답 중 리액션 표시 |

> 스코프 추가 후 워크스페이스에 앱을 **재설치**해야 새 권한이 토큰에 반영됩니다.

## 구조

```
slack_bot/
├── main.py            # 엔트리포인트 (Slack Bolt + Socket Mode)
├── config.py          # ProjectConfig/DBConfig, projects.yaml 로드
├── intent.py          # 자연어 인텐트 파싱 (규칙 기반)
├── runner.py          # claude -p 비동기 실행 (프로젝트별 MCP 도구 동적 구성)
├── handlers.py        # @멘션/DM 통합 핸들러 + 확인 버튼 액션
├── chat.py            # 질문 답변 (동적 프롬프트, 프로젝트별 도구)
├── db_query.py        # 자연어→SQL→psql 실행 (DBConfig 기반)
├── task_manager.py    # 태스크 추적 및 출력 누적
└── security.py        # 환경변수 화이트리스트, 출력 마스킹, 인증, rate limit
projects.yaml          # 프로젝트 설정 (gitignore, example 참고)
```

## 요구사항

- Python 3.11+
- `claude` CLI가 PATH에 설치되어 있어야 함
- 각 프로젝트에 `.claude/` 하네스 설정과 `.claude/settings.local.json` 도구 권한 필요
- DB 조회 사용 시:
  - `psql` CLI가 PATH에 있어야 함
  - `projects.yaml`에 `db:` 설정이 있는 프로젝트 필요
  - **보안 권장**: DB 유저는 DB 레벨에서 read-only 권한만 갖는 계정을 사용할 것
