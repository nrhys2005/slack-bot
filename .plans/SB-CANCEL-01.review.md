## 리뷰 결과: SB-CANCEL-01

### 판정: APPROVE

### 요약
계획의 "검증 반영" 8개 항목이 모두 구현됐고, 핵심 게이트(소유자 검사 순서, nonce, 채널, 대기 중 가드, spawn 직후 terminate, 취소 시 실패 블록 미전송)는 뮤테이션을 걸면 테스트가 실패한다. 남은 것은 테스트 공백 몇 군데와 `/stop` 으로 취소했을 때 버튼이 남는 문제로, 둘 다 권장 수정이다.

검증 대상: 워크트리 `/Users/rsquare/sanghun/slack-bot-feat-cancel-button`, 커밋 `b2a4177`, base `origin/main` `a468736`.

### 실행한 검증

| 항목 | 명령 | 결과 |
|---|---|---|
| 전체 테스트 | `uv run pytest -q` | 165 passed |
| 린트 | `uv run ruff check .` | F401 2건, 둘 다 `tests/test_auth_login.py` (base 에도 있음, 이번 diff 밖). `slack_bot/`, `tests/test_task_cancel.py` 는 깨끗함 |
| 동어반복 확인 | `origin/main` 을 scratchpad 에 풀고 새 테스트 파일만 올려 실행 | 새 헬퍼가 없어 import 단계에서 실패. 헬퍼(`_BOOT_NONCE` ~ `_handle_task_cancel`)만 이식해 다시 돌리면 26개 중 14개 실패 (대기 중 가드, 실행 중 취소, spawn 가드, 정리, 등록 전부). 통과한 12개는 이식한 헬퍼 자체를 검사하는 blocks/콜백 테스트라 예상대로임 |

뮤테이션 결과 (매번 `git checkout -- <file>` 로 원복, 끝난 뒤 `git status` 에 변경 없음):

| 뮤테이션 | 결과 | 잡은 테스트 |
|---|---|---|
| 소유자 검사를 `stop_task` 검사 뒤로 옮김 | 잡힘 | `test_owner_check_runs_before_status_check`, `test_other_user_rejected` |
| nonce 검사 끔 | 잡힘 | `test_nonce_mismatch_rejected` |
| 채널 검사 끔 | 잡힘 | `test_other_channel_rejected_without_update` |
| 대기 중 가드 끔: 명령, 질문, DB 조회, shell | 잡힘 | `TestQueuedCancelGuard` 각 테스트 |
| 대기 중 가드 끔: DB 추출, 프롬프트 | **안 잡힘** | 없음 |
| spawn 전 가드 끔: runner `_run_prompt`, chat | 잡힘 | `TestSpawnGuard` |
| spawn 전 가드 끔: db_query 두 함수 | **안 잡힘** | 없음 |
| spawn 직후 terminate 끔: runner | 잡힘 | `test_run_claude_terminates_when_stopped_during_spawn` |
| spawn 직후 terminate 끔: chat, db_query 두 함수, shell | **안 잡힘** | 없음 |
| `_run_and_report` 의 stopped 분기 끔 (실패 블록이 나감) | 잡힘 | `test_command_cancelled_posts_cancel_only` |
| shell 실행 후 stopped 분기 끔 | 잡힘 | `test_shell_cancelled_posts_cancel_only` |
| `_finish_progress_buttons` 의 stopped 조건 끔 | 잡힘 | `test_command_cancelled_keeps_cancel_requested_text` |
| blocks 실패 시 평문 폴백 끔 | 잡힘 | `test_falls_back_to_plain_text_on_invalid_blocks` |
| 질문 태스크에 user 를 다시 `""` 로 | 잡힘 | `test_question_task_records_requester` |
| DB 추출의 진행 메시지 삭제 끔 | **안 잡힘** | 없음 (DB 조회 쪽만 테스트됨) |

### 🔴 필수 수정
없음.

### 🟡 권장 수정

1. **[handlers.py `_finish_progress_buttons`] `/stop` 으로 취소한 명령, 프롬프트, shell 알림에 버튼이 남는다.** stopped 면 아무것도 하지 않는 규칙(검증 반영 7번)은 버튼 콜백이 먼저 문구를 바꿔 뒀다는 전제다. `/stop` 으로 끊으면 콜백을 거치지 않아 시작 알림에 [취소] 버튼이 그대로 남는다. 누르면 "이미 종료된 작업입니다" ephemeral 만 나와 해는 없지만 화면이 어긋난다. 수정 방향: stopped 일 때도 `text=":octagonal_sign: 취소됨 (ID: NNN)", blocks=[]` 로 update 한다. 버튼 콜백이 이미 바꾼 경우에도 같은 뜻의 문구라 덮어써도 문제가 없다.

