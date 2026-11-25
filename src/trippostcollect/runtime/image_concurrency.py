"""同一帖正文图的有界并发下载。帖与帖之间仍由调用方逐条等待。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from typing import TypeVar

from trippostcollect.runtime.image_retry import (
    ImageDownloadFetchError,
    is_runtime_blocking_image_error,
)


IMAGE_DOWNLOAD_CONCURRENCY = 3

T = TypeVar("T")


class ImageDownloadBatchError(Exception):
    """本帖最终抛出的那一次图片失败。取消或未启动的图片不产生这个错误。"""

    def __init__(
        self,
        message: str,
        *,
        source_index: int,
        code: str,
        attempts: int,
        http_status: int | None,
    ) -> None:
        super().__init__(message)
        self.source_index = source_index
        self.code = code
        self.attempts = attempts
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class _Ok:
    asset: object
    content: bytes
    attempts: int


@dataclass(frozen=True, slots=True)
class _Err:
    source_index: int
    code: str
    attempts: int
    http_status: int | None
    cause: BaseException | None


def _source_index(asset: object) -> int:
    return int(asset["source_index"])  # type: ignore[index]


def _raise_selected(failure: _Err) -> None:
    error = ImageDownloadBatchError(
        f"image download failed: source_index={failure.source_index}, code={failure.code}",
        source_index=failure.source_index,
        code=failure.code,
        attempts=failure.attempts,
        http_status=failure.http_status,
    )
    if failure.cause is None:
        raise error
    raise error from failure.cause


async def _cancel_outstanding(tasks: set[asyncio.Task[object]]) -> None:
    """取消仍在跑的下载并等到它们结束。调用方处于取消中时也可以等待。"""

    pending = [task for task in list(tasks) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    tasks.clear()


async def download_images_bounded(
    assets: Sequence[T],
    *,
    fetch: Callable[[T], Awaitable[tuple[bytes | None, int]]],
    on_success: Callable[[T, bytes, int], Awaitable[object] | object],
) -> list[tuple[T, bytes, int]]:
    """按 source_index 顺序最多同时下载 3 张。

    `fetch` 是现有的 `fetch_image_bytes_with_retry`。成功后立刻调用 `on_success`
    （访问登记发生在这一刻）。返回值按 `source_index` 排序。

    `image_auth_required` / `image_rate_limited` 出现后不再启动新下载，并取消在途下载，
    然后抛出该错误。同一轮已经结束的多张阻断错误取 `source_index` 最小者。
    其他失败不再启动新下载，但会等在途下载结束：若其中出现阻断错误，改走上面的取消路径；
    否则抛出 `source_index` 最小的失败。返回 `None` 仍是 `image_download_retryable`。

    外层任务被取消时，取消并等待全部在途下载，再继续抛出 `CancelledError`。
    """

    if not assets:
        return []
    items = list(assets)
    inflight: set[asyncio.Task[object]] = set()
    try:
        return await _download(items, fetch, on_success, inflight)
    except asyncio.CancelledError:
        await _cancel_outstanding(inflight)
        raise
    except Exception:
        await _cancel_outstanding(inflight)
        raise


async def _download(
    assets: Sequence[T],
    fetch: Callable[[T], Awaitable[tuple[bytes | None, int]]],
    on_success: Callable[[T, bytes, int], Awaitable[object] | object],
    inflight: set[asyncio.Task[object]],
) -> list[tuple[T, bytes, int]]:
    successes: list[tuple[T, bytes, int]] = []
    failures: list[_Err] = []
    next_index = 0
    stop_launching = False

    async def run_one(asset: T) -> _Ok | _Err:
        try:
            content, attempts = await fetch(asset)
        except asyncio.CancelledError:
            raise
        except ImageDownloadFetchError as exc:
            return _Err(
                source_index=_source_index(asset),
                code=exc.code,
                attempts=exc.attempts,
                http_status=exc.http_status,
                cause=exc,
            )
        if content is None:
            return _Err(
                source_index=_source_index(asset),
                code="image_download_retryable",
                attempts=attempts,
                http_status=None,
                cause=None,
            )
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise asyncio.CancelledError()
        outcome = on_success(asset, content, attempts)
        if isawaitable(outcome):
            await outcome
        return _Ok(asset=asset, content=content, attempts=attempts)

    def launch() -> None:
        nonlocal next_index
        while (
            not stop_launching
            and next_index < len(assets)
            and len(inflight) < IMAGE_DOWNLOAD_CONCURRENCY
        ):
            asset = assets[next_index]
            next_index += 1
            inflight.add(asyncio.create_task(run_one(asset)))

    launch()
    while inflight:
        done, _pending = await asyncio.wait(set(inflight), return_when=asyncio.FIRST_COMPLETED)
        done.update(task for task in inflight if task.done())
        blocking: list[_Err] = []
        ordinary: list[_Err] = []
        unexpected: BaseException | None = None
        for task in done:
            inflight.discard(task)
            if task.cancelled():
                unexpected = asyncio.CancelledError()
                break
            exc = task.exception()
            if exc is not None:
                unexpected = exc
                break
            outcome = task.result()
            if isinstance(outcome, _Err):
                if is_runtime_blocking_image_error(outcome.code):
                    blocking.append(outcome)
                else:
                    ordinary.append(outcome)
                continue
            assert isinstance(outcome, _Ok)
            successes.append((outcome.asset, outcome.content, outcome.attempts))  # type: ignore[arg-type]
        if unexpected is not None:
            await _cancel_outstanding(inflight)
            raise unexpected
        if blocking:
            chosen = min(blocking, key=lambda item: item.source_index)
            await _cancel_outstanding(inflight)
            _raise_selected(chosen)
        if ordinary:
            stop_launching = True
            failures.extend(ordinary)
        launch()

    if failures:
        blocking_done = [item for item in failures if is_runtime_blocking_image_error(item.code)]
        chosen = min(blocking_done or failures, key=lambda item: item.source_index)
        _raise_selected(chosen)
    successes.sort(key=lambda item: _source_index(item[0]))
    return successes
