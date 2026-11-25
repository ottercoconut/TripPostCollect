"""同一帖正文图的有界并发：上限、完成顺序、失败取舍和取消。"""

from __future__ import annotations

import asyncio

import pytest

from trippostcollect.runtime.image_concurrency import (
    IMAGE_DOWNLOAD_CONCURRENCY,
    ImageDownloadBatchError,
    download_images_bounded,
)
from trippostcollect.runtime.image_retry import ImageDownloadFetchError


def _assets(count: int) -> list[dict[str, int]]:
    return [{"source_index": index} for index in range(count)]


async def _until(predicate, timeout: float = 1.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def _http_error(status: int, code: str) -> ImageDownloadFetchError:
    return ImageDownloadFetchError(
        f"HTTP {status}", code=code, retryable=False, http_status=status,
    )


@pytest.mark.asyncio
async def test_in_flight_downloads_reach_and_do_not_exceed_three():
    assert IMAGE_DOWNLOAD_CONCURRENCY == 3
    current = 0
    peak = 0
    release = asyncio.Event()
    started = asyncio.Event()

    async def fetch(asset):
        nonlocal current, peak
        del asset
        current += 1
        peak = max(peak, current)
        if current >= IMAGE_DOWNLOAD_CONCURRENCY:
            started.set()
        try:
            await release.wait()
            return b"img", 1
        finally:
            current -= 1

    task = asyncio.create_task(download_images_bounded(
        _assets(5), fetch=fetch, on_success=lambda *_args: None,
    ))
    try:
        assert await _until(lambda: started.is_set())
        await asyncio.sleep(0.05)
        assert current == 3
        assert peak == 3
        release.set()
        result = await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert [asset["source_index"] for asset, _content, _attempts in result] == [0, 1, 2, 3, 4]
    assert peak == 3


@pytest.mark.asyncio
async def test_success_callback_is_immediate_and_results_follow_source_index():
    gates = [asyncio.Event() for _ in range(3)]
    entered: list[int] = []
    recorded: list[int] = []

    async def fetch(asset):
        index = asset["source_index"]
        entered.append(index)
        await gates[index].wait()
        return b"img", 1

    def on_success(asset, content, attempts):
        del content, attempts
        recorded.append(asset["source_index"])

    task = asyncio.create_task(download_images_bounded(
        _assets(3), fetch=fetch, on_success=on_success,
    ))
    try:
        assert await _until(lambda: len(entered) == 3)
        gates[2].set()
        assert await _until(lambda: recorded == [2])
        gates[0].set()
        assert await _until(lambda: recorded == [2, 0])
        gates[1].set()
        result = await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert recorded == [2, 0, 1]
    assert [asset["source_index"] for asset, _content, _attempts in result] == [0, 1, 2]


@pytest.mark.asyncio
async def test_rate_limit_beats_earlier_404_and_unfinished_images_are_not_recorded():
    cancelled: list[int] = []
    recorded: list[int] = []
    started: list[int] = []

    async def fetch(asset):
        index = asset["source_index"]
        started.append(index)
        await asyncio.sleep(0)
        if index == 0:
            raise _http_error(404, "image_source_unavailable")
        if index == 1:
            raise _http_error(429, "image_rate_limited")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(index)
            raise
        return b"img", 1

    with pytest.raises(ImageDownloadBatchError) as exc_info:
        await asyncio.wait_for(download_images_bounded(
            _assets(4),
            fetch=fetch,
            on_success=lambda asset, _content, _attempts: recorded.append(asset["source_index"]),
        ), 2)

    assert exc_info.value.source_index == 1
    assert exc_info.value.code == "image_rate_limited"
    assert exc_info.value.http_status == 429
    assert exc_info.value.attempts == 1
    assert recorded == []
    assert started[:3] == [0, 1, 2]
    assert 3 not in started
    assert 2 in cancelled


@pytest.mark.asyncio
async def test_ordinary_failures_raise_the_smallest_source_index():
    started: list[int] = []
    release = [asyncio.Event() for _ in range(4)]

    async def fetch(asset):
        index = asset["source_index"]
        started.append(index)
        await release[index].wait()
        status = 404 if index == 0 else 410 if index == 1 else 500
        raise _http_error(status, "image_source_unavailable")

    task = asyncio.create_task(download_images_bounded(
        _assets(4), fetch=fetch, on_success=lambda *_args: None,
    ))
    try:
        assert await _until(lambda: len(started) >= 3)
        assert 3 not in started
        release[1].set()
        await asyncio.sleep(0.05)
        assert 3 not in started
        release[2].set()
        release[0].set()
        with pytest.raises(ImageDownloadBatchError) as exc_info:
            await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert exc_info.value.source_index == 0
    assert exc_info.value.http_status == 404
    assert exc_info.value.code == "image_source_unavailable"
    assert 3 not in started


@pytest.mark.asyncio
async def test_blocking_error_cancels_in_flight_downloads():
    cancelled: list[int] = []
    recorded: list[int] = []

    async def fetch(asset):
        index = asset["source_index"]
        if index == 1:
            await asyncio.sleep(0)
            raise _http_error(401, "image_auth_required")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(index)
            raise
        return b"img", 1

    with pytest.raises(ImageDownloadBatchError) as exc_info:
        await asyncio.wait_for(download_images_bounded(
            _assets(3),
            fetch=fetch,
            on_success=lambda asset, _content, _attempts: recorded.append(asset["source_index"]),
        ), 2)
    assert exc_info.value.code == "image_auth_required"
    assert exc_info.value.source_index == 1
    assert exc_info.value.http_status == 401
    assert sorted(cancelled) == [0, 2]
    assert recorded == []
    leftover = [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]
    assert leftover == []


@pytest.mark.asyncio
async def test_first_finished_blocking_error_cancels_another_blocking_download():
    """先结束的阻断错误被抛出，序号更小但仍在途的另一张阻断下载会被取消。"""

    gates = [asyncio.Event(), asyncio.Event()]
    cancelled: list[int] = []

    async def fetch(asset):
        index = asset["source_index"]
        try:
            await gates[index].wait()
        except asyncio.CancelledError:
            cancelled.append(index)
            raise
        if index == 0:
            raise _http_error(403, "image_auth_required")
        raise _http_error(429, "image_rate_limited")

    task = asyncio.create_task(download_images_bounded(
        _assets(2), fetch=fetch, on_success=lambda *_args: None,
    ))
    try:
        assert await _until(lambda: len(gates) == 2)
        gates[1].set()
        with pytest.raises(ImageDownloadBatchError) as exc_info:
            await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert exc_info.value.source_index == 1
    assert exc_info.value.code == "image_rate_limited"
    assert cancelled == [0]


@pytest.mark.asyncio
async def test_empty_retryable_result_waits_for_in_flight_work():
    recorded: list[int] = []

    async def fetch(asset):
        await asyncio.sleep(0)
        if asset["source_index"] == 0:
            return None, 3
        return b"img", 2

    with pytest.raises(ImageDownloadBatchError) as exc_info:
        await download_images_bounded(
            _assets(2),
            fetch=fetch,
            on_success=lambda asset, _content, attempts: recorded.append((asset["source_index"], attempts)),
        )
    assert exc_info.value.source_index == 0
    assert exc_info.value.code == "image_download_retryable"
    assert exc_info.value.attempts == 3
    assert exc_info.value.http_status is None
    assert recorded == [(1, 2)]


@pytest.mark.asyncio
async def test_outer_cancellation_cancels_in_flight_work_and_leaves_no_tasks():
    cancelled: list[int] = []
    entered: list[int] = []

    async def fetch(asset):
        index = asset["source_index"]
        entered.append(index)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(index)
            await asyncio.sleep(0.02)
            raise
        return b"img", 1

    task = asyncio.create_task(download_images_bounded(
        _assets(3), fetch=fetch, on_success=lambda *_args: (_ for _ in ()).throw(AssertionError("recorded")),
    ))
    assert await _until(lambda: len(entered) == 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(cancelled) == [0, 1, 2]
    leftover = [item for item in asyncio.all_tasks() if item is not asyncio.current_task() and not item.done()]
    assert leftover == []
