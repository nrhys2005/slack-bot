# slack-bot

Slack에서 프로젝트별 Claude Code 하네스 명령어를 실행하는 봇.

## 구조

```
slack_bot/
├── main.py        # 엔트리포인트 (Slack Bolt + Socket Mode)
├── config.py      # projects.yaml 로드
├── runner.py      # claude -p 비동기 실행
└── handlers.py    # 슬래시 커맨드 핸들러
projects.yaml      # 프로젝트 매핑 설정
```

## 설치

```bash
uv sync
cp .env.example .env
# .env에 Slack 토큰 입력
```

## Slack App 설정

1. [api.slack.com/apps](https://api.slack.com/apps)에서 앱 생성
2. **Socket Mode** 활성화 → App-Level Token 발급 (`xapp-...`)
3. **Slash Commands** 추가:
   - `/claude` — 하네스 명령어 실행
   - `/claude-projects` — 등록된 프로젝트 목록
4. **OAuth & Permissions** → Bot Token Scopes:
   - `commands`
   - `chat:write`
5. 워크스페이스에 앱 설치 → Bot Token (`xoxb-...`)
6. `.env`에 토큰 입력

## 프로젝트 추가

`projects.yaml`에 프로젝트를 추가한다:

```yaml
projects:
  moment-some:
    path: /Users/rsquare/sanghun/moment-some-app
    commands:
      - harness
      - plan
      - develop
      - review
  new-project:
    path: /path/to/project
    commands:
      - harness
      - plan
```

## 실행

```bash
uv run slack-bot
```

## 사용법

```
/claude <project> <command> [args]

# 예시
/claude moment-some harness MOM-43
/claude moment-some plan MOM-43
/claude moment-some develop MOM-43
/claude moment-some harness MOM-44,MOM-45 --auto

# 프로젝트 목록 조회
/claude-projects
```

## 요구사항

- Python 3.11+
- `claude` CLI가 PATH에 설치되어 있어야 함
- 각 프로젝트에 `.claude/` 하네스가 설정되어 있어야 함
- 프로젝트의 `.claude/settings.local.json`에 필요한 도구 권한이 허용되어 있어야 함
