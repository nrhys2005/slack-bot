## 검증 결과: SB-CANCEL-01

### 판정: REFINE

### 요약
기획(버튼 취소, 소유자 확인, 대기 중 취소 가드)은 타당하다. 다만 계획이 "기존 stopped 경로가 처리한다"고 전제한 곳 중 두 곳(`_run_and_report`, `_run_shell_and_report`)에 stopped 분기가 없고, spawn 직전 가드의 위치와 shell 경로의 구조 변경이 빠져 있어 그대로 구현하면 버튼 취소 후 "실패" 메시지가 나가거나 예외 경로로 빠진다.

검증 대상: `/Users/rsquare/sanghun/slack-bot-feat-cancel-button` HEAD `a468736` (origin/main). 참고 구현 `~/git/ra-ai/slack-bot` `d3975a7` (2026-11-05).

### 기획 검증
| 검증 항목 | 상태 | 비고 |
|-----------|------|------|
| 요구사항 일관성 | ✅ | 버튼, `/stop` 유지, 소유자 확인 순서가 서로 충돌하지 않음 |
| 기존 시스템 일관성 | ⚠️ | `/stop` 은 누구나 남의 태스크를 멈출 수 있는데 버튼만 소유자 확인을 한다. 계획이 범위 밖으로 명시했으므로 수용 |

### 계획 검증
| 검증 항목 | 상태 | 비고 |
|-----------|------|------|
| 요구사항 반영 완성도 | ⚠️ | 명령, shell 경로의 취소 응답 분기 누락 |
| 구현 단계 구체성 | ⚠️ | spawn 가드 위치(`_run_prompt`), 명령 시작 알림 순서 문제 누락 |
| 아키텍처 패턴 준수 | ✅ | 모듈 함수 + `register_handlers` 안 등록, best-effort 삭제 패턴 재사용 |
| 테스트 전략 충분성 | ⚠️ | 명령, shell 경로의 버튼 취소 후 최종 메시지 테스트 없음 |

### 코드 대조 결과 (확인한 사실)

| 경로 | 시작 알림 위치 | say 반환값 보관 | 실행 후 stopped 분기 | spawn 위치 |
|---|---|---|---|---|
| 질문 | `_handle_question_intent` (handlers.py 836) | 있음 (`progress_ts`) | 있음 (1503) | chat.py 247 |
| DB 조회 | `_handle_db_query_intent` (756) | 없음 | 있음 (1845) | db_query.py 315 |
| DB 추출 | 같은 함수 (736) | 없음 | 있음 (1757) | db_query.py 560 |
| 프롬프트 | `_handle_project_prompt_intent` (551) | 없음 | 있음 (1674) | runner.py `_run_prompt` 85 |
| 명령 | `_handle_command_intent` (482) | 없음 | **없음** | runner.py `_run_prompt` 85 |
| shell | `_handle_shell_exec_intent` (629) | 없음 | **없음** | handlers.py 1572/1580 (`_run_shell_and_report` 내부) |
| 레거시 `confirm_execute` | 시작 알림 없음 (확인 메시지를 chat_update) | 해당 없음 | **없음** | `_run_and_report` 경유 |

- `say` 반환값: Bolt `AsyncSay` 는 `AsyncSlackResponse` 를 돌려주며 `.get("ts")` 가 된다. 기존 테스트도 `say.return_value = {"ts": "1.0"}` 로 흉내낸다 (tests/test_handler_stability.py 980). 질문 흐름의 방어적 추출 코드(841~846)를 `_post_progress` 로 옮기면 된다.
- `_slash_say_factory` 는 `chat_postMessage` 응답을 그대로 돌려주므로 ts 를 얻을 수 있다. 다만 슬래시 흐름(`/restart`, `/stop`)은 태스크를 만들지 않아 진행 메시지 대상이 아니다. 계획에 반영할 필요 없음.
- `_handle_question_intent` 는 `user_id` 매개변수 자체가 없다. 디스패치(handlers.py 276~286)에서 넘기도록 시그니처를 바꿔야 한다. 계획 3번 항목과 일치.
- `stop_task` 는 `status != "running"` 이면 False, 프로세스가 있으면 `terminate()` 후 stopped. `complete_task` 는 stopped 를 덮어쓰지 않는다. 계획의 게이트 5번 전제와 일치.

### 문제점

#### 계획 문제 (REFINE 사유)

