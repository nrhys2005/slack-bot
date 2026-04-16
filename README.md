# slack-bot

Slack에서 프로젝트별 Claude Code 하네스 명령어를 실행하고, @멘션으로 진행상황 질문·Notion 위키 검색, `/db`로 ra_backend 모델 기반 자연어 DB 조회까지 할 수 있는 대화형 봇.

## 구조

```
slack_bot/
├── main.py            # 엔트리포인트 (Slack Bolt + Socket Mode)
├── config.py          # projects.yaml 로드
├── runner.py          # claude -p 비동기 실행 (--allowedTools 자동 적용)
├── handlers.py        # 슬래시 커맨드 & @멘션 핸들러
├── chat.py            # @멘션 질문 → Claude CLI로 답변 생성
├── db_query.py        # /db — db_backend 프로젝트 모델 기반 자연어→SQL→psql 실행
└── task_manager.py    # 실행 중 태스크 추적 및 출력 누적
projects.yaml              # 프로젝트 매핑 설정 (gitignore, example 참고)
```

## 설치

```bash
uv sync
cp .env.example .env
cp projects.yaml.example projects.yaml
# .env에 Slack 토큰 입력
# projects.yaml에 프로젝트 경로 및 명령어 설정
```

## Slack App 설정

1. [api.slack.com/apps](https://api.slack.com/apps)에서 앱 생성
2. **Socket Mode** 활성화 → App-Level Token 발급 (`xapp-...`)
3. **Slash Commands** 추가:
   - `/dev` — harness 단축 실행
   - `/claude` — 범용 명령어 실행
   - `/projects` — 등록된 프로젝트 목록
   - `/stop` — 실행 중인 태스크 중단
   - `/db` — `db_backend: true` 로 지정된 프로젝트의 SQLAlchemy 모델 기반 자연어 DB 조회 (읽기 전용, 결과 최대 100행)
4. **Event Subscriptions** → Subscribe to bot events:
   - `app_mention`
5. **OAuth & Permissions** → Bot Token Scopes:
   - `commands`
   - `chat:write`
   - `app_mentions:read`
   - `channels:history` (public 채널 스레드 이력)
   - `groups:history` (private 채널 스레드 이력)
   - `mpim:history` (그룹 DM 스레드 이력)
   - `im:history` (1:1 DM 스레드 이력)
   - `reactions:write` (응답 중 리액션 표시용)

   > 스코프 추가 후 워크스페이스에 앱을 **재설치**해야 새 권한이 토큰에 반영됩니다.
6. 워크스페이스에 앱 설치 → Bot Token (`xoxb-...`)
7. `.env`에 토큰 입력

## 프로젝트 추가

`projects.yaml`에 프로젝트를 추가한다 (`projects.yaml.example` 참고):

```yaml
projects:
  my-project:
    path: /path/to/my-project
    commands:
      - harness
      - plan
      - develop
      - review
```

## 실행

```bash
uv run slack-bot
```

## 사용법

```
# harness 단축 실행
/dev <project> <issue>

# 범용 명령어 실행
/claude <project> <command> [args]

# 프로젝트 목록 조회
/projects

# 태스크 중단
/stop              # 실행 중 목록 표시
/stop <ID>         # 특정 태스크 중단

# 자연어 DB 조회 (db_backend 프로젝트 모델 기반, 읽기 전용 SELECT, 결과 최대 100행)
/db 지난주 신규 가입한 유저 수
/db 최근 등록된 건축인허가 10건

# 진행상황 질문 (@멘션)
@bot 지금 어디까지 됐어?

# 위키/문서 검색 (@멘션)
@bot 온보딩 절차가 어떻게 돼?
```

## 요구사항

- Python 3.11+
- `claude` CLI가 PATH에 설치되어 있어야 함
- `/db` 사용 시:
  - `psql` CLI가 PATH에 있어야 함
  - `projects.yaml` 에 `db_backend: true` 가 설정된 FastAPI 백엔드 프로젝트가 1개 등록돼 있어야 함 (예: `ra_backend`)
  - 해당 프로젝트의 `app/.env` 에서 `POSTGRESQL_RA_*` / `POSTGRESQL_CORE_*` 환경변수를 읽어 psql 접속에 사용
  - **보안 권장**: `app/.env` 의 DB 유저는 DB 레벨에서 read-only 권한만 갖는 계정을 쓸 것. 앱 레벨 SELECT-only 프롬프트는 우회 가능성이 있으므로, DB 측에서도 방어하는 것이 안전 (예: `CREATE ROLE ... WITH LOGIN`, `GRANT SELECT ON ALL TABLES ...`).
- 각 프로젝트에 `.claude/` 하네스가 설정되어 있어야 함
- 프로젝트의 `.claude/settings.local.json`에 필요한 도구 권한이 허용되어 있어야 함
