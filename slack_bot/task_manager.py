from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass, field


def terminate_process(proc: asyncio.subprocess.Process) -> None:
    """서브프로세스를 종료한다. 자기 프로세스 그룹의 리더면 그룹 전체에 SIGTERM 을 보낸다.

    shell 은 start_new_session=True 로 띄워 sh 가 그룹 리더가 된다. sh 만 죽이면 그 아래
    명령이 고아로 남으므로 그룹째 끝낸다. getpgid(pid) == pid 일 때만 killpg 하는 것은
    그룹을 새로 만들지 않은 자식(claude -p 등)의 경우 봇 자신의 그룹을 죽이지 않기 위해서다.
    """
    try:
        pid = proc.pid
        if isinstance(pid, int) and pid > 0 and os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        pass


@dataclass
class TaskInfo:
    task_id: str
    project_name: str
    command: str
    args: str
    user: str
    channel: str
    start_time: float
    thread_ts: str = ""
    status: str = "running"  # running | completed | failed | stopped
    output_lines: list[str] = field(default_factory=list)
    process: asyncio.subprocess.Process | None = None
    complete_time: float | None = None
    # [취소] 버튼이 달린 시작 알림 메시지의 ts. 완료 시 버튼을 걷거나 메시지를 지울 때 쓴다.
    progress_ts: str = ""
    # stop_task 가 set 한다. 세마포어 대기 중인 태스크가 슬롯을 기다리지 않고 바로 빠져나오게 한다.
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    def attach_process(self, proc: asyncio.subprocess.Process) -> bool:
        """서브프로세스를 등록한다. 이미 취소된 태스크면 바로 종료하고 True 를 돌려준다.

        spawn 을 기다리는 동안 stop 되면 stop_task 는 process 를 보지 못해 종료하지 못한다.
        """
        self.process = proc
        if self.status == "stopped":
            terminate_process(proc)
            return True
        return False

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    @property
    def elapsed_display(self) -> str:
        s = int(self.elapsed)
        if s < 60:
            return f"{s}초"
        return f"{s // 60}분 {s % 60}초"

    @property
    def output_text(self) -> str:
        return "".join(self.output_lines)


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskInfo] = {}
        self._counter: int = 0
        self._lock = asyncio.Lock()

    def _create_task_unlocked(
        self,
        project_name: str,
        command: str,
        args: str,
        user: str,
        channel: str,
        thread_ts: str = "",
    ) -> TaskInfo:
        """락을 획득한 호출자 전용 — 태스크를 생성해 등록한다."""
        self._counter += 1
        task_id = f"{self._counter:03d}"
        task = TaskInfo(
            task_id=task_id,
            project_name=project_name,
            command=command,
            args=args,
            user=user,
            channel=channel,
            thread_ts=thread_ts,
            start_time=time.time(),
        )
        self._tasks[task_id] = task
        return task

    def _find_running_duplicate_unlocked(
        self, project_name: str, command: str, args: str
    ) -> TaskInfo | None:
        """락 없이 동일 (project, command, args) running 태스크를 스캔한다."""
        for t in self._tasks.values():
            if (
                t.status == "running"
                and t.project_name == project_name
                and t.command == command
                and t.args == args
            ):
                return t
        return None

    async def create_task(
        self,
        project_name: str,
        command: str,
        args: str,
        user: str,
        channel: str,
        thread_ts: str = "",
    ) -> TaskInfo:
        async with self._lock:
            return self._create_task_unlocked(
                project_name, command, args, user, channel, thread_ts
            )

    async def create_task_if_absent(
        self,
        project_name: str,
        command: str,
        args: str,
        user: str,
        channel: str,
        thread_ts: str = "",
    ) -> tuple[TaskInfo, bool]:
        """락 안에서 running 중복을 확인하고, 없으면 새로 생성한다.

        Returns (task, created):
          - created=True  → 새 태스크를 만들었다(호출자가 실행 진행).
          - created=False → 이미 동일 running 태스크가 있어 그걸 반환(호출자가 스킵).

        find→create 사이 await 양보점으로 생기던 중복 생성 레이스를 self._lock
        한 번으로 닫는다.
        """
        async with self._lock:
            existing = self._find_running_duplicate_unlocked(
                project_name, command, args
            )
            if existing is not None:
                return existing, False
            task = self._create_task_unlocked(
                project_name, command, args, user, channel, thread_ts
            )
            return task, True

    def get_task(self, task_id: str) -> TaskInfo | None:
        return self._tasks.get(task_id)

    def find_running_duplicate(
        self, project_name: str, command: str, args: str
    ) -> TaskInfo | None:
        """동일 (project, command, args)로 실행 중인 태스크가 있으면 반환.

        같은 요청(예: `harness TRD-128`)이 중복 전달돼 `claude -p`가 겹쳐
        실행되면 토큰이 배수로 소모되므로, 이미 실행 중인 동일 작업을 찾아
        호출자가 재실행을 건너뛸 수 있게 한다.
        """
        return self._find_running_duplicate_unlocked(project_name, command, args)

    def get_running_tasks(self) -> list[TaskInfo]:
        return [t for t in self._tasks.values() if t.status == "running"]

    def get_tasks_for_channel(self, channel: str) -> list[TaskInfo]:
        """채널의 실행 중 태스크 반환. 없으면 최근 완료 태스크 반환."""
        running = [
            t
            for t in self._tasks.values()
            if t.channel == channel and t.status == "running"
        ]
        if running:
            return running
        # 실행 중인 게 없으면 최근 완료/실패 태스크 중 10분 이내 것
        recent = [
            t
            for t in self._tasks.values()
            if t.channel == channel
            and t.status in ("completed", "failed", "stopped")
            and t.elapsed < 600
        ]
        return sorted(recent, key=lambda t: t.start_time, reverse=True)[:3]

    def stop_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.status != "running":
            return False
        if task.process and task.process.returncode is None:
            terminate_process(task.process)
        task.status = "stopped"
        task.complete_time = time.time()
        task.stop_event.set()
        return True

    def complete_task(self, task_id: str, success: bool) -> None:
        task = self._tasks.get(task_id)
        if task is None or task.status == "stopped":
            return
        task.status = "completed" if success else "failed"
        task.complete_time = time.time()

    def cleanup_old(self, max_age: float = 1800) -> None:
        """완료 후 max_age초 지난 태스크 제거."""
        now = time.time()
        to_remove = [
            tid
            for tid, t in self._tasks.items()
            if t.status != "running"
            and t.complete_time is not None
            and (now - t.complete_time) > max_age
        ]
        for tid in to_remove:
            del self._tasks[tid]
