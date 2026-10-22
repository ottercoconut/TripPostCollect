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


def box(x: float, y: float, width: float, height: float) -> dict[str, float]:
    """页面脚本给出的元素框：中心点 x、y 与 left、top、width、height。"""
    return {"x": x, "y": y, "left": x - width / 2, "top": y - height / 2, "width": width, "height": height}


def inside(point: dict[str, float], x: float, y: float) -> bool:
    return point["left"] <= x <= point["left"] + point["width"] and point["top"] <= y <= point["top"] + point["height"]


SEARCH_BOX = box(640.0, 40.0, 400, 36)
FILTER_ENTRY = box(1200.0, 100.0, 28, 20)
FILTER_OPTION = box(700.0, 200.0, 48, 24)
# 面板挂在入口下方、向左展开，上沿与入口下沿相接；选项靠近面板左沿（越过选项的接近轨迹会出面板）。
FILTER_PANEL = box(943.0, 195.0, 558, 170)
CLOSE_POINT = box(1250.0, 50.0, 36, 36)
ARROW_POINT = box(760.0, 450.0, 32, 32)
AUTHOR_POINT = box(980.0, 130.0, 64, 20)
AVATAR_POINT = box(930.0, 130.0, 36, 36)
COMMENT_POINT = box(980.0, 600.0, 64, 20)
# “点点AI”对话栏的关闭按钮（#99）：对话栏关闭后这个位置是第 4 列第 1 行卡片的封面，盲点会打开笔记。
AI_CHAT_CLOSE = box(1225.0, 150.0, 28, 28)
AI_CHAT_INNER = box(1100.0, 500.0, 300, 760)
# 列表顶部的标签行（.search-layout__top，滚动位置为 0 时的视口坐标）：含筛选入口与“问点点 ai”标签（横向位置与尺寸
# 取自 Mac 人工侦察 2026-10-22，纵向在行的正中）。
TOP_ROW = {"left": 40.0, "top": 76.0, "right": 1240.0, "bottom": 116.0}
ASK_AI_TAG_X, ASK_AI_TAG_SIZE = 298.5, (65.0, 18.0)


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


def engagement_view(card: dict[str, Any]) -> dict[str, Any]:
    """详情视图内的互动栏与评论区（操作人 2026-10-18 实地结构）：嵌套字典的键为“标签.类名”（同级重名在后面加 #n），
    叶子为元素文字，缺键即元素不存在。feed 计数按原值显示；空串按实地观察：点赞文字为空、收藏显示“收藏”、评论显示
    “评论”，评论区为空状态。分享只有图标。有评论时列一条评论，其点赞同样是 .like-wrapper span.count，在文档顺序上
    先于底部的互动栏（不限定在互动栏内读就会读到它）。
    """
    interact = card.get("interact_info") or {}

    def shown(key: str, label: str) -> str:
        value = interact.get(key)
        return label if value in (None, "") else str(value)

    comments = interact.get("comment_count")
    if comments in (None, ""):
        section: dict[str, Any] = {"div.no-comments-text": {"span": "这是一片荒地 ", "span.focus-comments": "点击评论"}}
    else:
        section = {
            "div.total": f"共 {comments} 条评论",
            "div.list-container": {"div.parent-comment": {"div.like-wrapper": {"svg.like-icon": "", "span.count": "5"}}},
        }
    return {
        "div.comments-el": {"div.comments-container": section},
        "div.buttons.engage-bar-style": {
            "div.left": {
                "span.like-wrapper": {"svg.like-icon": "", "span.count": shown("liked_count", "")},
                "span.collect-wrapper": {"svg.collect-icon": "", "span.count": shown("collected_count", "收藏")},
                "span.chat-wrapper": {"svg.chat-icon": "", "span.count": shown("comment_count", "评论")},
            },
            "div.share-wrapper": {"svg.share-icon": ""},
        },
    }


