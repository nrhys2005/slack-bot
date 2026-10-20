from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

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
async def test_complete_task_preserves_stopped(tm: TaskManager):
    """stop_task로 중단된 태스크는 complete_task에서 상태가 덮어쓰여지지 않는다."""
    task = await tm.create_task("proj", "cmd", "", "user", "ch")
    mock_proc = MagicMock()
    mock_proc.returncode = None
    task.process = mock_proc

    assert tm.stop_task(task.task_id) is True
    assert task.status == "stopped"

    # 백그라운드 흐름이 뒤늦게 complete_task를 호출해도 stopped가 유지돼야 함
    tm.complete_task(task.task_id, True)
    assert task.status == "stopped"


@pytest.mark.asyncio
async def test_cleanup_old(tm: TaskManager):
    """완료 후 max_age 초과 태스크는 cleanup_old()에서 제거된다."""
    task = await tm.create_task("proj", "cmd", "", "user", "ch")
    tm.complete_task(task.task_id, True)
    # complete_time을 과거로 조작
    task.complete_time -= 3600

    tm.cleanup_old(max_age=1800)
    assert tm.get_task(task.task_id) is None


@pytest.mark.asyncio
async def test_find_running_duplicate_matches_running(tm: TaskManager):
    """동일 (project, command, args)로 실행 중인 태스크를 찾아 반환한다."""
    task = await tm.create_task("trader", "harness", "TRD-128", "u", "ch")

    found = tm.find_running_duplicate("trader", "harness", "TRD-128")
    assert found is task


@pytest.mark.asyncio
async def test_find_running_duplicate_ignores_non_running(tm: TaskManager):
    """완료/중단된 태스크는 중복으로 보지 않는다 (재실행 허용)."""
    task = await tm.create_task("trader", "harness", "TRD-128", "u", "ch")
    tm.complete_task(task.task_id, True)

    assert tm.find_running_duplicate("trader", "harness", "TRD-128") is None


@pytest.mark.asyncio
async def test_find_running_duplicate_distinguishes_args(tm: TaskManager):
    """project/command가 같아도 args가 다르면 별개 작업으로 본다."""
    await tm.create_task("trader", "harness", "TRD-128", "u", "ch")

    assert tm.find_running_duplicate("trader", "harness", "TRD-999") is None
    assert tm.find_running_duplicate("trader", "review", "TRD-128") is None
    assert tm.find_running_duplicate("alert-cow", "harness", "TRD-128") is None


@pytest.mark.asyncio
async def test_create_task_if_absent_creates_when_empty(tm: TaskManager):
    """중복이 없으면 새 태스크를 생성하고 created=True를 반환한다."""
    task, created = await tm.create_task_if_absent(
        "trader", "harness", "TRD-128", "u", "ch"
    )
    assert created is True
    assert task.task_id == "001"
    assert tm.get_task("001") is task


@pytest.mark.asyncio
async def test_create_task_if_absent_skips_when_duplicate(tm: TaskManager):
    """동일 running 태스크가 있으면 기존 태스크를 created=False로 반환한다."""
    first, created1 = await tm.create_task_if_absent(
        "trader", "harness", "TRD-128", "u", "ch"
    )
    second, created2 = await tm.create_task_if_absent(
        "trader", "harness", "TRD-128", "u", "ch"
    )
    assert created1 is True
    assert created2 is False
    assert second is first
    assert len(tm.get_running_tasks()) == 1


@pytest.mark.asyncio
async def test_create_task_if_absent_allows_rerun_after_completion(tm: TaskManager):
    """완료된 태스크는 중복이 아니므로 동일 키 재실행을 허용한다."""
    first, created1 = await tm.create_task_if_absent(
        "trader", "harness", "TRD-128", "u", "ch"
    )
    tm.complete_task(first.task_id, True)

    second, created2 = await tm.create_task_if_absent(
        "trader", "harness", "TRD-128", "u", "ch"
    )
    assert created2 is True
    assert second is not first


@pytest.mark.asyncio
async def test_create_task_if_absent_concurrent_single_create(tm: TaskManager):
    """동일 키로 동시 호출해도 정확히 1개만 생성된다 (find→create 레이스 원자화)."""
    results = await asyncio.gather(
        *[
            tm.create_task_if_absent("trader", "harness", "TRD-128", "u", "ch")
            for _ in range(20)
        ]
    )
    created_flags = [created for _, created in results]
    assert created_flags.count(True) == 1

    created_task = next(task for task, created in results if created)
    # 모든 호출이 동일 태스크 인스턴스를 받는다
    assert all(task is created_task for task, _ in results)
    assert len(tm.get_running_tasks()) == 1
