# SB-CANCEL-01: 진행 메시지 [취소] 버튼과 대기 중 취소 가드

티켓 없음 (개인 레포, 저장소 관례대로 scope 없는 커밋). 참고 구현: `~/git/ra-ai/slack-bot` origin/main d3975a7 (2026-11-05) 의 RA1-17340
(`slack_bot/handlers.py` `_build_progress_blocks` 279, `_handle_chat_cancel` 4193, 큐 대기 가드 2927, `tests/test_chat_cancel.py`).

## 배경

- 지금은 시작 알림에 "취소: `/stop ID`" 문구만 있다. 사용자가 ID 를 보고 슬래시를 쳐야 하고, DM 이 아니면 불편하다.
- `semaphore` 대기 중에 `/stop` 하면 `task.process` 가 아직 None 이라 status 만 stopped 로 바뀌고, 이후 슬롯을 얻으면
  `answer_question` / `run_db_query*` / `run_claude` 가 CLI 를 끝까지 돌린 뒤에야 취소 문구가 나온다 (토큰 낭비, 체감상 취소 실패).
- 질문 태스크는 `create_task(..., user="")` 로 요청자가 기록되지 않는다 (`handlers.py` 826 부근). 소유자 확인이 불가능하다.
- DB 조회/추출 시작 알림은 ts 를 보관하지 않아 끝난 뒤에도 남는다.

## 목표 (검증 가능한 기준)

1. 질문(`chat`), DB 조회/추출, 명령(`/harness` 등 `_run_and_report`), 프로젝트 프롬프트, shell_exec 의 시작 알림에 danger 스타일 [취소] 버튼이 붙는다.
   - 버튼 `action_id = "task_cancel"`, `value = f"{task_id}:{_BOOT_NONCE}"`. `_BOOT_NONCE = uuid.uuid4().hex[:8]` (모듈 상수).
   - 기존 평문 문구(`/stop ID` 안내)는 `text` 와 section 에 그대로 남긴다 (알림 미리보기, 폴백용).
   - blocks 게시가 실패(`SlackApiError`, 예: invalid_blocks)하면 평문 `say(text)` 로 다시 보낸다.
2. `@app.action("task_cancel")` 콜백 검사 순서:
   1. value 파싱 실패 -> 로그만.
   2. nonce 불일치 -> ephemeral "봇이 재시작되어 만료된 버튼입니다" + 메시지 버튼 제거(`chat_update(blocks=[])`, best-effort).
   3. task 가 없거나 `task.channel != 클릭 채널` -> ephemeral "작업을 찾을 수 없습니다". task 가 None 이면 버튼 제거.
   4. `task.user != 클릭한 사용자` -> ephemeral ":lock: 본인이 요청한 작업만 취소할 수 있습니다". **status 검사보다 먼저** 한다.
   5. `task_manager.stop_task()` 가 False -> ephemeral "이미 종료된 작업입니다". 메시지는 건드리지 않는다.
   6. 성공 -> `chat_update(text=":octagonal_sign: 취소 요청됨 (ID: NNN), 정리 중입니다.", blocks=[])` 로 버튼 제거.
      최종 "취소되었습니다" 답글, 진행 메시지 삭제, 👀 제거는 기존 stopped 경로가 처리한다.
3. 질문 태스크에 요청자 user_id 를 기록한다 (`_handle_question_intent` 에 user_id 전달).
4. 대기 중 취소 가드: 각 `_run_*_and_report` 의 `async with semaphore:` 직후 `task.status == "stopped"` 이면 실행 함수를 부르지 않고
   기존 stopped 응답 분기로 간다. 추가로 서브프로세스 spawn 직전(`chat.py` answer_question, `db_query.py` 두 함수, `runner.py` run_claude,
   shell 실행부)에 task 가 stopped 면 spawn 하지 않고 빈 결과를 돌려준다 (슬롯 획득과 spawn 사이 경합 방지).
5. DB 조회/추출 시작 알림도 ts 를 보관해 결과 게시 후 `chat_delete` (질문 흐름의 `_delete_progress` 와 같은 방식).
   명령/프롬프트/shell 시작 알림은 실행 로그 성격이라 삭제하지 않고, 완료 시 `chat_update(blocks=[])` 로 버튼만 걷는다 (best-effort).
6. `/stop` 동작과 문구는 그대로 둔다.

## 검증 반영 (validation REFINE 1차)

