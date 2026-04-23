# SB-PROC-01: subprocess 안정화

## 요구사항 요약
runner.py, chat.py, db_query.py의 subprocess 실행 안정성 개선:
1. timeout 없이 영원히 대기하는 문제 해결
2. stdout/stderr 동시 읽기로 deadlock 방지
3. assert → raise 변경으로 프로덕션 안전성 확보

## 영향 분석

### 수정 파일
- `slack_bot/runner.py` — stdout/stderr 동시 읽기, timeout, assert→raise
- `slack_bot/chat.py` — proc.communicate() timeout 추가
- `slack_bot/db_query.py` — assert→raise 변경, wait() timeout 추가

### 영향받는 의존성
- `handlers.py` → `run_claude()`, `answer_question()`, `run_db_query()` 호출부 — 반환값/시그니처 변경 없으므로 수정 불필요

## 구현 단계

### Step 1: runner.py — stdout/stderr 동시 읽기 + timeout

**현재 문제** (runner.py:54-64):
- stdout을 다 읽은 후 stderr를 읽는 순차 구조
- stderr 버퍼(~64KB)가 차면 프로세스가 block → deadlock
- `proc.wait()`에 timeout 없음

**수정 방안**:
1. `assert proc.stdout is not None` → `if proc.stdout is None: raise RuntimeError(...)` (line 54, 61)
2. stdout 라인별 스트리밍은 유지하되, stderr는 별도 태스크로 동시 읽기
3. `asyncio.wait_for(proc.wait(), timeout=SUBPROCESS_TIMEOUT)` 추가
4. timeout 발생 시 process.kill() 후 TimeoutError를 RunResult(success=False)로 변환

**상수 추가**:
```python
SUBPROCESS_TIMEOUT = 3600  # 1시간. harness 파이프라인은 오래 걸릴 수 있음
```

**수정 후 구조**:
```python
if proc.stdout is None:
    raise RuntimeError("stdout pipe not created")
if proc.stderr is None:
    raise RuntimeError("stderr pipe not created")

# stderr를 동시에 읽는 태스크
async def _drain_stderr():
    data = await proc.stderr.read()
    return data

stderr_task = asyncio.create_task(_drain_stderr())

# stdout 라인별 스트리밍 (기존과 동일)
async for line in proc.stdout:
    task.output_lines.append(line.decode(errors="replace"))

# timeout 적용하여 프로세스 종료 대기
try:
    await asyncio.wait_for(proc.wait(), timeout=SUBPROCESS_TIMEOUT)
except asyncio.TimeoutError:
    proc.kill()
    await proc.wait()
    # timeout 결과 반환
    return RunResult(success=False, output="...(timeout)...", return_code=-1)

stderr_data = await stderr_task
```

### Step 2: chat.py — communicate() timeout 추가

**현재 문제** (chat.py:128):
- `proc.communicate()`에 timeout 없음 → CLI hang 시 봇 영구 대기

**수정 방안**:
1. `CHAT_TIMEOUT = 300` 상수 추가 (질문 답변은 5분이면 충분)
2. `asyncio.wait_for(proc.communicate(), timeout=CHAT_TIMEOUT)` 사용
3. TimeoutError 시 proc.kill() 후 에러 메시지 반환

**수정 후 구조**:
```python
CHAT_TIMEOUT = 300  # 5분

try:
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=CHAT_TIMEOUT
    )
except asyncio.TimeoutError:
    proc.kill()
    await proc.communicate()  # 리소스 정리
    logger.error("Claude CLI 응답 시간 초과 (%ds)", CHAT_TIMEOUT)
    return ":warning: 응답 시간이 초과되었습니다. 질문을 더 구체적으로 해주세요."
```

### Step 3: db_query.py — assert→raise + wait() timeout

**현재 문제** (db_query.py:269, 286):
- `assert proc.stdout/stderr is not None` → -O 실행 시 제거됨
- `proc.wait()`에 timeout 없음 (line 283)

**수정 방안**:
1. `assert` → `if ... is None: raise RuntimeError(...)` (line 269, 286)
2. `await proc.wait()` → `await asyncio.wait_for(proc.wait(), timeout=DB_QUERY_TIMEOUT)` (line 283)
3. `DB_QUERY_TIMEOUT = 120` 상수 추가 (DB 조회는 2분이면 충분)
4. TimeoutError 시 proc.kill() 후 에러 메시지 반환

### Step 4: decode 안전성

모든 `.decode()` 호출에 `errors="replace"` 추가:
- `runner.py:56` — `line.decode()` → `line.decode(errors="replace")`
- `runner.py:64` — `stderr_data.decode()` → `stderr_data.decode(errors="replace")`
- db_query.py는 이미 `errors="replace"` 사용 중 (line 299, 300, 304) — 변경 불필요

## 테스트 전략

### 성공 케이스
- runner.py: subprocess 정상 종료 시 stdout/stderr 모두 캡처 확인
- chat.py: 정상 응답 시 기존과 동일한 출력 확인
- db_query.py: 기존 동작과 동일 확인

### 실패 케이스
- timeout 초과 시 적절한 에러 메시지 반환 확인
- subprocess 비정상 종료 시 RunResult.success=False 확인

### 엣지 케이스
- stderr만 있고 stdout 없는 경우
- stdout에 non-UTF-8 바이트가 포함된 경우

## 완료 기준
- [ ] runner.py: stdout/stderr 동시 읽기, timeout, assert→raise, decode safety
- [ ] chat.py: communicate() timeout 추가
- [ ] db_query.py: assert→raise, wait() timeout 추가
- [ ] 기존 import/호출부 호환성 유지 (시그니처 변경 없음)
- [ ] ruff check/format 통과
- [ ] import 검증 통과
