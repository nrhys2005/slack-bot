from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class TaskInfo:
    task_id: str
    project_name: str
    command: str
    args: str
    user: str
    channel: str
    start_time: float
    status: str = "running"  # running | completed | failed | stopped
    output_lines: list[str] = field(default_factory=list)
    process: asyncio.subprocess.Process | None = None

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

    async def create_task(
        self,
        project_name: str,
        command: str,
        args: str,
        user: str,
        channel: str,
    ) -> TaskInfo:
        async with self._lock:
            self._counter += 1
            task_id = f"{self._counter:03d}"
            task = TaskInfo(
                task_id=task_id,
                project_name=project_name,
                command=command,
                args=args,
                user=user,
                channel=channel,
                start_time=time.time(),
            )
            self._tasks[task_id] = task
            return task

    def get_task(self, task_id: str) -> TaskInfo | None:
        return self._tasks.get(task_id)

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
            try:
                task.process.terminate()
            except ProcessLookupError:
                pass
        task.status = "stopped"
        return True

    def complete_task(self, task_id: str, success: bool) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = "completed" if success else "failed"

    def cleanup_old(self, max_age: float = 1800) -> None:
        """완료 후 max_age초 지난 태스크 제거."""
        now = time.time()
        to_remove = [
            tid
            for tid, t in self._tasks.items()
            if t.status != "running" and (now - t.start_time) > max_age
        ]
        for tid in to_remove:
            del self._tasks[tid]
