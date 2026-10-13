"""OS lane 浏览器回放用的离线小红书站点（设计 §11.2）。

静态 HTML/JS（同目录的 explore.html、site.js、site.css、author.html、captcha.html）模拟 R2 的 DOM 与 V1、V16 的
搜索界面；页面里的请求全部经测试的 `context.route("**/*")` 交给 `XhsOfflineSite.handle`，用 fixture 回应或拒绝，
不联网、不起任何服务。站点自带的 JS 是假站点自身的前端，不是项目注入的脚本。

只由 tests/platforms/xhs/test_xhs_browser_replay.py（browser_replay 标记）使用。
"""

from __future__ import annotations

import asyncio
from collections import Counter
from hashlib import sha256
import html
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from support.stable_png import solid_png
from support.xhs_fake_site import TARGET_FILTERS, creator_projection, detail_card, image_url
from trippostcollect.xhs.search_target import is_target_search

SITE_DIR = Path(__file__).resolve().parent
ORIGIN = "https://www.xiaohongshu.com"
EXPLORE_URL = f"{ORIGIN}/explore"
SEARCH_URL = "https://so.xiaohongshu.com/api/sns/web/v2/search/notes"
FEED_URL = "https://edith.xiaohongshu.com/api/sns/web/v1/feed"
CAPTCHA_PATH = "/website-login/captcha"
AVATAR_HOST = "sns-avatar-qc.xhscdn.com"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
API_HEADERS = {
    "content-type": "application/json; charset=utf-8",
    "access-control-allow-origin": ORIGIN,
    "access-control-allow-credentials": "true",
    "access-control-expose-headers": "verifytype, verifyuuid",
}
STATIC_TYPES = {".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}

__all__ = [
    "EXPLORE_URL", "PNG_SIGNATURE", "TARGET_FILTERS", "XhsOfflineSite", "cover_url", "image_url", "search_item",
]


def cover_url(note_id: str) -> str:
    """列表卡片缩略图；与正文首图不是同一 URL（浮层另载 url_default）。"""
    return f"https://sns-webpic-qc.xhscdn.com/202601/{note_id}/cover/{note_id}!nc_n_webp_mw_1"


def search_item(note_id: str, *, card_type: str = "normal") -> dict[str, Any]:
    """搜索响应中的笔记项：cover.url_pre 为卡片缩略图，cover.url_default 即正文首图（浮层先载）。"""
    return {
        "id": note_id,
        "model_type": "note",
        "xsec_token": f"token-{note_id}",
        "note_card": {
            "type": card_type,
            "display_title": f"笔记 {note_id}",
            "cover": {"url_pre": cover_url(note_id), "url_default": image_url(note_id, 0)},
        },
    }


def _png_for(url: str) -> bytes:
    digest = sha256(url.encode()).digest()
    return solid_png(2, 2, (digest[0], digest[1], digest[2]))


class XhsOfflineSite:
    """目标会话的页由 `pages` 给出（每页一个 `search_item` 列表），最后一页 has_more=false。

    回车后的综合会话（不带 filters）只返回 `general_items`，has_more=false。`details[id]` 为 feed 的 note_card
    （默认 `detail_card(id)`）；`feed_overrides[id]` 依次替换每次 feed：整数为 HTTP 状态码（461/471 带验证头）。
    feed 响应延迟 `feed_delay_seconds` 才回应，使首图先于 feed 到达。`creators[user_id]` 为作者页投影的来源
    （默认 `creator_projection()`）。
    """

    def __init__(
        self,
        keyword: str,
        pages: list[list[dict[str, Any]]],
        *,
        general_items: list[dict[str, Any]] | None = None,
        feed_delay_seconds: float = 0.6,
    ) -> None:
        self.keyword = keyword
        self.pages = [list(items) for items in pages]
        self.general_items = list(general_items if general_items is not None else
                                  [search_item(f"general-{index}") for index in range(8)])
        self.feed_delay_seconds = feed_delay_seconds
        self.details: dict[str, dict[str, Any]] = {}
        self.feed_overrides: dict[str, list[Any]] = {}
        self.creators: dict[str, dict[str, Any]] = {}
        self.search_requests: list[dict[str, Any]] = []
        self.feed_requests: list[str] = []
        self.image_requests: Counter[str] = Counter()
        self.document_requests: list[str] = []
        self.unexpected: list[str] = []
        self.seen_requests: list[Any] = []
        self.handled_requests: list[Any] = []

    # 请求登记 -----------------------------------------------------------------

    def watch(self, context: Any) -> None:
        """记下 Context 内的全部请求，供结束时核对没有请求逃逸路由。"""
        context.on("request", self.seen_requests.append)

    def escaped(self) -> list[str]:
        handled = {id(request) for request in self.handled_requests}
        return [
            request.url for request in self.seen_requests
            if urlsplit(request.url).scheme in {"http", "https"} and id(request) not in handled
        ]

    def detail(self, note_id: str) -> dict[str, Any]:
        return self.details.setdefault(note_id, detail_card(note_id))

    def image_body(self, url: str) -> bytes:
        return _png_for(url)

    # 路由 ---------------------------------------------------------------------

    async def handle(self, route: Any) -> None:
        request = route.request
        self.handled_requests.append(request)
        parts = urlsplit(request.url)
        host, path = (parts.hostname or "").lower(), parts.path
        if request.method == "OPTIONS":
            await route.fulfill(status=204, headers={
                **API_HEADERS, "access-control-allow-methods": "GET, POST, OPTIONS",
                "access-control-allow-headers": request.headers.get("access-control-request-headers", "*"),
            })
            return
        if host == "www.xiaohongshu.com":
            await self._document(route, path)
        elif request.url.split("?", 1)[0] == SEARCH_URL and request.method == "POST":
            await self._search(route, request)
        elif request.url.split("?", 1)[0] == FEED_URL and request.method == "POST":
            await self._feed(route, request)
        elif host.endswith(".xhscdn.com") and request.method == "GET":
            self.image_requests[request.url] += 1
            await route.fulfill(status=200, headers={"content-type": "image/png"}, body=_png_for(request.url))
        else:
            self.unexpected.append(f"{request.method} {request.url}")
            await route.abort("blockedbyclient")

    async def _document(self, route: Any, path: str) -> None:
        self.document_requests.append(path)
        if path == "/explore":
            await self._file(route, "explore.html", "text/html; charset=utf-8")
        elif path.startswith("/__offline__/") and Path(path).suffix in STATIC_TYPES:
            await self._file(route, Path(path).name, STATIC_TYPES[Path(path).suffix])
        elif path.startswith("/user/profile/"):
            await route.fulfill(status=200, headers={"content-type": "text/html; charset=utf-8"},
                                body=self._author_page(path.rsplit("/", 1)[-1]))
        elif path == CAPTCHA_PATH:
            await self._file(route, "captcha.html", "text/html; charset=utf-8")
        elif path == "/favicon.ico":
            await route.fulfill(status=404, body=b"")
        else:
            self.unexpected.append(f"GET {ORIGIN}{path}")
            await route.abort("blockedbyclient")

    @staticmethod
    async def _file(route: Any, name: str, content_type: str) -> None:
        if name not in {"explore.html", "site.js", "site.css", "captcha.html"}:
            await route.abort("blockedbyclient")
            return
        await route.fulfill(status=200, headers={"content-type": content_type}, body=(SITE_DIR / name).read_bytes())

    def _author_page(self, user_id: str) -> bytes:
        creator = self.creators.get(user_id) or creator_projection()["creator"]
        avatar = f"https://{AVATAR_HOST}/avatar/{user_id}.jpg"
        basic = {**creator.get("basicInfo", {}), "imageb": avatar, "images": avatar, "redId": f"red-{user_id}"}
        state = {"user": {"userPageData": {**creator, "basicInfo": basic}, "notes": []}}
        fans = next((item["count"] for item in creator.get("interactions", []) if item.get("type") == "fans"), "0")
        text = (SITE_DIR / "author.html").read_text(encoding="utf-8")
        replacements = {
            "__TPC_STATE__": json.dumps(state, ensure_ascii=False).replace("<", "\\u003c"),
            "__TPC_AVATAR__": html.escape(avatar),
            "__TPC_NICKNAME__": html.escape(str(basic.get("nickname") or "")),
            "__TPC_FANS__": html.escape(str(fans)),
        }
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, value)
        return text.encode("utf-8")

    async def _search(self, route: Any, request: Any) -> None:
        body = request.post_data_json or {}
        self.search_requests.append(body)
        number = int(body.get("page") or 0)
        if "filters" in body and is_target_search(body) and 1 <= number <= len(self.pages):
            items, has_more = self.pages[number - 1], number < len(self.pages)
        else:
            items, has_more = (self.general_items if number == 1 else []), False
        hot = [{"id": f"hot-{number}", "model_type": "hot_query"}]
        payload = {"code": 0, "success": True, "msg": "成功", "data": {"has_more": has_more, "items": [*items, *hot]}}
        await route.fulfill(status=200, headers=API_HEADERS, body=json.dumps(payload, ensure_ascii=False).encode())

    async def _feed(self, route: Any, request: Any) -> None:
        body = request.post_data_json or {}
        note_id = str(body.get("source_note_id") or "")
        self.feed_requests.append(note_id)
        override = self.feed_overrides[note_id].pop(0) if self.feed_overrides.get(note_id) else None
        await asyncio.sleep(self.feed_delay_seconds)
        if isinstance(override, int):
            headers = {**API_HEADERS, "verifytype": "102", "verifyuuid": "offline"} if override in {461, 471} else API_HEADERS
            await route.fulfill(status=override, headers=headers, body=b"{}")
            return
        payload = {
            "code": 0, "success": True, "msg": "成功",
            "data": {"items": [{"id": note_id, "model_type": "note", "note_card": self.detail(note_id)}]},
        }
        await route.fulfill(status=200, headers=API_HEADERS, body=json.dumps(payload, ensure_ascii=False).encode())
