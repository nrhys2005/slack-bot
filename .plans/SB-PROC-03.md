# SB-PROC-03: handler 안정성 강화

## 요구사항 요약
handlers.py의 에러 핸들링 및 동시실행 제어 개선:
1. answer_question() 호출을 try-except로 감싸기 (app_mention, DM 핸들러)
2. asyncio.create_task()에 예외 콜백 추가
3. asyncio.Semaphore로 동시 Claude CLI 실행 수 제한

## 영향 분석

### 수정 파일
- `slack_bot/handlers.py` — 모든 변경이 이 파일에 집중

### 영향받는 의존성
- 다른 모듈 변경 없음

## 구현 단계

### Step 1: answer_question() try-except 감싸기

**현재 문제** (handlers.py:281, 368):
- `app_mention` 핸들러에서 `answer_question()` 호출이 try-except 밖
- `handle_dm_message` 핸들러에서도 동일
- `answer_question()` 내부에 try-except가 있지만, 호출 전 인자 구성 중 예외 발생 가능

**수정 방안**:
```python
# app_mention 핸들러
try:
    answer = await answer_question(
        question, tasks, thread_history,
        wiki_project_path=wiki_path,
        db_backend_path=db_backend_path,
    )
except Exception:
    logger.exception("질문 답변 처리 중 에러")
    answer = ":warning: 질문 처리 중 오류가 발생했습니다."
```

DM 핸들러에서도 동일하게 적용.

### Step 2: asyncio.create_task() 예외 콜백 추가

**현재 문제** (handlers.py:82, 143, 220):
- `asyncio.create_task()`로 백그라운드 태스크를 생성하지만 예외 콜백 없음
- `_run_and_report()`와 `_run_db_query_and_report()` 내부에 try-except가 있지만, 최상위 예외(예: asyncio.CancelledError)가 빠져나갈 수 있음

**수정 방안**:
```python
def _log_task_exception(t: asyncio.Task) -> None:
    """백그라운드 태스크의 미처리 예외를 로깅."""
    if t.cancelled():
        return
    exc = t.exception()
    if exc is not None:
        logger.error("백그라운드 태스크 예외: %s", exc, exc_info=exc)

# 사용
bg_task = asyncio.create_task(...)
bg_task.add_done_callback(_log_task_exception)
```

### Step 3: asyncio.Semaphore로 동시실행 제한

**현재 문제**:
- 동시에 다수 사용자가 @mention 또는 /dev를 실행하면 claude 프로세스가 무제한 생성
- 시스템 리소스 고갈 가능

**수정 방안**:
```python
# register_handlers 함수 상단
MAX_CONCURRENT_CLAUDE = 5
_claude_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CLAUDE)
```

Semaphore 적용 대상:
- `_run_and_report()` — `async with _claude_semaphore:` 로 전체 실행부 감싸기
- `_run_db_query_and_report()` — 동일
- `answer_question()` 호출부 (app_mention, DM) — 동일

Semaphore 획득 실패 시(다른 요청이 점유 중) 대기하므로 별도 에러 처리 불필요.
다만 사용자에게 대기 중이라는 피드백이 없으므로, Semaphore 진입 전 로그만 남긴다.

## 테스트 전략

### 성공 케이스
- answer_question 정상 호출 시 기존 동작 유지

### 실패 케이스
- answer_question이 예외를 던질 때 에러 메시지 반환 확인
- 백그라운드 태스크 예외 시 로깅 확인

### 엣지 케이스
- Semaphore 가득 찬 상태에서 요청이 대기 후 정상 처리 확인

## 완료 기준
- [ ] app_mention, DM 핸들러의 answer_question() 호출이 try-except로 감싸짐
- [ ] asyncio.create_task()에 _log_task_exception 콜백 추가 (3곳)
- [ ] asyncio.Semaphore(MAX_CONCURRENT_CLAUDE) 적용 (5곳)
- [ ] ruff check/format 통과
- [ ] import 검증 통과
