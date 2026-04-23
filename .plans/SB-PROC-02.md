# SB-PROC-02: TaskManager 개선

## 요구사항 요약
task_manager.py의 안정성 및 메모리 관리 개선:
1. asyncio.Lock으로 태스크 ID 생성 경합조건 방지
2. terminate() 예외처리 추가
3. cleanup_old() 호출을 모든 핸들러로 확대

## 영향 분석

### 수정 파일
- `slack_bot/task_manager.py` — Lock 추가, terminate() try-except, cleanup_old() 관련
- `slack_bot/handlers.py` — /dev, /claude, /db 핸들러에서 cleanup_old() 호출 추가

### 영향받는 의존성
- `main.py` → TaskManager() 생성 — 변경 없음
- `chat.py` → TaskManager 사용 안 함 — 변경 없음

## 구현 단계

### Step 1: task_manager.py — asyncio.Lock 추가

**현재 문제** (task_manager.py:50):
- `self._counter += 1`이 atomic하지 않음
- 동시에 두 슬래시 커맨드가 들어오면 같은 task_id 생성 가능

**수정 방안**:
```python
class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskInfo] = {}
        self._counter: int = 0
        self._lock = asyncio.Lock()

    async def create_task(self, ...) -> TaskInfo:
        async with self._lock:
            self._counter += 1
            task_id = f"{self._counter:03d}"
            task = TaskInfo(...)
            self._tasks[task_id] = task
            return task
```

**주의**: `create_task`가 sync→async로 변경되므로 호출부(handlers.py)에서 `await` 추가 필요.

### Step 2: task_manager.py — terminate() 예외처리

**현재 문제** (task_manager.py:92):
- `task.process.terminate()`가 이미 종료된 프로세스에서 `ProcessLookupError` 발생 가능

**수정 방안**:
```python
def stop_task(self, task_id: str) -> bool:
    task = self._tasks.get(task_id)
    if task is None or task.status != "running":
        return False
    if task.process and task.process.returncode is None:
        try:
            task.process.terminate()
        except ProcessLookupError:
            pass
    task.status = "stopped"
    return True
```

### Step 3: handlers.py — cleanup_old() 호출 확대

**현재 문제**:
- `cleanup_old()`가 `app_mention`(line 278)과 `handle_dm_message`(line 365)에서만 호출
- `/dev`, `/claude`, `/db` 핸들러에서는 호출 안 됨 → 태스크 누적

**수정 방안**:
- `/dev` 핸들러 시작부에 `task_manager.cleanup_old()` 추가
- `/claude` 핸들러 시작부에 `task_manager.cleanup_old()` 추가
- `/db` 핸들러 시작부에 `task_manager.cleanup_old()` 추가

## 테스트 전략

### 성공 케이스
- create_task() 호출 시 순차 ID 생성 확인
- stop_task() 정상 동작 확인

### 실패 케이스
- stop_task()에서 이미 종료된 프로세스 terminate() 시 예외 안 남 확인

### 엣지 케이스
- 동시 create_task() 호출 시 ID 충돌 없음 확인

## 완료 기준
- [ ] create_task()가 async 메서드로 변경, asyncio.Lock 사용
- [ ] handlers.py의 create_task() 호출부에 await 추가
- [ ] stop_task()에 ProcessLookupError 예외처리
- [ ] /dev, /claude, /db 핸들러에 cleanup_old() 호출 추가
- [ ] ruff check/format 통과
- [ ] import 검증 통과
