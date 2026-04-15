# slack-bot

Slack에서 프로젝트별 Claude Code 하네스 명령어를 실행하고, @멘션으로 진행상황 질문 및 Notion 위키 검색을 할 수 있는 대화형 봇.

## 구조

```
slack_bot/
├── main.py            # 엔트리포인트 (Slack Bolt + Socket Mode)
├── config.py          # projects.yaml 로드
├── runner.py          # claude -p 비동기 실행 (--allowedTools 자동 적용)
├── handlers.py        # 슬래시 커맨드 & @멘션 핸들러
├── chat.py            # @멘션 질문 → Claude CLI로 답변 생성
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

# 진행상황 질문 (@멘션)
@bot 지금 어디까지 됐어?

# 위키/문서 검색 (@멘션)
@bot 온보딩 절차가 어떻게 돼?
```

## 요구사항

- Python 3.11+
- `claude` CLI가 PATH에 설치되어 있어야 함
- 각 프로젝트에 `.claude/` 하네스가 설정되어 있어야 함
- 프로젝트의 `.claude/settings.local.json`에 필요한 도구 권한이 허용되어 있어야 함