2. **[handlers.py `_handle_command_intent`, `_handle_project_prompt_intent`] 차이 (6) 은 명령뿐 아니라 프롬프트 경로에도 있다.** 백그라운드 태스크가 `_post_progress` 의 `await say` 사이에 끝나면 `progress_ts` 가 아직 비어 있어 버튼이 남는다. 알림을 먼저 게시하도록 순서를 바꾸는 것은 두 줄 이동이라 간단하지만, 그러면 게시가 예외로 끝났을 때 `create_task_if_absent` 로 등록된 태스크가 running 으로 영영 남는다. `cleanup_old` 는 running 을 지우지 않으므로 같은 명령의 재실행이 계속 "이미 실행 중" 으로 막힌다 (shell, DB, 질문 경로는 지금도 이 순서라 같은 위험이 있다). 더 안전하고 간단한 방법은 순서를 그대로 두고, `task.progress_ts = ...` 바로 다음 줄(사이에 await 없음)에서 `if task.status != "running": await _finish_progress_buttons(...)` 처럼 이미 끝난 경우만 handler 가 직접 버튼을 걷는 것이다. 1번을 반영하면 stopped 도 같이 처리된다.

3. **[tests/test_task_cancel.py] 위 표의 "안 잡힘" 항목에 테스트가 없다.** 특히 spawn 직후 terminate 는 6곳 중 runner 한 곳만 검증된다. 같은 모양이라 chat, db_query 두 함수, shell 에 `test_run_claude_terminates_when_stopped_during_spawn` 과 같은 테스트를 하나씩 추가하고, DB 추출과 프롬프트의 대기 중 가드, DB 추출의 진행 메시지 삭제도 하나씩 둔다.

### 🟢 참고: 개발자가 밝힌 계획과의 차이 판단

| 차이 | 판단 |
|---|---|
| (1) 명령, shell 알림에 `/stop ID` 문구 추가 | 타당. blocks 폴백 시 취소 수단이 남고 다른 경로와 문구가 맞는다 |
| (2) spawn 전 가드의 반환값이 함수마다 다름 | 타당. 모든 호출부가 반환값보다 `task.status == "stopped"` 를 먼저 보므로 반환값은 사용자에게 나가지 않는다. 각 함수의 반환 타입을 지킨 것 |
| (3) `progress_text` 인자 추가 | 타당. `blocks=[]` 와 함께 원문 text 를 줘야 한다는 검증 반영 6번을 지키려면 필요하다 |
| (4) `_response_ts`, `_task_progress_ts` 의 타입 검사 | 수용. `AsyncSlackResponse.data` 와 dict 를 모두 다루고 MagicMock 은 빈 문자열로 떨어진다. `_task_progress_ts` 는 필드가 생겨 운영 코드에서는 불필요하지만 해는 없다 |
| (5) 질문 흐름의 기존 `progress_ts` 인자 유지 | 수용. 기존 삭제 경로와 테스트를 건드리지 않는 선택이다. 같은 값이 인자와 `task.progress_ts` 두 군데에 있다는 점만 남는다 |
| (6) 명령 경로의 버튼 잔류 가능성 | 위 권장 수정 2번. 단순 순서 교체보다 게시 후 상태를 확인하는 쪽을 권한다 |

### 승인 항목
- 콜백 검사 순서(파싱, nonce, 존재와 채널, 소유자, `stop_task`)가 계획과 같고, 소유자 검사를 상태 검사보다 앞에 둬 남의 작업 상태가 드러나지 않는다.
- `chat_update(blocks=[])` 를 부르는 곳마다 text 를 함께 넘긴다.
- shell 경로의 `proc` 미정의 문제를 조기 반환으로 피했다.
- `_run_and_report` 의 취소 안내 추가로 레거시 `confirm_execute` 경로도 같이 고쳐졌다.
- 명령, shell, DB, 질문 태스크 모두 `task.user` 에 요청자가 기록된다 (`create_task_if_absent` 호출 확인).
