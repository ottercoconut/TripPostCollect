"""小红书搜索流程的离线站点：首页输入、回车、筛选面板点选“图文”、虚拟列表滚动与逐页搜索响应。

只模拟扫描用到的 DOM 读数（按 `page_scripts.DOM_SCRIPTS` 的脚本文本分派）与鼠标、键盘动作；搜索响应经
`page_events` 的假请求/响应发给页面取数通道。不启动浏览器、不联网。
"""

from __future__ import annotations

import json
from typing import Any, Iterable
from urllib.parse import quote

from support.page_events import FakeContext, FakePage
from trippostcollect.platforms.xhs.page_scripts import DOM_SCRIPTS

SEARCH_URL = "https://so.xiaohongshu.com/api/sns/web/v2/search/notes"
TARGET_FILTERS = [
    {"type": "sort_type", "tags": ["general"]},
    {"type": "filter_note_type", "tags": ["普通笔记"]},
    {"type": "filter_note_time", "tags": ["不限"]},
    {"type": "filter_note_range", "tags": ["不限"]},
    {"type": "filter_pos_distance", "tags": ["不限"]},
]
SCRIPT_NAMES = {script: name for name, script in DOM_SCRIPTS.items()}

VIEW_WIDTH = 1280
VIEW_HEIGHT = 900
LIST_TOP = 120
ROW_HEIGHT = 360
COLUMNS = 4
FOOTER = 200
PREFETCH_PX = 400
SEARCH_BOX = {"x": 640.0, "y": 40.0}
FILTER_ENTRY = {"x": 1200.0, "y": 100.0}
FILTER_OPTION = {"x": 700.0, "y": 200.0}


def note(note_id: str, *, card_type: str = "normal") -> dict[str, Any]:
    return {
        "id": note_id,
        "model_type": "note",
        "xsec_token": f"token-{note_id}",
        "note_card": {"type": card_type},
    }


class _Mouse:
    def __init__(self, site: "XhsFakeSite") -> None:
        self.site = site

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        self.site.mouse_position = (x, y)

    async def click(self, x: float, y: float, delay: int = 0) -> None:
        self.site.click(x, y)

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        self.site.wheel(delta_y)


class _Keyboard:
    def __init__(self, site: "XhsFakeSite") -> None:
        self.site = site

    async def type(self, text: str) -> None:
        if self.site.box_focused:
            self.site.box_value += text

    async def press(self, key: str) -> None:
        if key == "Enter" and self.site.box_focused:
            self.site.press_enter()


