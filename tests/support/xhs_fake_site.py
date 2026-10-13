"""小红书搜索流程的离线站点：首页输入、回车、筛选面板点选“图文”、虚拟列表滚动与逐页搜索响应；
点封面打开笔记浮层（feed 与图片响应、翻图、关闭按钮），点作者名打开作者标签页（页面状态投影）。
repair 直开 `/explore/<id>` 时为整页详情：没有 feed，页面状态 note_detail_state 按脚本参数的白名单投影。

只模拟用到的 DOM 读数（按 `page_scripts.DOM_SCRIPTS` / `STATE_SCRIPTS` 的脚本文本分派）与鼠标、键盘动作；
响应经 `page_events` 的假请求/响应发给页面取数通道。不启动浏览器、不联网。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

from support.page_events import FakeContext, FakePage
from trippostcollect.platforms.xhs.page_scripts import DOM_SCRIPTS, STATE_SCRIPTS

SEARCH_URL = "https://so.xiaohongshu.com/api/sns/web/v2/search/notes"
TARGET_FILTERS = [
    {"type": "sort_type", "tags": ["general"]},
    {"type": "filter_note_type", "tags": ["普通笔记"]},
    {"type": "filter_note_time", "tags": ["不限"]},
    {"type": "filter_note_range", "tags": ["不限"]},
    {"type": "filter_pos_distance", "tags": ["不限"]},
]
FEED_URL = "https://edith.xiaohongshu.com/api/sns/web/v1/feed"
SCRIPT_NAMES = {script: name for name, script in DOM_SCRIPTS.items()}
STATE_NAMES = {script: name for name, script in STATE_SCRIPTS.items()}

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
CLOSE_POINT = {"x": 1250.0, "y": 50.0}
ARROW_POINT = {"x": 760.0, "y": 450.0}
AUTHOR_POINT = {"x": 980.0, "y": 130.0}


def image_url(note_id: str, index: int) -> str:
    return f"https://sns-webpic-qc.xhscdn.com/202601/{note_id}/notes_pre_post/{note_id}-{index}!nd_dft_wlteh_webp_3"


def image_bytes(note_id: str, index: int) -> bytes:
    return f"image-{note_id}-{index}".encode()


def detail_card(note_id: str, *, images: int = 2, user_id: str | None = None, note_type: str = "normal") -> dict[str, Any]:
    """feed 的 note_card：字段齐全的有效图文（头像只在内存中，写出时由清理器去除）。"""
    return {
        "note_id": note_id,
        "type": note_type,
        "title": f"note {note_id}",
        "desc": "青岛 正文",
        "time": 1_700_000_000_000,
        "user": {"user_id": user_id or f"author-{note_id}", "nickname": "作者", "avatar": "https://sns-avatar-qc.xhscdn.com/a.jpg"},
        "interact_info": {"liked_count": "1", "collected_count": "2", "comment_count": "3", "share_count": "4"},
        "image_list": [{"url_default": image_url(note_id, index)} for index in range(images)],
        "tag_list": [],
    }


def state_note(card: dict[str, Any]) -> dict[str, Any]:
    """feed note_card 对应的页面状态 note（camelCase）；带上投影白名单之外的字段（头像、token、互动状态）。"""
    user = card.get("user") or {}
    interact = card.get("interact_info") or {}
    return {
        "noteId": card.get("note_id"),
        "type": card.get("type"),
        "title": card.get("title"),
        "desc": card.get("desc"),
        "time": card.get("time"),
        "lastUpdateTime": card.get("last_update_time", card.get("time")),
        "ipLocation": "山东",
        "xsecToken": "state-token",
        "user": {
            "userId": user.get("user_id"), "nickname": user.get("nickname"),
            "avatar": user.get("avatar"), "xsecToken": "user-token",
        },
        "interactInfo": {
            "likedCount": interact.get("liked_count"), "collectedCount": interact.get("collected_count"),
            "commentCount": interact.get("comment_count"), "shareCount": interact.get("share_count"),
            "liked": False, "collected": False, "followed": False, "relation": "none",
        },
        "imageList": [
            {"urlDefault": image.get("url_default"), "width": 1, "height": 1, "livePhoto": False,
             "traceId": "trace", "stream": {}}
            for image in card.get("image_list") or []
        ],
        "tagList": list(card.get("tag_list") or []),
    }


def whitelisted_state(note: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """按 note_detail_state 的脚本参数只复制白名单标量，等价于页面内脚本的投影。"""
    def scalars(source: Any, fields: list[str]) -> dict[str, Any]:
        source = source if isinstance(source, dict) else {}
        return {key: source[key] for key in fields if isinstance(source.get(key), (str, int, float, bool))}

    projected = scalars(note, spec["top_fields"])
    for key, fields in spec["object_fields"]:
        projected[key] = scalars(note.get(key), fields)
    for key, fields in spec["list_fields"]:
        projected[key] = [scalars(item, fields) for item in (note.get(key) or [])[: spec["max_items"]]]
    return projected


def creator_projection(*, fans: str = "128") -> dict[str, Any]:
    return {
        "status": "ok",
        "creator": {
            "basicInfo": {"nickname": "作者", "desc": "简介", "ipLocation": "山东"},
            "interactions": [
                {"type": "follows", "name": "关注", "count": "5"},
                {"type": "fans", "name": "粉丝", "count": fans},
                {"type": "interaction", "name": "获赞与收藏", "count": "99"},
            ],
        },
    }


class AuthorTab(FakePage):
    """作者标签页：地址路径为 /user/profile/<id>，页面状态投影由站点给出。"""

    viewport_size = {"width": 1280, "height": 800}

    def __init__(self, site: "XhsFakeSite", user_id: str, url_user_id: str) -> None:
        super().__init__(f"https://www.xiaohongshu.com/user/profile/{url_user_id}?xsec_source=pc_note")
        self.site = site
        self.user_id = user_id
        self.closed = False
        self.mouse = _TabMouse()
        self.projection_calls = 0

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if STATE_NAMES.get(script) == "creator_page_data":
            self.projection_calls += 1
            hook = self.site.projection_hooks.get(self.user_id)
            if hook is not None:
                hook()
            value = self.site.creator_projections.get(self.user_id, creator_projection())
            if isinstance(value, BaseException):
                raise value
            return value
        if SCRIPT_NAMES.get(script) == "render_diagnostic":
            return {"ready_state": "complete", "body_text_length": 100, "body_child_element_count": 3}
        raise AssertionError(f"unexpected author tab script: {script[:40]}")

    async def wait_for_timeout(self, milliseconds: float) -> None:
        return None

    async def wait_for_load_state(self, state: str, timeout: float | None = None) -> None:
        return None

    async def bring_to_front(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


class _TabMouse:
    async def move(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def wheel(self, *args: Any, **kwargs: Any) -> None:
        return None


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
        self.site.wheel_points.append(self.site.mouse_position)
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
        elif key == "ArrowRight" and self.site.overlay_note is not None:
            self.site.flip()


class XhsFakeSite:
    """目标会话的页由 `pages` 给出：每页一个笔记项列表；最后一页之后自动追加 has_more=false 的空页（R4）。

    选项：`apply_filter_works=False` 点选“图文”不产生目标会话；`panel_closes=False` 面板收不起；
    `entry_delay_reads` 为结果页上前几次读筛选状态时入口尚未渲染（None 为始终不出现）；
    `option_missing_opens` 为前几次打开面板时没有“图文”选项；`list_rect` 为滚动度量里结果列表与视口的
    相交矩形（None 时不给，扫描按视口比例落点），`wheel_points` 记下每次滚轮时的鼠标位置；
    `typed_keyword` 让回车请求体里的关键词与目标不同；`stall_after` 页之后前端不再加载；
    `hot_query` 每页附一条热搜项（无 DOM 卡片）。`search_overrides[k]` 依次替换目标会话第 k 页的响应：
    字符串为请求失败文本（requestfailed），字典为响应 JSON；被替换的页不算已加载，前端下次滚动会重发。

    浮层：`details[id]` 为 feed 的 note_card（默认 `detail_card(id)`）。`feed_overrides[id]` 依次替换每次点击：
    "no_overlay" 浮层不出现；"no_feed" 浮层出现但无 feed；"net::…" 为 feed 请求失败；整数为 HTTP 状态码；
    字典为 feed 响应 JSON。`image_overrides[url]` 为 "missing"（从不加载）或 HTTP 状态码。打开浮层加载前两张图，
    翻到第 k 页再加载第 k+1 张。`author_tab_failures` 次点击作者名不开标签页；`author_url_ids[user]` 改写标签页
    地址里的作者 ID；`creator_projections[user]` 替换投影返回值，`projection_hooks[user]` 在每次读取投影时调用。
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
        self.entry_delay_reads: int | None = 0
        self.filter_reads = 0
        self.option_missing_opens = 0
        self.panel_opens = 0
        self.diagnostic_reads = 0
        self.list_rect: dict[str, float] | None = None
        self.wheel_points: list[tuple[float, float]] = []
        self.session_counter = 0
        self.search_id = ""
        self.session_filters: list[dict[str, Any]] | None = None
        self.session_keyword = keyword
        self.loaded: list[list[dict[str, Any]]] = []
        self.requests: list[dict[str, Any]] = []
        self.search_overrides: dict[int, list[Any]] = {}
        self.before_respond: Any = None
        self.enter_items = [note(f"general-{index}") for index in range(4)]
        self.details: dict[str, dict[str, Any]] = {}
        self.feed_overrides: dict[str, list[Any]] = {}
        self.image_overrides: dict[str, Any] = {}
        self.author_tab_failures = 0
        self.author_url_ids: dict[str, str] = {}
        self.creator_projections: dict[str, Any] = {}
        self.projection_hooks: dict[str, Any] = {}
        self.overlay_closes = True
        self.overlay_note: str | None = None
        self.overlay_page = 0
        self.results_url = ""
        self.feed_requests: list[str] = []
        self.author_tabs: list[AuthorTab] = []
        self.loaded_images: set[str] = set()
        # repair 整页：失效链接跳 /404；note_states[id] 为列表时依次返回（用完后照常投影），否则每次都返回该值。
        self.unavailable_notes: set[str] = set()
        self.note_states: dict[str, Any] = {}
        self.note_page_opens: list[str] = []
        self.author_same_page = False
        self.same_page_author: str | None = None
        self.page_history: list[str] = []
        self.page.go_back = self.go_back  # type: ignore[attr-defined]
        # 作者页同页跳转时主页面也要做到达检查与小幅滚动。
        self.page.viewport_size = {"width": VIEW_WIDTH, "height": VIEW_HEIGHT}  # type: ignore[attr-defined]
        self.page.wait_for_timeout = self._noop  # type: ignore[attr-defined]
        self.page.wait_for_load_state = self._noop  # type: ignore[attr-defined]

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
        self.overlay_note = None
        self.same_page_author = None
        match = re.match(r"^/explore/([^/]+)$", urlsplit(url).path)
        if match:
            self.open_note_page(match.group(1))
        return None

    async def go_back(self, **kwargs: Any) -> None:
        """回到笔记整页：同一文档的图片走缓存，不再加载。"""
        self.same_page_author = None
        self.page.url = self.page_history.pop()
        match = re.match(r"^/explore/([^/]+)$", urlsplit(self.page.url).path)
        self.overlay_note = match.group(1) if match else None

    def open_note_page(self, note_id: str) -> None:
        """repair 直开保存的链接：整页详情，首图与相邻图加载（同一文档内已加载的图走缓存）；没有 feed。"""
        self.note_page_opens.append(note_id)
        if note_id in self.unavailable_notes:
            self.page.url = "https://www.xiaohongshu.com/404?source=note"
            return
        self.overlay_note = note_id
        self.overlay_page = 1
        self.load_image(note_id, 0)
        self.load_image(note_id, 1)

    def note_state(self, spec: dict[str, Any]) -> Any:
        note_id = spec["note_id"]
        queued = self.note_states.get(note_id)
        if queued is not None and not isinstance(queued, list):
            return queued
        if queued:
            value = queued.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        if self.overlay_note != note_id:
            return {"status": "missing"}
        return {"status": "ok", "note": whitelisted_state(state_note(self.detail(note_id)), spec)}

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        state_name = STATE_NAMES.get(script)
        if state_name == "note_detail_state":
            return self.note_state(arg)
        if state_name == "creator_page_data" and self.same_page_author is not None:
            return self.creator_projections.get(self.same_page_author, creator_projection())
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
                "list_rect": dict(self.list_rect) if self.list_rect else None,
            }
        if name == "filter_state":
            return self.filter_state(arg)
        if name == "filter_diagnostic":
            return self.filter_diagnostic()
        if name == "visible_cards":
            return self.visible_cards()
        if name == "search_ready_counts":
            return {"card_count": len(self.rendered_items()), "profile_count": 1}
        if name == "note_view_state":
            return self.overlay_state()
        raise AssertionError(f"unexpected page script: {name or script[:40]}")

    @property
    def entry_rendered(self) -> bool:
        return self.on_results and self.entry_delay_reads is not None and self.filter_reads > self.entry_delay_reads

    def filter_state(self, label: str) -> dict[str, Any]:
        if self.on_results:
            self.filter_reads += 1
        rendered = self.entry_rendered
        has_option = self.panel_visible and label == "图文" and self.panel_opens > self.option_missing_opens
        return {
            "entry_text": ("已筛选" if self.filter_applied else "筛选") if rendered else "",
            "entry": dict(FILTER_ENTRY) if rendered else None,
            "panel_visible": self.panel_visible,
            "option": dict(FILTER_OPTION) if has_option else None,
        }

    def filter_diagnostic(self) -> dict[str, Any]:
        self.diagnostic_reads += 1
        rendered = self.entry_rendered
        return {
            "layout_count": int(self.on_results),
            "entry_candidates": int(rendered),
            "entry_visible": rendered,
            "panel_present": self.on_results,
            "label_nodes": int(rendered),
            "label_chains": [["span", "div.filter", "div.<id>", "div.search-layout"]] if rendered else [],
            "inner_width": VIEW_WIDTH,
            "inner_height": VIEW_HEIGHT,
            "scroll_y": self.scroll_y,
        }

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

        if self.overlay_note is not None:
            if near(CLOSE_POINT) and self.overlay_closes:
                self.close_overlay()
            elif near(ARROW_POINT):
                self.flip()
            elif near(AUTHOR_POINT):
                self.open_author_tab()
            return
        card = next((card for card in self.visible_cards() if card["cover"] and near(card["cover"])), None)
        if self.on_results and card is not None:
            self.open_overlay(card["note_id"])
        elif not self.on_results and near(SEARCH_BOX):
            self.box_focused = True
        elif self.entry_rendered and near(FILTER_ENTRY):
            if self.panel_visible and not self.panel_closes:
                return
            self.panel_visible = not self.panel_visible
            self.panel_opens += int(self.panel_visible)
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

    # 浮层 --------------------------------------------------------------------

    def detail(self, note_id: str) -> dict[str, Any]:
        return self.details.setdefault(note_id, detail_card(note_id))

    def overlay_state(self) -> dict[str, Any]:
        if self.overlay_note is None:
            return {"visible": False}
        total = len(self.detail(self.overlay_note).get("image_list") or [])
        return {
            "visible": True,
            "content_visible": True,
            "page_index": self.overlay_page if total else None,
            "page_total": total or None,
            "close": dict(CLOSE_POINT),
            "right_arrow": dict(ARROW_POINT) if total > 1 else None,
            "author": dict(AUTHOR_POINT),
        }

    def open_overlay(self, note_id: str) -> None:
        override = self.feed_overrides[note_id].pop(0) if self.feed_overrides.get(note_id) else None
        if override == "no_overlay":
            return
        self.results_url = self.page.url
        self.overlay_note = note_id
        self.overlay_page = 1
        self.page.url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=token-{note_id}&xsec_source=pc_search"
        if override != "no_feed":
            self.respond_feed(note_id, override)
        self.load_image(note_id, 0)
        self.load_image(note_id, 1)

    def respond_feed(self, note_id: str, override: Any = None) -> None:
        self.feed_requests.append(note_id)
        request_json = {"source_note_id": note_id, "image_formats": ["jpg", "webp", "avif"], "xsec_source": "pc_search"}
        request = self.page.request("POST", FEED_URL, resource_type="fetch", post_data_json=request_json)
        if isinstance(override, str):
            self.page.fail(request, override)
            return
        if isinstance(override, int):
            self.page.respond(request, status=override, body=b"{}")
            return
        payload = override if isinstance(override, dict) else {
            "code": 0, "success": True, "data": {"items": [{"id": note_id, "model_type": "note", "note_card": self.detail(note_id)}]},
        }
        self.page.respond(request, status=200, body=json.dumps(payload, ensure_ascii=False).encode())

    def load_image(self, note_id: str, index: int) -> None:
        images = self.detail(note_id).get("image_list") or []
        if index >= len(images):
            return
        url = images[index]["url_default"]
        override = self.image_overrides.get(url)
        if override == "missing" or url in self.loaded_images:
            return
        self.loaded_images.add(url)
        request = self.page.request("GET", url, resource_type="image")
        status = override if isinstance(override, int) else 200
        self.page.respond(request, status=status, body=image_bytes(note_id, index) if status == 200 else b"")

    def flip(self) -> None:
        total = len(self.detail(self.overlay_note).get("image_list") or [])
        if self.overlay_page < total:
            self.overlay_page += 1
            self.load_image(self.overlay_note, self.overlay_page)

    def close_overlay(self) -> None:
        self.overlay_note = None
        self.page.url = self.results_url

    def open_author_tab(self) -> None:
        if self.author_tab_failures > 0:
            self.author_tab_failures -= 1
            return
        user_id = str(self.detail(self.overlay_note)["user"]["user_id"])
        if self.author_same_page:
            self.page_history.append(self.page.url)
            self.overlay_note = None
            self.same_page_author = user_id
            self.page.url = f"https://www.xiaohongshu.com/user/profile/{self.author_url_ids.get(user_id, user_id)}"
            return
        tab = AuthorTab(self, user_id, self.author_url_ids.get(user_id, user_id))
        self.author_tabs.append(tab)
        self.context.new_page(page=tab)

    def restart_without_filter(self) -> None:
        """前端在验证或登录恢复后自行重搜且丢了筛选：新会话、无 filters、列表回顶。"""
        self.filter_applied = False
        self.start_session(filters=None)


def make_site(*args: Any, **kwargs: Any) -> XhsFakeSite:
    return XhsFakeSite(*args, **kwargs)


__all__ = [
    "XhsFakeSite", "FakePage", "make_site", "note", "TARGET_FILTERS", "detail_card", "creator_projection",
    "image_url", "image_bytes", "state_note", "whitelisted_state",
]