1. **[handlers.py `_run_and_report`] stopped 분기가 없다.** 지금은 `/stop` 이나 버튼으로 끊으면 terminate 된 프로세스의 0 아닌 종료 코드로 ":x: ... 실패" 블록이 나간다. 계획 목표 2-6 의 "최종 취소되었습니다 답글은 기존 stopped 경로가 처리" 가 명령 경로에서는 성립하지 않는다. `_run_prompt_and_report` 의 1674 분기와 같은 모양의 취소 안내를 새로 추가하는 단계를 명시할 것. 레거시 `confirm_execute` 도 같은 함수를 쓰므로 함께 해결된다.
2. **[handlers.py `_run_shell_and_report`] stopped 분기가 없고, spawn 전 가드를 넣으면 `proc` 가 정의되지 않는다.** 가드가 spawn 을 건너뛰면 1587 `proc.returncode` 에서 `UnboundLocalError` 가 나 except 경로(":warning: 실행 중 에러")로 빠진다. `async with semaphore:` 직후 stopped 검사 + 조기 반환, 실행 후 `task.status == "stopped"` 면 취소 안내를 보내는 분기를 둘 다 명시할 것. shell 은 서브모듈이 아니라 이 함수 안에서 spawn 하므로 "shell 실행부" 가드도 이 함수 안이다.
3. **[runner.py] spawn 직전 가드는 `run_claude` 가 아니라 `_run_prompt` 에 둔다.** `run_claude` 와 `run_free_prompt` 가 모두 `_run_prompt` 를 거치므로 한 곳에서 두 경로가 막힌다. 반환값은 `RunResult(success=False, output="", return_code=-1)` 처럼 정하고 테스트도 `run_free_prompt` 를 포함한다.
4. **spawn 가드의 남은 틈.** `await asyncio.create_subprocess_exec(...)` 자체가 대기 지점이라, 검사를 통과한 뒤 spawn 이 끝나기 전에 stop 이 오면 `task.process` 가 None 이라 terminate 되지 않고 CLI 가 끝까지 돈다. 각 spawn 지점(chat.py 247, db_query.py 315/560, runner.py 85, handlers.py shell 2곳)에서 `task.process = proc` 직후 `task.status == "stopped"` 면 `proc.terminate()` 하는 재검사를 추가할 것. spawn 전 가드만으로는 계획이 말한 "경합 방지" 가 완성되지 않는다.
5. **[handlers.py `_handle_command_intent`] 시작 알림이 백그라운드 태스크 생성 뒤(482)에 나간다.** ts 를 `_run_and_report` 에 넘기려면 알림을 먼저 게시하고 태스크를 띄우도록 순서를 바꾸거나, `TaskInfo` 에 `progress_ts` 필드를 두어 나중에 채워야 한다. 어느 쪽인지 정할 것 (shell 은 이미 알림이 먼저다).
6. **`chat_update(blocks=[])` 에 text 를 함께 넘긴다.** 계획 2-2(nonce 불일치)와 5번(완료 시 버튼 걷기)은 `blocks=[]` 만 적었다. text 없이 blocks 를 비우면 Slack 이 `no_text` 로 거절할 수 있다. 참고 구현도 모든 호출에 `text=..., blocks=[]` 를 함께 준다 (ra-ai handlers.py 3456, 3751 등). `_clear_progress_buttons(client, channel, ts, text)` 처럼 원문 text 를 받게 하고, 버튼 콜백에서는 `body["message"]["text"]` 를 쓴다.
7. **명령/shell 완료 시 버튼 걷기와 "취소 요청됨" 문구의 충돌.** 버튼 취소 후 콜백이 text 를 "취소 요청됨" 으로 바꿨는데, 완료 시 `_clear_progress_buttons` 가 원래 시작 문구로 다시 덮으면 기록이 뒤집힌다. 완료 시에는 stopped 가 아닐 때만 버튼을 걷거나, 같은 text 를 유지하는 규칙을 명시할 것.
8. **테스트 보강.**
   - 명령(`_run_and_report`)과 shell 의 "실행 중 stop 후 취소 안내가 나가고 '실패' 는 안 나간다".
   - shell 의 대기 중 취소 시 `create_subprocess_shell` 미호출, 에러 메시지 미게시.
   - spawn 직후 재검사: `create_subprocess_exec` mock 안에서 stop 을 호출하면 반환된 proc 에 `terminate` 가 불린다.
   - DB 조회/추출 결과 게시 후 진행 메시지 `chat_delete` 호출.

#### 참고 (판정에 영향 없음)
- 계획이 인용한 ra-ai 줄 번호(279, 4193, 2927)는 `d3975a7` 기준 정확성을 이번에 대조하지 않았다. 구현 참고용이라 영향 없음.
- shell 의 `create_subprocess_shell` 은 terminate 가 `/bin/sh` 만 끝내 자식이 남을 수 있고, 원격 ssh 는 원격 프로세스가 계속 돌 수 있다. 기존 `/stop` 과 같은 한계라 범위 밖으로 두되, 버튼 문구가 "정리 중" 이라 체감과 다를 수 있음을 후속 후보에 적어 둘 것.

### 승인 항목
- nonce 로 재시작 전 버튼을 막는 설계, 소유자 확인을 status 검사보다 먼저 두는 순서(남의 작업 상태가 새지 않음).
- blocks 실패 시 평문 폴백, section 3000자 제한 명시.
- 질문 태스크에 요청자 기록 누락을 정확히 짚음 (`create_task(..., "", ...)` handlers.py 825~832).
- DB 시작 알림 삭제를 질문 흐름의 `_delete_progress` 와 맞춘 것, 명령/shell 알림은 로그 성격이라 남기는 구분.
- 범위 밖 항목(`/stop` 권한, 멘션 무력화, 재시작 대기)을 분리한 것.