1. `_run_and_report` 에 stopped 분기가 없다. `_run_prompt_and_report` 와 같은 모양의 취소 안내(":octagonal_sign: ... 취소되었습니다.")를 추가하고, 취소 시 ":x: 실패" 블록을 보내지 않는다.
2. `_run_shell_and_report` 도 stopped 분기가 없다. 세마포어 직후 stopped 면 spawn 없이 취소 안내 후 조기 반환하고, 실행 후 stopped 면 실패 대신 취소 안내를 보낸다. `proc` 미정의(UnboundLocalError)가 생기지 않게 한다.
3. runner 쪽 가드는 `run_claude` 가 아니라 공통 경로 `_run_prompt` 에 둔다.
4. spawn 하는 6곳(chat.py, db_query.py 2곳, runner.py `_run_prompt`, handlers.py `_run_shell_and_report` 2곳) 모두 `task.process = proc` 직후 status 를 다시 보고 stopped 면 `terminate()` 한다 (spawn await 중 stop 경합).
5. 시작 알림 ts 는 `TaskInfo.progress_ts: str = ""` 필드에 둔다. 알림 게시 후 `task.progress_ts = resp.get("ts", "")`. `_handle_command_intent` 처럼 백그라운드 태스크를 먼저 띄우는 경로도 이 필드로 해결된다.
6. 버튼을 걷는 `chat_update(blocks=[])` 에는 항상 text 를 함께 준다. 헬퍼는 text 를 인자로 받고, 콜백은 `body["message"]["text"]` 를 쓴다.
7. 명령/shell/프롬프트 완료 시 버튼을 걷을 때, task 가 stopped 면 "취소 요청됨" 문구를 유지하고(아무것도 하지 않음) 그 외에만 원래 문구로 update 한다.
8. 테스트 추가: 명령과 shell 을 실행 중 취소하면 취소 안내만 나가고 실패는 안 나감 / shell 대기 중 취소 시 spawn 없음 / spawn 직후 재검사에서 terminate 호출 / DB 결과 게시 후 진행 메시지 삭제.

## 구현 단계

1. `handlers.py`: `_BOOT_NONCE`, `_TASK_CANCEL_ACTION_ID`, `_build_progress_blocks(text, task_id)` (section 텍스트 3000자 제한), 
   `_post_progress(say, text, task_id)` (blocks 게시 + 평문 폴백, 응답 반환), `_clear_progress_buttons(client, channel, ts)` 추가
   -> verify: 단위 테스트로 blocks 구조와 value 확인.
2. 각 시작 알림을 `_post_progress` 로 교체, ts 보관 -> verify: 기존 테스트 통과 + 새 테스트에서 blocks 확인.
3. `task_cancel` 액션 콜백 (`_handle_task_cancel` 을 모듈 함수로 두어 테스트 가능하게) -> verify: 게이트별 테스트.
4. 대기 중 / spawn 직전 가드 -> verify: semaphore 를 미리 점유한 상태에서 stop 후 풀어 주면 실행 함수(mock)가 호출되지 않는 테스트.
5. 질문 태스크 user 기록 -> verify: 테스트.
6. CLAUDE.md 의 태스크 제어 절에 버튼 설명 추가.

## 테스트 (tests/test_task_cancel.py 신규)

- blocks 에 task_id, nonce, action_id 가 실린다 / 긴 텍스트가 3000자 이내로 잘린다
- 소유자 클릭 -> stop_task 호출, 버튼 제거 update
- 다른 사용자 -> 거부, stop 안 됨 / 소유자 검사가 status 검사보다 먼저
- 이미 종료 -> ephemeral, update 없음
- 없는 task_id, 다른 채널 -> 거부
- nonce 불일치 거부, 현재 nonce 수락
- 큐 대기 중 취소 시 실행 함수 미호출 (질문, DB 조회, 명령 각각 1개 이상)
- spawn 직전 가드: stopped task 로 answer_question / run_claude 호출 시 create_subprocess_exec 미호출
- invalid_blocks 시 평문 폴백

명령: `uv run pytest -q`, `uv run ruff check .` (설정이 있으면)

## 범위 밖 (후속 후보)

- 재시작 전 실행 중 태스크 대기와 재시작 완료 알림 (ra-ai `restart.py`)
- `/stop` 관리자 게이트 (현재 누구나 남의 태스크 중단 가능)
- 되비치는 사용자 텍스트의 `<!here>` 등 멘션 무력화 (ra-ai `slack_output.neutralize_mentions`)