class XhsFakeSite:
    """目标会话的页由 `pages` 给出：每页一个笔记项列表；最后一页之后自动追加 has_more=false 的空页（R4）。

    选项：`apply_filter_works=False` 点选“图文”不产生目标会话；`panel_closes=False` 面板收不起；
    `typed_keyword` 让回车请求体里的关键词与目标不同；`stall_after` 页之后前端不再加载；
    `hot_query` 每页附一条热搜项（无 DOM 卡片）。`search_overrides[k]` 依次替换目标会话第 k 页的响应：
    字符串为请求失败文本（requestfailed），字典为响应 JSON；被替换的页不算已加载，前端下次滚动会重发。
    """

    def __init__(
        self,
        keyword: str,
        pages: list[list[dict[str, Any]]],
        *,
        apply_filter_works: bool = True,
        panel_closes: bool = True,
        typed_keyword: str | None = None,
        stall_after: int | None = None,
        hot_query: bool = True,
        unregistered_ids: Iterable[str] = (),
    ) -> None:
        self.keyword = keyword
        self.target_pages = [list(items) for items in pages] + [[]]
        self.apply_filter_works = apply_filter_works
        self.panel_closes = panel_closes
        self.typed_keyword = typed_keyword
        self.stall_after = stall_after
        self.hot_query = hot_query
        self.unregistered_ids = list(unregistered_ids)
        self.context = FakeContext()
        self.page = self.context.new_page("https://www.xiaohongshu.com/explore")
        self.page.mouse = _Mouse(self)
        self.page.keyboard = _Keyboard(self)
        self.page.evaluate = self.evaluate  # type: ignore[method-assign]
        self.page.goto = self.goto  # type: ignore[attr-defined]
        self.page.is_closed = lambda: False  # type: ignore[attr-defined]
        self.page.bring_to_front = self._noop  # type: ignore[attr-defined]
        self.mouse_position = (0.0, 0.0)
        self.box_focused = False
        self.box_value = ""
        self.scroll_y = 0
        self.panel_visible = False
        self.filter_applied = False
        self.session_counter = 0
        self.search_id = ""
        self.session_filters: list[dict[str, Any]] | None = None
        self.session_keyword = keyword
        self.loaded: list[list[dict[str, Any]]] = []
        self.requests: list[dict[str, Any]] = []
        self.search_overrides: dict[int, list[Any]] = {}
        self.before_respond: Any = None
        self.enter_items = [note(f"general-{index}") for index in range(4)]

    @staticmethod
    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    # 页面状态 ------------------------------------------------------------------

    @property
    def on_results(self) -> bool:
        return "/search_result_ai" in self.page.url

    def rendered_items(self) -> list[dict[str, Any]]:
        if not self.on_results:
            return []
        if self.session_filters is None:
            return list(self.enter_items)
        rendered = [item for items in self.loaded for item in items]
        return [{"id": note_id} for note_id in self.unregistered_ids] + rendered

    def scroll_height(self) -> int:
        rows = -(-len(self.rendered_items()) // COLUMNS)
        return LIST_TOP + rows * ROW_HEIGHT + FOOTER

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.page.url = url
        self.scroll_y = 0
        self.panel_visible = False
        self.box_value = ""
        self.box_focused = False
        return None

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        name = SCRIPT_NAMES.get(script)
        if name == "render_diagnostic":
            return {"ready_state": "complete", "body_text_length": 100, "body_child_element_count": 3}
        if name == "search_box":
            visible = not self.on_results
            return {"present": visible, "visible": visible, "value_length": len(self.box_value), **SEARCH_BOX}
        if name == "search_layout":
            return {"visible": self.on_results}
        if name == "scroll_metrics":
            return {
                "scroll_y": self.scroll_y,
                "inner_height": VIEW_HEIGHT,
                "inner_width": VIEW_WIDTH,
                "scroll_height": self.scroll_height(),
            }
        if name == "filter_state":
            return {
                "entry_text": "已筛选" if self.filter_applied else "筛选",
                "entry": dict(FILTER_ENTRY) if self.on_results else None,
                "panel_visible": self.panel_visible,
                "option": dict(FILTER_OPTION) if self.panel_visible and arg == "图文" else None,
            }
        if name == "visible_cards":
            return self.visible_cards()
        if name == "search_ready_counts":
            return {"card_count": len(self.rendered_items()), "profile_count": 1}
        raise AssertionError(f"unexpected page script: {name or script[:40]}")

    def visible_cards(self) -> list[dict[str, Any]]:
        cards = []
        for index, item in enumerate(self.rendered_items()):
            if item.get("model_type") not in (None, "note"):
                continue
            top = LIST_TOP + (index // COLUMNS) * ROW_HEIGHT - self.scroll_y
            bottom = top + ROW_HEIGHT - 20
            visible = max(0, min(bottom, VIEW_HEIGHT) - max(top, 0))
            if visible <= 0:
                continue
            cards.append(
                {
                    "note_id": item["id"],
                    "data_index": index,
                    "visible_ratio": visible / (bottom - top),
                    "top": top,
                    "bottom": bottom,
                    "cover": {"x": 160.0 + 300 * (index % COLUMNS), "y": top + 130.0, "width": 240, "height": 240},
                }
            )
        return cards

    # 动作 ----------------------------------------------------------------------

    def click(self, x: float, y: float) -> None:
        def near(point: dict[str, float]) -> bool:
            return abs(point["x"] - x) < 20 and abs(point["y"] - y) < 20

        if not self.on_results and near(SEARCH_BOX):
            self.box_focused = True
        elif self.on_results and near(FILTER_ENTRY):
            if self.panel_visible and not self.panel_closes:
                return
            self.panel_visible = not self.panel_visible
        elif self.on_results and self.panel_visible and near(FILTER_OPTION):
            if self.apply_filter_works:
                self.filter_applied = True
                self.start_session(filters=TARGET_FILTERS)

    def press_enter(self) -> None:
        keyword = self.typed_keyword if self.typed_keyword is not None else self.box_value
        self.page.url = f"https://www.xiaohongshu.com/search_result_ai?keyword={quote(quote(keyword))}&source=web"
        self.box_focused = False
        self.filter_applied = False
        self.start_session(filters=None, keyword=keyword)

    def start_session(self, *, filters: list[dict[str, Any]] | None, keyword: str | None = None) -> None:
        self.session_counter += 1
        self.search_id = f"2f{self.session_counter:04d}"
        self.session_filters = filters
        self.session_keyword = keyword if keyword is not None else self.session_keyword
        self.scroll_y = 0
        self.loaded = []
        self.respond_page(1)

    def wheel(self, delta_y: float) -> None:
        maximum = max(0, self.scroll_height() - VIEW_HEIGHT)
        self.scroll_y = int(min(max(0, self.scroll_y + delta_y), maximum))
        self.maybe_load_next()

    def maybe_load_next(self) -> None:
        if self.session_filters is None or not self.loaded:
            return
        next_page = len(self.loaded) + 1
        if next_page > len(self.target_pages):
            return
        if self.stall_after is not None and next_page > self.stall_after:
            return
        if self.scroll_y + VIEW_HEIGHT >= self.scroll_height() - PREFETCH_PX:
            self.respond_page(next_page)

    def respond_page(self, number: int, *, status: int = 200, failure: str | None = None) -> None:
        request_json: dict[str, Any] = {
            "keyword": self.session_keyword,
            "page": number,
            "page_size": 20,
            "search_id": self.search_id,
            "sort": "general",
            "note_type": 0,
            "ext_flags": [],
            "geo": "",
            "image_formats": ["jpg", "webp", "avif"],
        }
        if self.session_filters is not None:
            request_json["filters"] = self.session_filters
            items = list(self.target_pages[number - 1])
            has_more = number < len(self.target_pages)
        else:
            items = list(self.enter_items)
            has_more = True
        self.requests.append(request_json)
        request = self.page.request("POST", SEARCH_URL, resource_type="fetch", post_data_json=request_json)
        if self.before_respond is not None:
            self.before_respond(number, request_json)
        override = None
        if self.session_filters is not None and self.search_overrides.get(number):
            override = self.search_overrides[number].pop(0)
        if isinstance(override, str):
            failure = override
        if failure is not None:
            self.page.fail(request, failure)
            return
        if isinstance(override, dict):
            self.page.respond(request, status=status, body=json.dumps(override).encode())
            return
        if self.session_filters is not None:
            self.loaded.append(items)
        body_items = items + ([{"id": f"hot-{number}", "model_type": "hot_query"}] if self.hot_query else [])
        payload = {"code": 0, "success": True, "data": {"has_more": has_more, "items": body_items}}
        self.page.respond(request, status=status, body=json.dumps(payload, ensure_ascii=False).encode())

    def restart_without_filter(self) -> None:
        """前端在验证或登录恢复后自行重搜且丢了筛选：新会话、无 filters、列表回顶。"""
        self.filter_applied = False
        self.start_session(filters=None)


def make_site(*args: Any, **kwargs: Any) -> XhsFakeSite:
    return XhsFakeSite(*args, **kwargs)


__all__ = ["XhsFakeSite", "FakePage", "make_site", "note", "TARGET_FILTERS"]
