from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from slack_bot.task_manager import TaskManager


@pytest.fixture
def tm() -> TaskManager:
    return TaskManager()


@pytest.mark.asyncio
async def test_create_task_sequential_ids(tm: TaskManager):
    """create_task() 호출 시 순차 ID(001, 002, ...)가 생성된다."""
    t1 = await tm.create_task("proj", "cmd", "", "user", "ch")
    t2 = await tm.create_task("proj", "cmd", "", "user", "ch")
    assert t1.task_id == "001"
    assert t2.task_id == "002"


@pytest.mark.asyncio
async def test_create_task_concurrent_no_duplicate_ids(tm: TaskManager):
    """동시 create_task() 호출 시 ID 충돌이 없어야 한다."""
    tasks = await asyncio.gather(
        *[tm.create_task("proj", "cmd", "", "user", "ch") for _ in range(20)]
    )
    ids = [t.task_id for t in tasks]
    assert len(ids) == len(set(ids)), f"중복 ID 발생: {ids}"


@pytest.mark.asyncio
async def test_stop_task_success(tm: TaskManager):
    """실행 중인 태스크를 정상 중단할 수 있다."""
    task = await tm.create_task("proj", "cmd", "", "user", "ch")
    mock_proc = MagicMock()
    mock_proc.returncode = None
    task.process = mock_proc

    result = tm.stop_task(task.task_id)
    assert result is True
    assert task.status == "stopped"
    mock_proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_stop_task_already_exited_process(tm: TaskManager):
    """이미 종료된 프로세스에 terminate() 호출 시 ProcessLookupError가 발생해도 예외 없이 처리된다."""
    task = await tm.create_task("proj", "cmd", "", "user", "ch")
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.terminate.side_effect = ProcessLookupError()
    task.process = mock_proc

    result = tm.stop_task(task.task_id)
    assert result is True
    assert task.status == "stopped"


@pytest.mark.asyncio
async def test_stop_task_nonexistent(tm: TaskManager):
    """존재하지 않는 태스크 ID로 stop_task() 호출 시 False 반환."""
    assert tm.stop_task("999") is False


@pytest.mark.asyncio
async def test_cleanup_old(tm: TaskManager):
    """완료 후 max_age 초과 태스크는 cleanup_old()에서 제거된다."""
    task = await tm.create_task("proj", "cmd", "", "user", "ch")
    tm.complete_task(task.task_id, True)
    # start_time을 과거로 조작
    task.start_time -= 3600

    tm.cleanup_old(max_age=1800)
    assert tm.get_task(task.task_id) is None
