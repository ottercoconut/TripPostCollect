"""页面取数通道：平台数据只能从这里取得，且只来自页面自身发出的请求或页面状态（L1）。

监听挂在本 Context 已登记的每个 Page 上，只接受 method、host、path 与 resource_type 都匹配
规则、且发起 frame 属于已登记页面的响应。Playwright 的 `page.request` / `context.request`
不会出现在这些事件里，所以通道在结构上只看得到页面自己的请求。

所有动作、截获、停顿、决定和诊断共用账本的一个单调序号。截获的序号在事件回调里同步分配
（先于读体的第一个 await），因此等于事件到达顺序；之后无论何时写访问行都沿用这个序号。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache, partial
from hashlib import sha256
import json
import re
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qsl, urlsplit

from trippostcollect.runtime.helpers import utc_now
from trippostcollect.runtime.visit_ledger import VisitLedger


IMAGE_WINDOW_MAX_HANDLES = 64
MAX_RETAINED_PER_RULE = 256
BODY_KINDS = frozenset({"json", "bytes", "none"})
_ID_SEGMENT_RE = re.compile(r"[0-9a-fA-F]{16,}|\d{5,}|[A-Za-z0-9_-]{20,}")


class CaptureChannelError(RuntimeError):
    """通道在事件回调中失败（如账本无法写入）；之后的公开调用一律拒绝。"""


def route_of(url: str) -> tuple[str, tuple[str, ...]]:
    """页面地址的路径模板与查询键名；ID 样式和编码过的段替换为占位符，不保留任何值。"""
    parts = urlsplit(str(url or ""))
    segments = []
    for segment in parts.path.split("/"):
        if not segment:
            segments.append(segment)
        elif "%" in segment or not segment.isascii():
            segments.append("<value>")
        elif _ID_SEGMENT_RE.fullmatch(segment):
            segments.append("<id>")
        else:
            segments.append(segment)
    route = "/".join(segments) or "/"
    keys = tuple(sorted({key for key, _ in parse_qsl(parts.query, keep_blank_values=True)}))
    return route, keys


@lru_cache(maxsize=64)
def _template_regex(template: str) -> re.Pattern[str]:
    pieces = re.split(r"(<[A-Za-z_][A-Za-z0-9_]*>)", template)
    pattern = "".join("[^/]+" if piece.startswith("<") else re.escape(piece) for piece in pieces)
    return re.compile(pattern)


def _host_matches(pattern: str, host: str) -> bool:
    host = host.lower()
    pattern = pattern.lower()
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix) and len(host) > len(suffix)
    return host == pattern


@dataclass(frozen=True)
class CaptureRule:
    """`host` 为精确主机或 `*.后缀`；`path` 为精确路径、含 `<name>` 段的模板，或 None（任意路径）。"""

    name: str
    stage: str
    method: str
    host: str
    path: str | None
    resource_types: frozenset[str]
    body: str
    host_exclude_prefixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.body not in BODY_KINDS:
            raise ValueError(f"unsupported capture body kind: {self.body}")

    def matches(self, method: str, url: str, resource_type: str) -> bool:
        if method.upper() != self.method.upper() or resource_type not in self.resource_types:
            return False
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not _host_matches(self.host, host):
            return False
        if any(host.startswith(prefix.lower()) for prefix in self.host_exclude_prefixes):
            return False
        if self.path is None:
            return True
        if "<" in self.path:
            return bool(_template_regex(self.path).fullmatch(parts.path))
        return parts.path == self.path

    def endpoint(self, url: str) -> str:
        """`METHOD host path`；path 只取规则声明的路径或模板，不写实际路径值。"""
        host = (urlsplit(url).hostname or "").lower()
        return f"{self.method.upper()} {host} {self.path or '*'}"


@dataclass(frozen=True)
class SentinelRule:
    """非规则端点的状态哨兵：只看状态码与验证头是否存在，不读响应体。

    `surface_check_statuses` 中的状态交给平台检查验证界面后再写诊断，其余状态立即写 `ignored`。
    """

    host: str
    resource_types: frozenset[str]
    statuses: frozenset[int]
    surface_check_statuses: frozenset[int] = frozenset()

    def matches(self, url: str, resource_type: str, status: int) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return (
            status in self.statuses
            and resource_type in self.resource_types
            and _host_matches(self.host, host)
        )


@dataclass(frozen=True)
class CapturedResponse:
    rule: CaptureRule
    seq: int
    page_role: str
    route: str
    query_keys: tuple[str, ...]
    endpoint: str
    status: int
    verify_headers_present: bool
    request_json: Any
    json: Any
    observed_at: str
    body_error: str = ""


@dataclass(frozen=True)
class CaptureFailure:
    rule: CaptureRule
    seq: int
    page_role: str
    route: str
    query_keys: tuple[str, ...]
    endpoint: str
    failure_text: str
    request_json: Any
    observed_at: str


@dataclass(frozen=True)
class SentinelHit:
    seq: int
    page_role: str
    status: int
    endpoint: str
    verify_headers_present: bool
    observed_at: str


@dataclass(frozen=True)
class CapturedImage:
    url: str
    seq: int
    page_role: str
    status: int
    content: bytes = field(repr=False)
    sha256: str
    observed_at: str


@dataclass(frozen=True)
class _ImageExpectation:
    asset_key: str
    subject_id: str
    trigger: str | None
    attempt: int


@dataclass
class _HeldImage:
    rule: CaptureRule
    seq: int
    page_role: str
    route: str
    query_keys: tuple[str, ...]
    observed_at: str
    response: Any


def _request_json(request: Any) -> Any:
    try:
        return request.post_data_json
    except Exception:
        return None


class ImageWindow:
    """一篇笔记的图片窗口：先持有响应句柄、不读体，期望 URL 登记后只对精确匹配的句柄读体。"""

    def __init__(self, channel: PageCaptureChannel, page_role: str, max_handles: int) -> None:
        self._channel = channel
        self.page_role = page_role
        self._max_handles = max_handles
        self._held: OrderedDict[str, _HeldImage] = OrderedDict()
        self._expected: dict[str, _ImageExpectation] = {}
        self._results: dict[str, tuple[CapturedImage, str]] = {}
        self._reading: set[str] = set()
        self.read_errors = 0
        self.closed = False

    @property
    def held_count(self) -> int:
        return len(self._held)

    def _offer(self, held: _HeldImage, url: str) -> None:
        if self.closed or held.page_role != self.page_role or url in self._results:
            return
        if url in self._expected:
            self._start_read(url, held)
            return
        self._held.pop(url, None)
        self._held[url] = held
        while len(self._held) > self._max_handles:
            self._held.popitem(last=False)

    def expect(
        self,
        assets: Mapping[str, str],
        *,
        subject_id: str,
        trigger: str | None,
        attempt: int = 1,
    ) -> None:
        """登记期望 URL（url → 资产键）；可多次调用合并，已截获的字节保留。"""
        self._channel._raise_if_faulted()
        if self.closed:
            raise RuntimeError("image window is closed")
        for url, asset_key in assets.items():
            if url in self._results:
                continue
            self._expected[url] = _ImageExpectation(
                asset_key=str(asset_key), subject_id=str(subject_id), trigger=trigger, attempt=int(attempt)
            )
            held = self._held.pop(url, None)
            if held is not None:
                self._start_read(url, held)

    def _start_read(self, url: str, held: _HeldImage) -> None:
        if url in self._reading:
            return
        self._reading.add(url)
        self._channel._spawn(self._read(url, held))

    async def _read(self, url: str, held: _HeldImage) -> None:
        try:
            content = await held.response.body()
        except Exception:
            self.read_errors += 1
            return
        finally:
            self._reading.discard(url)
            self._channel._notify()
        expectation = self._expected.get(url)
        if self.closed or expectation is None or url in self._results:
            return
        status = int(held.response.status)
        digest = sha256(content).hexdigest()
        image = CapturedImage(
            url=url,
            seq=held.seq,
            page_role=held.page_role,
            status=status,
            content=content,
            sha256=digest,
            observed_at=held.observed_at,
        )
        visit_id = self._channel._write_visit(
            seq=held.seq,
            page_role=held.page_role,
            at=held.observed_at,
            stage=held.rule.stage,
            source="page_response",
            rule=held.rule.name,
            endpoint=held.rule.endpoint(url),
            status=status,
            outcome="ok" if status == 200 else "http_error",
            route=held.route,
            query_keys=list(held.query_keys),
            subject_id=expectation.subject_id,
            trigger=expectation.trigger,
            asset_key=expectation.asset_key,
            sha256=digest,
            bytes=len(content),
            attempt=expectation.attempt,
        )
        self._results[url] = (image, visit_id)
        self._channel._notify()

    def _complete(self) -> bool:
        return all(url in self._results for url in self._expected)

    async def collect(self, timeout: float) -> dict[str, tuple[CapturedImage, str] | None]:
        """等到全部期望 URL 截获或超时；未截获的 URL 映射为 None。"""
        await self._channel._wait_until(lambda: self._complete() or None, timeout)
        return {url: self._results.get(url) for url in self._expected}

    def close(self) -> None:
        """丢弃未认领的句柄；已截获的字节由调用方持有，不受影响。"""
        self.closed = True
        self._held.clear()
        self._channel._windows.discard(self)


class PageCaptureChannel:
    def __init__(
        self,
        context: Any,
        *,
        platform: str,
        rules: Iterable[CaptureRule],
        ledger: VisitLedger,
        monotonic: Callable[[], float] = time.monotonic,
        sentinel: SentinelRule | None = None,
        verify_headers: Iterable[str] = (),
        state_scripts: Mapping[str, str] | None = None,
        new_page_role: str = "new_tab",
        clock: Callable[[], str] = utc_now,
        max_retained_per_rule: int = MAX_RETAINED_PER_RULE,
        max_image_handles: int = IMAGE_WINDOW_MAX_HANDLES,
    ) -> None:
        self.context = context
        self.platform = platform
        self.rules = tuple(rules)
        names = [rule.name for rule in self.rules]
        if len(set(names)) != len(names):
            raise ValueError("capture rule names must be unique")
        self._rules_by_name = {rule.name: rule for rule in self.rules}
        self.ledger = ledger
        self._monotonic = monotonic
        self._clock = clock
        self.sentinel = sentinel
        self._verify_headers = frozenset(name.lower() for name in verify_headers)
        self._state_scripts = dict(state_scripts or {})
        self._new_page_role = new_page_role
        self._max_retained = max_retained_per_rule
        self._max_image_handles = max_image_handles
        self._pages: dict[int, tuple[Any, str]] = {}
        self._items: dict[str, list[CapturedResponse | CaptureFailure]] = {name: [] for name in names}
        self._pending: dict[str, set[int]] = {name: set() for name in names}
        self._recorded: set[int] = set()
        self._windows: set[ImageWindow] = set()
        self._sentinel_hits: list[SentinelHit] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._event = asyncio.Event()
        self._fault: BaseException | None = None
        self._installed = False
        self.closed = False
        self.dropped_responses = 0

    # 页面登记 -----------------------------------------------------------------

    def install(self, primary: Any) -> None:
        """登记主页面与现有页面，并让 Context 之后打开的页面自动登记。"""
        self.attach(primary, "primary")
        for page in list(getattr(self.context, "pages", ()) or ()):
            if page is not primary:
                self.attach(page, self._new_page_role)
        if not self._installed:
            self.context.on("page", self._on_context_page)
            self._installed = True

    def _on_context_page(self, page: Any) -> None:
        if id(page) not in self._pages:
            self.attach(page, self._new_page_role)

    def attach(self, page: Any, role: str) -> None:
        """登记页面；已登记的页面只更新角色（如新标签页确认为作者页）。"""
        known = self._pages.get(id(page))
        self._pages[id(page)] = (page, role)
        if known is None:
            page.on("response", partial(self._on_response, page))
            page.on("requestfailed", partial(self._on_request_failed, page))

    def role_of(self, page: Any) -> str | None:
        known = self._pages.get(id(page))
        return known[1] if known is not None and known[0] is page else None

    # 序号与账本 ---------------------------------------------------------------

    def next_seq(self) -> int:
        self._raise_if_faulted()
        return self.ledger.next_seq()

    def action(
        self,
        kind: str,
        *,
        page_role: str,
        target: Mapping[str, Any],
        from_visit: str | None = None,
        pointer: Mapping[str, Any] | None = None,
    ) -> str:
        """分配序号并写账本后返回动作 id；调用方随后才执行界面动作。`pointer` 为执行前计划好的指针记录
        （点击落点比例与接近时长，或各滚轮手势的事件数与位移），给出时写进同一行。"""
        seq = self.next_seq()
        fields: dict[str, Any] = {"pointer": dict(pointer)} if pointer is not None else {}
        return self.ledger.append(
            "action", seq=seq, page_role=page_role, action=kind, target=dict(target), from_visit=from_visit, **fields
        )

    def pause_started(self) -> int:
        return self.next_seq()

    def pause_finished(
        self,
        stage: str,
        start_seq: int,
        seconds: float,
        continuity: str = "n/a",
        *,
        page_role: str = "primary",
    ) -> str:
        seq = self.next_seq()
        return self.ledger.append(
            "pause",
            seq=seq,
            page_role=page_role,
            stage=stage,
            start_seq=int(start_seq),
            seconds=round(float(seconds), 3),
            continuity=continuity,
        )

    def decision(
        self,
        subject_id: str,
        decision: str,
        *,
        from_visit: str | None = None,
        page_role: str | None = None,
    ) -> str:
        seq = self.next_seq()
        return self.ledger.append(
            "decision", seq=seq, page_role=page_role, subject_id=str(subject_id), decision=decision,
            from_visit=from_visit,
        )

    def take_sentinel_hits(self) -> list[SentinelHit]:
        """取走待平台判定的哨兵事件（需检查验证界面的状态）。"""
        self._raise_if_faulted()
        hits, self._sentinel_hits = self._sentinel_hits, []
        return hits

    def signal(self, hit: SentinelHit, *, outcome: str) -> str:
        """非规则端点的异常状态只写诊断行；沿用事件到达时的序号。"""
        self._raise_if_faulted()
        return self.ledger.append(
            "signal",
            seq=hit.seq,
            page_role=hit.page_role,
            at=hit.observed_at,
            status=hit.status,
            endpoint=hit.endpoint,
            verify_headers_present=hit.verify_headers_present,
            outcome=outcome,
        )

    def record_visit(
        self,
        captured: CapturedResponse,
        *,
        stage: str,
        subject_id: str | None,
        trigger: str | None,
        outcome: str = "ok",
        **audit: Any,
    ) -> str:
        """为一条截获的 JSON 响应写访问行，沿用其到达序号；同一响应只能记录一次。"""
        self._raise_if_faulted()
        if not isinstance(captured, CapturedResponse):
            raise TypeError("record_visit requires a captured page response")
        return self._write_visit(
            seq=captured.seq,
            page_role=captured.page_role,
            at=captured.observed_at,
            stage=stage,
            source="page_response",
            rule=captured.rule.name,
            endpoint=captured.endpoint,
            status=captured.status,
            outcome=outcome,
            route=captured.route,
            query_keys=list(captured.query_keys),
            subject_id=subject_id,
            trigger=trigger,
            **audit,
        )

    def _write_visit(self, *, seq: int, page_role: str, at: str, **fields: Any) -> str:
        if seq in self._recorded:
            raise ValueError(f"captured response seq {seq} already has a visit")
        visit_id = self.ledger.append(
            "visit", seq=seq, page_role=page_role, at=at, recorded_at=self._clock(), **fields
        )
        self._recorded.add(seq)
        return visit_id

    async def read_state(
        self,
        page: Any,
        script_name: str,
        arg: Any = None,
        *,
        stage: str,
        subject_id: str | None,
        trigger: str | None,
        accept: Callable[[Any], bool] | None = None,
        timeout: float | None = None,
        **audit: Any,
    ) -> tuple[Any, str | None]:
        """执行登记的状态脚本读取白名单投影并写访问；`accept` 拒绝时只返回值、不写访问。"""
        self._raise_if_faulted()
        script = self._state_scripts.get(script_name)
        if script is None:
            raise ValueError(f"unknown state script: {script_name}")
        role = self.role_of(page)
        if role is None:
            raise ValueError("read_state requires a page attached to the capture channel")
        if timeout is None:
            value = await page.evaluate(script, arg)
        else:
            async with asyncio.timeout(timeout):
                value = await page.evaluate(script, arg)
        if accept is not None and not accept(value):
            return value, None
        seq = self.next_seq()
        route, query_keys = route_of(getattr(page, "url", ""))
        visit_id = self._write_visit(
            seq=seq,
            page_role=role,
            at=self._clock(),
            stage=stage,
            source="page_state",
            rule=script_name,
            endpoint=None,
            status=None,
            outcome="ok",
            route=route,
            query_keys=list(query_keys),
            subject_id=subject_id,
            trigger=trigger,
            **audit,
        )
        return value, visit_id

    # 等待截获 -----------------------------------------------------------------

    def _rule(self, name: str) -> CaptureRule:
        try:
            return self._rules_by_name[name]
        except KeyError:
            raise ValueError(f"unknown capture rule: {name}") from None

    def _settled(self, rule_name: str, after_seq: int) -> list[CapturedResponse | CaptureFailure]:
        """序号大于 after_seq、且之前没有仍在读体的截获，按到达序号排列。"""
        pending = sorted(seq for seq in self._pending[rule_name] if seq > after_seq)
        limit = pending[0] if pending else None
        return [
            item
            for item in self._items[rule_name]
            if item.seq > after_seq and (limit is None or item.seq < limit)
        ]

    async def wait_for(
        self,
        rule_name: str,
        *,
        after_seq: int,
        predicate: Callable[[CapturedResponse | CaptureFailure], bool] | None = None,
        timeout: float,
    ) -> CapturedResponse | CaptureFailure | None:
        """返回 after_seq 之后第一条（按到达序号）满足谓词的截获或请求失败；超时返回 None。"""
        self._rule(rule_name)

        def check() -> CapturedResponse | CaptureFailure | None:
            for item in self._settled(rule_name, after_seq):
                if predicate is None or predicate(item):
                    return item
            return None

        return await self._wait_until(check, timeout)

    def latest(
        self,
        rule_name: str,
        *,
        after_seq: int = 0,
        predicate: Callable[[CapturedResponse], bool] | None = None,
    ) -> CapturedResponse | None:
        self._raise_if_faulted()
        self._rule(rule_name)
        for item in reversed(self._items[rule_name]):
            if item.seq <= after_seq:
                break
            if isinstance(item, CapturedResponse) and (predicate is None or predicate(item)):
                return item
        return None

    def latest_pending_seq(self, rule_name: str) -> int:
        """仍在读体的截获中最大的到达序号；没有在途截获时为 0。"""
        self._rule(rule_name)
        return max(self._pending[rule_name], default=0)

    def drain(self, rule_name: str, *, after_seq: int) -> list[CapturedResponse | CaptureFailure]:
        """after_seq 之后已结算的全部截获与失败，按到达序号排列（供逐页扫描）。"""
        self._raise_if_faulted()
        self._rule(rule_name)
        return self._settled(rule_name, after_seq)

    async def _wait_until(self, check: Callable[[], Any], timeout: float) -> Any:
        deadline = self._monotonic() + max(0.0, float(timeout))
        while True:
            self._raise_if_faulted()
            event = self._event
            result = check()
            if result is not None:
                return result
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except TimeoutError:
                pass

    def open_image_window(self, page_role: str) -> ImageWindow:
        self._raise_if_faulted()
        window = ImageWindow(self, page_role, self._max_image_handles)
        self._windows.add(window)
        return window

    # 事件回调（同步；只在这里分配截获序号） ------------------------------------

    def _on_response(self, page: Any, response: Any) -> None:
        if self._fault is not None or self.closed:
            return
        try:
            self._handle_response(page, response)
        except Exception as exc:  # 回调不得把异常抛回 Playwright 的事件分发
            self._fault = exc
            self._notify()

    def _origin(self, page: Any, request: Any) -> tuple[Any, str] | None:
        try:
            origin = request.frame.page
        except Exception:
            return None  # service worker 等没有 frame 的请求
        role = self.role_of(origin)
        if role is None or origin is not page:
            return None
        return origin, role

    def _handle_response(self, page: Any, response: Any) -> None:
        request = response.request
        origin = self._origin(page, request)
        if origin is None:
            self.dropped_responses += 1
            return
        origin_page, role = origin
        method = str(request.method or "")
        resource_type = str(request.resource_type or "")
        url = str(response.url or "")
        status = int(response.status)
        rule = next((item for item in self.rules if item.matches(method, url, resource_type)), None)
        if rule is None:
            if self.sentinel is not None and self.sentinel.matches(url, resource_type, status):
                self._handle_sentinel(origin_page, role, response, url, status)
            return
        if rule.body == "none":
            return
        if rule.body == "bytes" and not any(
            not window.closed and window.page_role == role for window in self._windows
        ):
            return  # 窗口之外的图片从不读体，也不占用序号
        seq = self.ledger.next_seq()
        observed_at = self._clock()
        route, query_keys = route_of(getattr(origin_page, "url", ""))
        if rule.body == "bytes":
            held = _HeldImage(
                rule=rule,
                seq=seq,
                page_role=role,
                route=route,
                query_keys=query_keys,
                observed_at=observed_at,
                response=response,
            )
            for window in list(self._windows):
                window._offer(held, url)
            return
        self._pending[rule.name].add(seq)
        self._spawn(
            self._read_json(
                rule,
                seq=seq,
                role=role,
                route=route,
                query_keys=query_keys,
                endpoint=rule.endpoint(url),
                status=status,
                verify=self._verify_present(response),
                request_json=_request_json(request),
                observed_at=observed_at,
                response=response,
            )
        )

    def _handle_sentinel(self, page: Any, role: str, response: Any, url: str, status: int) -> None:
        assert self.sentinel is not None
        seq = self.ledger.next_seq()
        hit = SentinelHit(
            seq=seq,
            page_role=role,
            status=status,
            endpoint=f"{response.request.method} {(urlsplit(url).hostname or '').lower()} {route_of(url)[0]}",
            verify_headers_present=self._verify_present(response),
            observed_at=self._clock(),
        )
        if status in self.sentinel.surface_check_statuses:
            self._sentinel_hits.append(hit)
            self._notify()
        else:
            self.signal(hit, outcome="ignored")

    def _verify_present(self, response: Any) -> bool:
        if not self._verify_headers:
            return False
        try:
            headers = response.headers or {}
        except Exception:
            return False
        return any(str(name).lower() in self._verify_headers for name in headers)

    async def _read_json(
        self,
        rule: CaptureRule,
        *,
        seq: int,
        role: str,
        route: str,
        query_keys: tuple[str, ...],
        endpoint: str,
        status: int,
        verify: bool,
        request_json: Any,
        observed_at: str,
        response: Any,
    ) -> None:
        payload: Any = None
        body_error = ""
        try:
            payload = json.loads(await response.body())
        except Exception as exc:
            body_error = type(exc).__name__
        captured = CapturedResponse(
            rule=rule,
            seq=seq,
            page_role=role,
            route=route,
            query_keys=query_keys,
            endpoint=endpoint,
            status=status,
            verify_headers_present=verify,
            request_json=request_json,
            json=payload,
            observed_at=observed_at,
            body_error=body_error,
        )
        self._store(rule.name, captured)

    def _on_request_failed(self, page: Any, request: Any) -> None:
        if self._fault is not None or self.closed:
            return
        try:
            origin = self._origin(page, request)
            if origin is None:
                return
            origin_page, role = origin
            url = str(request.url or "")
            rule = next(
                (
                    item
                    for item in self.rules
                    if item.body == "json" and item.matches(str(request.method or ""), url, str(request.resource_type or ""))
                ),
                None,
            )
            if rule is None:
                return
            seq = self.ledger.next_seq()
            route, query_keys = route_of(getattr(origin_page, "url", ""))
            failure = CaptureFailure(
                rule=rule,
                seq=seq,
                page_role=role,
                route=route,
                query_keys=query_keys,
                endpoint=rule.endpoint(url),
                failure_text=str(request.failure or ""),
                request_json=_request_json(request),
                observed_at=self._clock(),
            )
            self._store(rule.name, failure)
        except Exception as exc:
            self._fault = exc
            self._notify()

    def _store(self, rule_name: str, item: CapturedResponse | CaptureFailure) -> None:
        items = self._items[rule_name]
        index = len(items)
        while index and items[index - 1].seq > item.seq:
            index -= 1
        items.insert(index, item)
        if len(items) > self._max_retained:
            del items[: len(items) - self._max_retained]
        self._pending[rule_name].discard(item.seq)
        self._notify()

    # 后台任务与收尾 -----------------------------------------------------------

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None and self._fault is None:
            self._fault = task.exception()
            self._notify()

    def _notify(self) -> None:
        event, self._event = self._event, asyncio.Event()
        event.set()

    def _raise_if_faulted(self) -> None:
        if self._fault is not None:
            raise CaptureChannelError(
                f"page capture channel failed: {type(self._fault).__name__}"
            ) from self._fault

    def close(self) -> None:
        """停止截获：关闭窗口、取消未完成的读体并关闭账本；之后到达的事件一律忽略。"""
        self.closed = True
        for window in list(self._windows):
            window.close()
        for task in list(self._tasks):
            task.cancel()
        self.ledger.close()

    async def aclose(self) -> None:
        tasks = list(self._tasks)
        self.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