def _key_matches(key: str, compound: str) -> bool:
    tag, *classes = key.split("#", 1)[0].split(".")
    wanted_tag, *wanted = compound.split(".")
    return (not wanted_tag or wanted_tag == tag) and set(wanted) <= set(classes)


def _descendants(node: dict[str, Any]) -> Iterable[tuple[str, Any]]:
    for key, child in node.items():
        yield key, child
        if isinstance(child, dict):
            yield from _descendants(child)


def select(node: Any, selector: str) -> list[Any]:
    """后代选择器（空格分隔、只含标签与类名的复合选择器）在 engagement_view 上按文档顺序匹配，同 querySelectorAll。"""
    current = [node]
    for compound in selector.split():
        current = [
            child for parent in current if isinstance(parent, dict)
            for key, child in _descendants(parent) if _key_matches(key, compound)
        ]
    return current


def text_of(node: Any) -> str:
    return "".join(text_of(child) for child in node.values()) if isinstance(node, dict) else str(node)


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
    """作者页不移动光标、不滚动（#98）；记录调用供断言。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def move(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("move")

    async def wheel(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("wheel")


# 搜索条目的 note_card.interact_info（操作人 2026-10-21 DevTools 实地结构）：四项计数为字符串，分享键为 shared_count
# （feed 为 share_count），另有布尔 liked、collected。默认与 detail_card 的 feed 计数相同。
SEARCH_INTERACT = {
    "liked": False, "liked_count": "1", "collected": False, "collected_count": "2", "comment_count": "3",
    "shared_count": "4",
}


def note(note_id: str, *, card_type: str = "normal", interact: dict[str, Any] | None = None) -> dict[str, Any]:
    """搜索响应中的笔记条目；`interact` 整体替换默认的 interact_info（缺键即不存在）。"""
    return {
        "id": note_id,
        "model_type": "note",
        "xsec_token": f"token-{note_id}",
        "note_card": {"type": card_type, "interact_info": dict(SEARCH_INTERACT if interact is None else interact)},
    }


class _Mouse:
    def __init__(self, site: "XhsFakeSite") -> None:
        self.site = site

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        entry = self.site.hover_target()
        entered = not inside(entry, *self.site.mouse_position) and inside(entry, x, y)
        self.site.mouse_position = (x, y)
        self.site.move_count += 1
        if entered:
            self.site.hover_entry()
        self.site.hover_leave(x, y)

    async def down(self) -> None:
        self.site.mouse_down = True

    async def up(self) -> None:
        assert self.site.mouse_down, "mouse up without down"
        self.site.mouse_down = False
        self.site.click(*self.site.mouse_position)

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
    `option_missing_opens` 为前几次打开面板时没有“图文”选项；`entry_click_applies_filter` 为点入口打开面板时前端同时
    发出目标会话第 1 页（触发者是 panel_open），之后点“图文”不再发请求；`hover_opens_panel` 为光标移入入口框时面板
    就打开（已开着不变；按下入口仍切换开关），面板开着时光标移到入口框与面板框 `panel_box`（默认 FILTER_PANEL）的并集之外即收起
    （`panel_leave_closes` 计次；`hover_gaps` 里的框也算在内，如 V16 入口与面板之间的间隙）；`entry_box`、`option_box`
    为入口文字 span 与“图文”的元素框（默认 FILTER_ENTRY、FILTER_OPTION），`entry_container_box` 为入口 div.filter 的框
    （默认同 span），悬停的进出按它算；`list_rect` 为滚动度量里结果列表与列表可见区的
    相交矩形（None 时不给，扫描按可见区比例落点），`wheel_points` 记下每次滚轮时的鼠标位置；`view_rect` 为列表
    滚动容器的可见区（None 为整页滚动，可见区即视口；给出时列表在容器内自顶部排起、滚动位置是容器的）；
    `cover_blocked[id]` 为这张卡片可见时前几次读数的封面中心被遮住（cover_hit 为假，点击落空）；
    `typed_keyword` 让回车请求体里的关键词与目标不同；`stall_after` 页之后前端不再加载；
    `hot_query` 每页附一条热搜项（无 DOM 卡片）；`unrendered_ids` 中的条目照常出现在响应里，但列表从不渲染（不占位置，
    后面卡片的 data-index 依次前移）；`card_shift[id]` 把这张卡片下移若干像素（多列瀑布流：它可能比 data-index
    更大的卡片更靠下；列表高度与加载时机仍按行数，下移后须落在后面页的行内）；可见区上下 `dom_margin` 像素内的卡片节点也在 DOM 中（card_diagnostic 读得到、visible_cards
    不列出）。`search_overrides[k]` 依次替换目标会话第 k 页的响应：
    字符串为请求失败文本（requestfailed），字典为响应 JSON；被替换的页不算已加载，前端下次滚动会重发。

    浮层：`details[id]` 为 feed 的 note_card（默认 `detail_card(id)`）。`feed_overrides[id]` 依次替换每次点击：
    "no_overlay" 浮层不出现；"no_feed" 浮层出现但无 feed；"net::…" 为 feed 请求失败；整数为 HTTP 状态码；
    字典为 feed 响应 JSON。

    “点点AI”对话栏（#99）：`ai_chat` 为每次回车进入结果页时对话栏打开（默认不打开，即 V16）。关闭按钮 AI_CHAT_CLOSE
    的 `ai_chat_button_mode` 为 "ok"、"missing"（没有按钮）、"covered"（中心点被盖住，点到的是盖住它的元素）或
    "aria_hidden"（在 aria-hidden 祖先内），`ai_chat_covered_from_read` 为从第几次 ai_chat_state 读数起按钮被盖住；
    前 `ai_chat_close_ignored` 次点中按钮不生效；生效后对话栏再保持
    `ai_chat_close_delay_reads` 次 ai_chat_state 读数才关。关闭时前端在同一非目标会话里续页 `ai_chat_followups`
    （默认第 2、3 页）；`ai_chat_followups_late` 为续页在点选“图文”、目标会话第 1 页到达之后才到。`ai_chat_log`
    依次记 ("read", 对话栏是否打开, 按钮是否可点)、("click",)、("closed",)。`top_row` 为列表顶部标签行（默认不给；
    `top_row_sticky` 为吸顶，否则随列表滚动），点中其中的“问点点 ai”标签（`ask_ai_box`）重新打开对话栏
    （`ask_ai_clicks` 计次）。

    `image_overrides[url]` 为 "missing"（从不加载）、HTTP 状态码，或前端实际请求的另一地址
    （如浏览器把 http 升级为 https、前端换了样式）。打开浮层加载前两张图，
    翻到第 k 页再加载第 k+1 张。`author_tab_failures` 次点击作者名不开标签页；浮层内的作者主页链接由
    `profile_links[id]` 给出（每项 user_id、point（不可见为 None）、comment、named），默认只有一个指向作者的可见作者名
    （AUTHOR_POINT）；`hidden_author_notes` 中笔记默认的作者名不可见。note_view_state 按脚本参数里的作者 ID 过滤，
    同页面脚本；点中 AUTHOR_POINT 或 AVATAR_POINT 打开作者页，点中 COMMENT_POINT 只记 `comment_clicks`；`author_url_ids[user]` 改写标签页
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
        self.mouse_down = False
        self.move_count = 0
        self.wheel_loaded = False
        self.box_focused = False
        self.box_value = ""
        self.scroll_y = 0
        self.panel_visible = False
        self.filter_applied = False
        self.entry_delay_reads: int | None = 0
        self.filter_reads = 0
        self.option_missing_opens = 0
        self.entry_click_applies_filter = False
        self.hover_opens_panel = False
        self.panel_box = dict(FILTER_PANEL)
        self.entry_box = dict(FILTER_ENTRY)
        self.entry_container_box: dict[str, float] | None = None
        self.option_box = dict(FILTER_OPTION)
        self.hover_gaps: list[dict[str, float]] = []
        # “点点AI”对话栏（#99）：`ai_chat` 为每次回车进入结果页时对话栏打开。
        self.ai_chat = False
        self.ai_chat_open = False
        self.ai_chat_button_mode = "ok"
        self.ai_chat_covered_from_read: int | None = None
        self.ai_chat_reads = 0
        self.ai_chat_close_ignored = 0
        self.ai_chat_close_delay_reads = 0
        self.ai_chat_closing: int | None = None
        self.ai_chat_followups = [2, 3]
        self.ai_chat_followups_late = False
        self.ai_chat_pending: list[tuple[int, str]] = []
        self.ai_chat_log: list[tuple] = []
        self.ask_ai_clicks = 0
        self.top_row: dict[str, float] | None = None
        self.top_row_sticky = False
        self.panel_opens = 0
        self.panel_leave_closes = 0
        self.diagnostic_reads = 0
        self.card_diagnostic_reads: list[list[str]] = []
        self.author_diagnostic_reads = 0
        self.hidden_author_notes: set[str] = set()
        self.profile_links: dict[str, list[dict[str, Any]]] = {}
        self.comment_clicks = 0
        self.click_points: list[tuple[float, float]] = []
        self.list_rect: dict[str, float] | None = None
        self.view_rect: dict[str, float] | None = None
        self.cover_blocked: dict[str, int] = {}
        self.unrendered_ids: set[str] = set()
        self.card_shift: dict[str, float] = {}
        self.dom_margin = 0.0
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
        # #94：engagement_views[id] 替换默认的互动栏与评论区；comments_late_reads[id] 次读数内评论区只有空容器。
        self.engagement_views: dict[str, dict[str, Any]] = {}
        self.comments_late_reads: dict[str, int] = {}
        self.engagement_reads: list[str] = []
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
        # 主页面的光标动作需要视口尺寸；作者页同页跳转时主页面也要做到达检查与就绪等待。
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
        rendered = [item for items in self.loaded for item in items if item["id"] not in self.unrendered_ids]
        return [{"id": note_id} for note_id in self.unregistered_ids] + rendered

    def view(self) -> dict[str, float]:
        return dict(self.view_rect) if self.view_rect else {
            "left": 0.0, "top": 0.0, "right": float(VIEW_WIDTH), "bottom": float(VIEW_HEIGHT),
        }

    def view_height(self) -> int:
        view = self.view()
        return int(view["bottom"] - view["top"])

    def list_origin(self) -> float:
        """第 0 行卡片在滚动位置为 0 时的视口纵坐标：整页滚动自 LIST_TOP 起，容器滚动自容器顶部起。"""
        return self.view()["top"] if self.view_rect else float(LIST_TOP)

    def scroll_height(self) -> int:
        rows = -(-len(self.rendered_items()) // COLUMNS)
        return int(self.list_origin() - self.view()["top"]) + rows * ROW_HEIGHT + FOOTER

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
        self.wheel_loaded = False
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
            view = self.view()
            return {
                "scroll_container": "element" if self.view_rect else "document",
                "scroll_y": self.scroll_y,
                "inner_height": self.view_height(),
                "inner_width": int(view["right"] - view["left"]),
                "scroll_height": self.scroll_height(),
                "view_rect": view,
                "list_rect": dict(self.list_rect) if self.list_rect else None,
                "top_row_rect": self.top_row_rect(),
                "layout_present": self.on_results,
                "with_ai_chat": self.on_results and self.ai_chat_open,
            }
        if name == "ai_chat_state":
            return self.ai_chat_state()
        if name == "filter_state":
            return self.filter_state(arg)
        if name == "filter_diagnostic":
            return self.filter_diagnostic()
        if name == "visible_cards":
            return self.read_visible_cards()
        if name == "card_diagnostic":
            return self.card_diagnostic(arg)
        if name == "author_link_diagnostic":
            return self.author_link_diagnostic(arg["author"])
        if name == "search_ready_counts":
            return {"card_count": len(self.rendered_items()), "profile_count": 1}
        if name == "note_view_state":
            return self.overlay_state(arg["author"])
        if name == "note_engagement":
            return self.note_engagement()
        raise AssertionError(f"unexpected page script: {name or script[:40]}")

    def top_row_shift(self) -> float:
        return 0.0 if self.top_row_sticky else float(self.scroll_y)

    def top_row_rect(self) -> dict[str, float] | None:
        """标签行与列表可见区的交集（同 scroll_metrics.top_row_rect）；没有或不在可见区内为 None。"""
        if self.top_row is None or not self.on_results:
            return None
        view, shift = self.view(), self.top_row_shift()
        top = max(self.top_row["top"] - shift, view["top"])
        bottom = min(self.top_row["bottom"] - shift, view["bottom"])
        if bottom <= top:
            return None
        return {"left": self.top_row["left"], "top": top, "right": self.top_row["right"], "bottom": bottom}

    def ask_ai_box(self) -> dict[str, float]:
        assert self.top_row is not None
        middle = (self.top_row["top"] + self.top_row["bottom"]) / 2 - self.top_row_shift()
        return box(ASK_AI_TAG_X, middle, *ASK_AI_TAG_SIZE)

    def ai_chat_state(self) -> dict[str, Any]:
        """同页面脚本 ai_chat_state；关闭生效后的延迟按读数递减，减完的那次读数已是关闭后。"""
        if self.ai_chat_closing is not None:
            if self.ai_chat_closing <= 0:
                self.ai_chat_closing = None
                self.close_ai_chat()
            else:
                self.ai_chat_closing -= 1
        self.ai_chat_reads += 1
        if self.ai_chat_covered_from_read is not None and self.ai_chat_reads >= self.ai_chat_covered_from_read:
            self.ai_chat_button_mode = "covered"
        opened = self.on_results and self.ai_chat_open
        mode = self.ai_chat_button_mode
        present = opened and mode != "missing"
        self.ai_chat_log.append(("read", opened, present and mode == "ok"))
        return {
            "with_ai_chat": opened,
            "ai_chat_inner": dict(AI_CHAT_INNER) if opened else None,
            "button_count": int(present),
            "button_visible": int(present),
            "button": dict(AI_CHAT_CLOSE) if present else None,
            "aria_label_ok": present,
            "aria_hidden_ancestor": present and mode == "aria_hidden",
            "center_hit": present and mode != "covered",
        }

    def close_ai_chat(self) -> None:
        """对话栏关闭：前端以同一 search_id、相同筛选签名续页（默认立即到达）。"""
        self.ai_chat_open = False
        self.ai_chat_log.append(("closed",))
        pages = [(number, self.search_id) for number in self.ai_chat_followups]
        if self.ai_chat_followups_late:
            self.ai_chat_pending.extend(pages)
            return
        for number, search_id in pages:
            self.respond_followup(number, search_id)

    def respond_followup(self, number: int, search_id: str) -> None:
        """非目标会话（回车会话）的第 number 页：不改当前会话。"""
        current = (self.search_id, self.session_filters)
        self.search_id, self.session_filters = search_id, None
        try:
            self.respond_page(number)
        finally:
            self.search_id, self.session_filters = current

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
            "entry": dict(self.entry_box) if rendered else None,
            "entry_container": dict(self.hover_target()) if rendered else None,
            "with_ai_chat": self.on_results and self.ai_chat_open,
            "panel_visible": self.panel_visible,
            "panel": dict(self.panel_box) if self.panel_visible else None,
            "option": dict(self.option_box) if has_option else None,
        }

    def filter_diagnostic(self) -> dict[str, Any]:
        self.diagnostic_reads += 1
        rendered = self.entry_rendered
        return {
            "layout_count": int(self.on_results),
            "entry_candidates": int(rendered),
            "entry_visible": rendered,
            "panel_count": int(self.on_results),
            "panel_visible_count": int(self.panel_visible),
            "label_nodes": int(rendered),
            "label_chains": [["span", "div.filter", "div.<id>", "div.search-layout"]] if rendered else [],
            "inner_width": VIEW_WIDTH,
            "inner_height": VIEW_HEIGHT,
            "scroll_y": self.scroll_y,
        }

    def card_top(self, index: int, note_id: str) -> float:
        return self.list_origin() + (index // COLUMNS) * ROW_HEIGHT + self.card_shift.get(note_id, 0.0) - self.scroll_y

    def visible_cards(self) -> list[dict[str, Any]]:
        """列表可见区内的卡片几何；封面中心落在可见区外或被 cover_blocked 遮住时 cover_hit 为假。不改变状态。

        页里的非笔记条目（如 hot_query，Mac 人工取证 2026-10-14）同样占一个列表位置，渲染为没有封面的卡片。
        """
        view = self.view()
        row = self.top_row_rect()
        cards = []
        for index, item in enumerate(self.rendered_items()):
            top = self.card_top(index, item["id"])
            bottom = top + ROW_HEIGHT - 20
            visible = max(0, min(bottom, view["bottom"]) - max(top, view["top"]))
            if visible <= 0:
                continue
            cover = box(160.0 + 300 * (index % COLUMNS), top + 130.0, 240, 240)
            has_cover = item.get("model_type") in (None, "note")
            cards.append(
                {
                    "note_id": item["id"],
                    "data_index": index,
                    "visible_ratio": visible / (bottom - top),
                    "top": top,
                    "bottom": bottom,
                    "cover": cover if has_cover else None,
                    "cover_hit": has_cover and view["top"] <= cover["y"] < view["bottom"]
                    and self.cover_blocked.get(item["id"], 0) <= 0
                    and not (self.top_row_sticky and row is not None and row["top"] <= cover["y"] <= row["bottom"]),
                }
            )
        return cards

    def card_diagnostic(self, note_ids: list[str]) -> list[dict[str, Any]]:
        """卡片节点读数：可见区上下 dom_margin 像素内的卡片在 DOM 中，位置相对列表可见区左上角。"""
        self.card_diagnostic_reads.append(list(note_ids))
        view = self.view()
        nodes = {}
        for index, item in enumerate(self.rendered_items()):
            top = self.card_top(index, item["id"])
            if top + ROW_HEIGHT - 20 > view["top"] - self.dom_margin and top < view["bottom"] + self.dom_margin:
                nodes[item["id"]] = (index, top, item.get("model_type") in (None, "note"))
        result = []
        for note_id in note_ids:
            if note_id not in nodes:
                result.append({"note_id": note_id, "nodes": 0})
                continue
            index, top, has_cover = nodes[note_id]
            result.append(
                {
                    "note_id": note_id,
                    "nodes": 1,
                    "data_index": str(index),
                    "has_cover": has_cover,
                    "cover_path": "/search_result" if has_cover else None,
                    "card": "section.note-item",
                    "first_child": "a.cover.mask" if has_cover else "div.query-note-wrapper",
                    "position": {"left": 0, "top": round(top - view["top"]), "width": 240, "height": 340},
                }
            )
        return result

    def overlay_links(self) -> list[dict[str, Any]]:
        note_id = self.overlay_note
        if note_id in self.profile_links:
            return self.profile_links[note_id]
        point = None if note_id in self.hidden_author_notes else dict(AUTHOR_POINT)
        return [{"user_id": str(self.detail(note_id)["user"]["user_id"]), "point": point, "comment": False, "named": True}]

    def author_link_diagnostic(self, author: str) -> dict[str, Any]:
        self.author_diagnostic_reads += 1
        if self.overlay_note is None:
            return {"view_visible": False}
        links = self.overlay_links()
        names = [link for link in links if link["named"]]
        own = [link for link in links if link["user_id"] == author]
        chain = ["a.name", "div.author", "div.author-container", "div.note-detail-mask", "body", "html"]
        return {
            "view_visible": True,
            "author_names": len(names),
            "author_names_visible": sum(link["point"] is not None for link in names),
            "profile_links": len(links),
            "profile_links_visible": sum(link["point"] is not None for link in links),
            "author_id_links": len(own),
            "author_id_links_visible": sum(link["point"] is not None for link in own),
            "profile_link_nodes": [
                {"visible": link["point"] is not None, "target": "_blank", "chain": chain} for link in links[:4]
            ],
        }

    def read_visible_cards(self) -> list[dict[str, Any]]:
        """页面脚本读数：可见卡片的遮挡计数各减一。"""
        cards = self.visible_cards()
        for card in cards:
            if self.cover_blocked.get(card["note_id"], 0) > 0:
                self.cover_blocked[card["note_id"]] -= 1
        return cards

    # 动作 ----------------------------------------------------------------------

    def click(self, x: float, y: float) -> None:
        self.click_points.append((x, y))

        def near(point: dict[str, float]) -> bool:
            return inside(point, x, y)

        if self.overlay_note is not None:
            if near(CLOSE_POINT) and self.overlay_closes:
                self.close_overlay()
            elif near(ARROW_POINT):
                self.flip()
            elif near(AUTHOR_POINT) or near(AVATAR_POINT):
                self.open_author_tab()
            elif near(COMMENT_POINT):
                self.comment_clicks += 1
            return
        if self.on_results and self.ai_chat_open and self.ai_chat_button_mode != "missing" and near(AI_CHAT_CLOSE):
            self.ai_chat_log.append(("click",))
            if self.ai_chat_button_mode == "covered":
                return
            if self.ai_chat_close_ignored > 0:
                self.ai_chat_close_ignored -= 1
            elif self.ai_chat_close_delay_reads > 0:
                self.ai_chat_closing = self.ai_chat_close_delay_reads
            else:
                self.close_ai_chat()
            return
        # 筛选面板与入口在卡片之上：落点同时落在标签与卡片封面内时点中的是标签。
        card = next(
            (card for card in self.visible_cards() if card["cover"] and card["cover_hit"] and near(card["cover"])), None,
        )
        if self.on_results and self.panel_visible and near(self.option_box):
            if self.apply_filter_works and not (self.entry_click_applies_filter and self.filter_applied):
                self.filter_applied = True
                self.start_session(filters=TARGET_FILTERS)
            for number, search_id in self.ai_chat_pending:
                self.respond_followup(number, search_id)
            self.ai_chat_pending = []
        elif self.entry_rendered and near(self.entry_box):
            if self.panel_visible and not self.panel_closes:
                return
            self.panel_visible = not self.panel_visible
            self.panel_opens += int(self.panel_visible)
            if self.panel_visible and self.entry_click_applies_filter and not self.filter_applied:
                self.filter_applied = True
                self.start_session(filters=TARGET_FILTERS)
        elif self.on_results and self.top_row is not None and near(self.ask_ai_box()):
            self.ask_ai_clicks += 1
            self.ai_chat_open = True
        elif self.on_results and card is not None:
            self.open_overlay(card["note_id"])
        elif not self.on_results and near(SEARCH_BOX):
            self.box_focused = True

    def hover_target(self) -> dict[str, float]:
        return self.entry_container_box or self.entry_box

    def hover_entry(self) -> None:
        if self.hover_opens_panel and self.overlay_note is None and self.entry_rendered and not self.panel_visible:
            self.panel_visible = True
            self.panel_opens += 1

    def hover_leave(self, x: float, y: float) -> None:
        held = (self.hover_target(), self.panel_box, *self.hover_gaps)
        if self.hover_opens_panel and self.panel_visible and not any(inside(area, x, y) for area in held):
            self.panel_visible = False
            self.panel_leave_closes += 1

    def press_enter(self) -> None:
        keyword = self.typed_keyword if self.typed_keyword is not None else self.box_value
        self.page.url = f"https://www.xiaohongshu.com/search_result_ai?keyword={quote(quote(keyword))}&source=web"
        self.box_focused = False
        self.filter_applied = False
        self.ai_chat_open = self.ai_chat
        self.ai_chat_closing = None
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
        """一个滚轮事件。一次滚动由若干手势、几十个事件组成；页面在下一页渲染前不会再发请求，所以两次页面读数之间
        的连续滚轮事件最多触发一页。"""
        maximum = max(0, self.scroll_height() - self.view_height())
        self.scroll_y = int(min(max(0, self.scroll_y + delta_y), maximum))
        if not self.wheel_loaded:
            loaded = len(self.loaded)
            self.maybe_load_next()
            self.wheel_loaded = len(self.loaded) > loaded

    def maybe_load_next(self) -> None:
        if self.session_filters is None or not self.loaded:
            return
        next_page = len(self.loaded) + 1
        if next_page > len(self.target_pages):
            return
        if self.stall_after is not None and next_page > self.stall_after:
            return
        if self.scroll_y + self.view_height() >= self.scroll_height() - PREFETCH_PX:
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

    def overlay_state(self, author: str = "") -> dict[str, Any]:
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
            "author_links": [
                {"point": link["point"], "comment": link["comment"], "named": link["named"]}
                for link in self.overlay_links()
                if author and link["user_id"] == author
            ],
        }

    def note_engagement(self) -> dict[str, Any]:
        """note_engagement 脚本在 engagement_view 上的等价读数，选择器与页面脚本相同。"""
        if self.overlay_note is None:
            return {"view_visible": False}
        note_id = self.overlay_note
        self.engagement_reads.append(note_id)
        view = self.engagement_views.get(note_id) or engagement_view(self.detail(note_id))
        if self.comments_late_reads.get(note_id, 0) > 0:
            self.comments_late_reads[note_id] -= 1
            view = {**view, "div.comments-el": {"div.comments-container": {}}}

        def first(found: list[Any]) -> Any:
            return found[0] if found else None

        def text(node: Any) -> str:
            return text_of(node).strip()[:32]

        bar = first(select(view, ".buttons.engage-bar-style"))
        left = first(select(bar, ".left")) if bar is not None else None

        def slot(wrapper: str) -> dict[str, Any]:
            counts = select(left, f"{wrapper} span.count") if left is not None else []
            return {
                "text": text(counts[0]) if counts else None,
                "matches": len(counts),
                "wrappers": len(select(view, wrapper)),
            }

        share = first(select(bar, ".share-wrapper")) if bar is not None else None
        share_count = first(select(share, ".count")) if share is not None else None
        total = first(select(view, "div.total"))
        return {
            "view_visible": True,
            "bar": bar is not None,
            "left": left is not None,
            "like": slot(".like-wrapper"),
            "collect": slot(".collect-wrapper"),
            "chat": slot(".chat-wrapper"),
            "share": {
                "button": share is not None,
                "text": text(share_count) if share_count is not None else None,
                "matches": int(share_count is not None),
                "wrappers": len(select(view, ".share-wrapper")),
            },
            "comments": {
                "container": bool(select(view, ".comments-container")),
                "total_text": text(total) if total is not None else None,
                "empty_state": bool(select(view, ".no-comments-text")),
            },
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
        request = self.page.request("GET", override if isinstance(override, str) else url, resource_type="image")
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
    "AI_CHAT_CLOSE", "TOP_ROW",
    "engagement_view", "image_url", "image_bytes", "state_note", "whitelisted_state",
]
