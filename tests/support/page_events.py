"""与 Playwright 事件语义一致的最小假对象：同步处理器在 emit 内直接执行，不启动浏览器、不联网。"""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from typing import Any, Callable


class Emitter:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[..., Any]]] = defaultdict(list)

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        # 与 Playwright 相同：非绑定方法的回调要能设属性（其包装器缓存在回调对象上），内置方法会在此失败。
        if not inspect.ismethod(handler):
            setattr(handler, "__impl__", getattr(handler, "__impl__", None))
        self._handlers[event].append(handler)

    def emit(self, event: str, *args: Any) -> None:
        for handler in list(self._handlers[event]):
            handler(*args)

    def handler_count(self, event: str) -> int:
        return len(self._handlers[event])


class FakeFrame:
    def __init__(self, page: Any) -> None:
        self.page = page


class ServiceWorkerFrame:
    """Playwright 对 service worker 请求访问 frame 时抛错。"""

    @property
    def page(self) -> Any:
        raise RuntimeError("Service Worker requests do not have an associated frame.")


class FakeRequest:
    def __init__(
        self,
        *,
        method: str,
        url: str,
        resource_type: str,
        frame: Any,
        post_data_json: Any = None,
        failure: str | None = None,
    ) -> None:
        self.method = method
        self.url = url
        self.resource_type = resource_type
        self._frame = frame
        self._post_data_json = post_data_json
        self.failure = failure

    @property
    def frame(self) -> Any:
        if isinstance(self._frame, ServiceWorkerFrame):
            raise RuntimeError("Service Worker requests do not have an associated frame.")
        return self._frame

    @property
    def post_data_json(self) -> Any:
        if isinstance(self._post_data_json, Exception):
            raise self._post_data_json
        return self._post_data_json


class FakeResponse:
    """`body()` 可延迟到 `release()` 后返回，也可设为抛错，用来模拟读体时序与缓冲淘汰。"""

    def __init__(
        self,
        request: FakeRequest,
        *,
        status: int = 200,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        delayed: bool = False,
        body_error: Exception | None = None,
    ) -> None:
        self.request = request
        self.url = request.url
        self.status = status
        self.headers = dict(headers or {})
        self._body = body
        self._body_error = body_error
        self._released = asyncio.Event()
        if not delayed:
            self._released.set()
        self.body_calls = 0

    @property
    def frame(self) -> Any:
        return self.request.frame

    def release(self) -> None:
        self._released.set()

    async def body(self) -> bytes:
        self.body_calls += 1
        await self._released.wait()
        if self._body_error is not None:
            raise self._body_error
        return self._body


class FakePage(Emitter):
    def __init__(self, url: str = "about:blank", *, state: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.url = url
        self.state = dict(state or {})
        self.evaluate_calls: list[tuple[str, Any]] = []

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.evaluate_calls.append((script, arg))
        value = self.state.get(script)
        return value(arg) if callable(value) else value

    def request(
        self,
        method: str,
        url: str,
        *,
        resource_type: str = "xhr",
        post_data_json: Any = None,
        frame_page: Any = None,
    ) -> FakeRequest:
        return FakeRequest(
            method=method,
            url=url,
            resource_type=resource_type,
            frame=FakeFrame(frame_page if frame_page is not None else self),
            post_data_json=post_data_json,
        )

    def respond(self, request: FakeRequest, **kwargs: Any) -> FakeResponse:
        response = FakeResponse(request, **kwargs)
        self.emit("response", response)
        return response

    def fail(self, request: FakeRequest, failure: str) -> None:
        request.failure = failure
        self.emit("requestfailed", request)


class _ExpectPage:
    """`async with context.expect_page(timeout=…) as info: …; page = await info.value` 的最小替身；
    与 Playwright 一样在离开 `async with` 时等待新页面，超时抛 TimeoutError。"""

    def __init__(self, context: "FakeContext", timeout_seconds: float) -> None:
        self._context = context
        self._timeout = timeout_seconds
        self._future: asyncio.Future[Any] | None = None

    async def __aenter__(self) -> "_ExpectPage":
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        def handler(page: Any) -> None:
            if not future.done():
                future.set_result(page)

        self._context.on("page", handler)
        self._future = future
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            assert self._future is not None
            await asyncio.wait_for(asyncio.shield(self._future), self._timeout)
        return False

    @property
    def value(self) -> "asyncio.Future[Any]":
        assert self._future is not None
        return self._future


class FakeContext(Emitter):
    def __init__(self) -> None:
        super().__init__()
        self.pages: list[FakePage] = []

    def new_page(self, url: str = "about:blank", *, page: FakePage | None = None) -> FakePage:
        page = page if page is not None else FakePage(url)
        self.pages.append(page)
        self.emit("page", page)
        return page

    def expect_page(self, *, timeout: float = 30_000) -> _ExpectPage:
        return _ExpectPage(self, timeout / 1000)
