"""小红书 T09：冻结旧实现与根平台实现在同一离线边界下逐项对照。

两侧都只替换最外层边界：Playwright/CDP、HTTPX 工厂、xhshow、单调时钟、sleep 与随机源。
边界在源模块上替换后再全新导入被测实现，因此不依赖新实现内部如何传递这些依赖；
业务代码、tenacity 重试层、行为桥、候选累计器、JSONL 与图片 staging 全部真实运行。
"""

from __future__ import annotations

import ast
import asyncio
import asyncio.base_events
from collections import Counter
import dataclasses
from hashlib import sha256
import importlib
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import humps
from PIL import Image
from playwright._impl._errors import TargetClosedError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
import pytest
import tenacity._asyncio

from support.creator_runtime_profile import use_legacy_creator_page_read
from support.main_page_lifecycle import use_legacy_main_page_closed_name
from support.raw_author_identity import XHS_KEEP_AUTHOR_DETAIL_ENV, use_raw_author_identity
from trippostcollect.application import events
from trippostcollect.platforms import _fork_bridge
from trippostcollect.runtime import behavior as runtime_behavior
from trippostcollect.runtime import browser as runtime_browser
from trippostcollect.runtime import http as runtime_http
from trippostcollect.runtime import human_flow


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/adapter_t09"
FORK = ROOT / "tools/MediaCrawler"
REAL_SLEEP = asyncio.sleep
REAL_MONOTONIC = time.monotonic
CLOCK_START = 1000.0
EPOCH = 1_760_000_000.0
SEED = 20261007
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
ADDRESS_RE = re.compile(r"0x[0-9a-f]{6,}")
LEGACY_MODULES = (
    ("model.m_xiaohongshu", "model/m_xiaohongshu.py"),
    ("tools.image_manifest", "tools/image_manifest.py"),
    ("tools.trippostcollect_behavior", "tools/trippostcollect_behavior.py"),
    ("mediacrawler_behavior", "scripts/mediacrawler_behavior.py"),
    ("store.xhs", "store/xhs/__init__.py"),
    ("media_platform.xhs", "media_platform/xhs/__init__.py"),
    ("mediacrawler_export_entrypoint", "scripts/mediacrawler_export_entrypoint.py"),
)
# 共享的 fork 薄转发在导入时按名绑定边界，旧侧每次随冻结实现重新执行。
LEGACY_SHARED = (
    ("tools.httpx_util", "tools/httpx_util.py"),
    ("tools._browser_bridge", "tools/_browser_bridge.py"),
    ("tools.cdp_browser", "tools/cdp_browser.py"),
)


# ---------------------------------------------------------------------------
# 两侧实现装载


def frozen_source(relative: str) -> bytes:
    metadata = json.loads((FIXTURE / "baseline.json").read_text())
    data = (FIXTURE / (relative + ".txt")).read_bytes()
    assert sha256(data).hexdigest() == metadata["files"][relative], relative
    return data


def fork_source(relative: str) -> bytes:
    """仅供迁移前自检：当前 fork/scripts 原文。"""
    base = ROOT if relative.startswith("scripts/") else FORK
    return (base / relative).read_bytes()


def _load(patch, name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    patch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    parent, _, attr = name.rpartition(".")
    if parent:
        patch.setattr(importlib.import_module(parent), attr, module, raising=False)
    return module


def _register_new_modules(patch, prefixes: tuple[str, ...]) -> None:
    for name in list(sys.modules):
        if name.startswith(prefixes):
            module = sys.modules.pop(name)
            patch.setitem(sys.modules, name, module)


class LegacySide:
    """冻结旧实现：fork 5a68eb5 的 XHS 文件与 main b4e893b 的两个 scripts。"""

    def __init__(self, source=frozen_source, label: str = "legacy"):
        self.source = source
        self.label = label

    def install(self, patch, workdir: Path):
        _fork_bridge.install()
        for parent in ("model", "tools", "store", "media_platform"):
            importlib.import_module(parent)
        exact = {name for name, _ in LEGACY_MODULES + LEGACY_SHARED}
        for name in list(sys.modules):
            if name in exact or name.startswith(("media_platform.xhs", "store.xhs")):
                patch.delitem(sys.modules, name)
        # 旧文件原文恢复到临时目录；包内相对导入从同一目录解析。
        directory = workdir / f"{self.label}-source"
        for relative in json.loads((FIXTURE / "baseline.json").read_text())["files"]:
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.source(relative))
        modules = {}
        for name, relative in LEGACY_MODULES[:3]:
            modules[name] = _load(patch, name, directory / relative)
        for name, relative in LEGACY_SHARED:
            modules[name] = _load(patch, name, FORK / relative)
        for name, relative in LEGACY_MODULES[3:]:
            modules[name] = _load(patch, name, directory / relative)
        _register_new_modules(patch, ("media_platform.xhs.", "store.xhs."))
        # #49：根实现保存作者原始 ID 与昵称；旧 store 按正式 worker 取值写出原值并替换身份转换。
        use_raw_author_identity(patch, modules["store.xhs"])
        patch.setenv(XHS_KEEP_AUTHOR_DETAIL_ENV, "1")
        import trippostcollect.platforms.entry as entry

        package = "media_platform.xhs."
        return SimpleNamespace(
            side=self,
            entry=entry,
            core=sys.modules[package + "core"],
            client=sys.modules[package + "client"],
            login=sys.modules[package + "login"],
            manual_wait=sys.modules[package + "manual_wait"],
            errors=sys.modules[package + "exception"],
            repair=modules["mediacrawler_export_entrypoint"],
            behavior=modules["mediacrawler_behavior"],
            signer=sys.modules[package + "playwright_sign"],
            parser=SimpleNamespace(
                help=sys.modules[package + "help"],
                extractor=sys.modules[package + "extractor"],
                store=modules["store.xhs"],
                manifest=modules["tools.image_manifest"],
                models=sys.modules[package + "field"],
            ),
        )

    def install_hooks(self, mods) -> None:
        # 旧 worker 的 XHS 修复入口：冻结旧桥 E 原样替换冻结 crawler 的方法。
        mods.repair.install_xhs_repair_resilience()

    def crawler_class(self, mods):
        return mods.core.XiaoHongShuCrawler


class RootSide:
    """迁移后实现：worker 入口与 trippostcollect.platforms.xhs。"""

    label = "root"

    def install(self, patch, workdir: Path):
        import trippostcollect.platforms as package

        for name in list(sys.modules):
            if name == "trippostcollect.platforms.entry" or name.startswith("trippostcollect.platforms.xhs"):
                patch.delitem(sys.modules, name)
        patch.delattr(package, "xhs", raising=False)
        patch.delattr(package, "entry", raising=False)
        try:
            entry = importlib.import_module("trippostcollect.platforms.entry")
            prefix = "trippostcollect.platforms.xhs."
            modules = {
                name: importlib.import_module(prefix + name)
                for name in ("core", "client", "login", "manual_wait", "errors", "repair",
                             "behavior", "signer", "parser", "models", "author", "session")
            }
        finally:
            _register_new_modules(patch, ("trippostcollect.platforms.entry", "trippostcollect.platforms.xhs"))
        # #52：作者页取数改为运行时投影优先；对照时换回旧的 content() + 静态解析，其余逐字节比较。
        use_legacy_creator_page_read(patch, modules["author"])
        # #55：主页面关闭改为生命周期异常子类；对照时只把其类名记为 RuntimeError，消息逐字比较。
        use_legacy_main_page_closed_name(patch, modules["errors"], modules["session"])
        return SimpleNamespace(
            side=self,
            entry=entry,
            core=modules["core"],
            client=modules["client"],
            login=modules["login"],
            manual_wait=modules["manual_wait"],
            errors=modules["errors"],
            repair=modules["repair"],
            behavior=modules["behavior"],
            signer=modules["signer"],
            parser=SimpleNamespace(
                help=modules["parser"], extractor=modules["parser"], store=modules["parser"],
                manifest=modules["parser"], models=modules["models"],
            ),
        )

    def install_hooks(self, mods) -> None:
        mods.entry.install_hooks()

    def crawler_class(self, mods):
        # 与三站先例一致：可返回根类本身，或在 platforms/xhs、entry 中定义的装配子类；不得来自 fork。
        cls = mods.entry.load_crawler("xhs")
        assert issubclass(cls, mods.core.XiaoHongShuCrawler)
        assert (cls.__module__.startswith("trippostcollect.platforms.xhs")
                or cls.__module__ == "trippostcollect.platforms.entry"), cls.__module__
        return cls


# 旧桥链路上按名绑定边界或随机源的模块，每次随被测实现重新导入。
LEGACY_BRIDGE_MODULES = (
    "model.m_xiaohongshu", "tools.image_manifest", "tools.trippostcollect_behavior",
    "tools.httpx_util", "tools._browser_bridge", "tools.cdp_browser",
    "mediacrawler_behavior", "mediacrawler_export_entrypoint",
)


class LegacyFactorySide(RootSide):
    """旧桥：fork main 的 CrawlerFactory 仍能装配小红书，且全部委托根实现（T14 前行为不变）。"""

    label = "legacy-factory"

    def install(self, patch, workdir: Path):
        mods = self.install_root(patch, workdir)
        _fork_bridge.install()
        for parent in ("model", "tools", "store", "media_platform"):
            importlib.import_module(parent)
        for name in list(sys.modules):
            if name in LEGACY_BRIDGE_MODULES or name.startswith(("media_platform.xhs", "store.xhs")):
                patch.delitem(sys.modules, name)
        for parent in ("media_platform", "store"):
            patch.delattr(importlib.import_module(parent), "xhs", raising=False)

        def unused_crawler():
            raise AssertionError("小红书对照不得构造其他平台")

        # main 顶层还装载其他站点、旧参数解析器和数据库入口；只真实加载小红书链路。
        patch.setitem(sys.modules, "cmd_arg", SimpleNamespace())
        patch.setitem(sys.modules, "database", SimpleNamespace(db=SimpleNamespace()))
        for platform, name in (
            ("bilibili", "BilibiliCrawler"), ("douyin", "DouYinCrawler"), ("kuaishou", "KuaishouCrawler"),
            ("tieba", "TieBaCrawler"), ("weibo", "WeiboCrawler"), ("zhihu", "ZhihuCrawler"),
        ):
            patch.setitem(sys.modules, "media_platform." + platform, SimpleNamespace(**{name: unused_crawler}))
        try:
            mods.legacy_main = _load(patch, "t09_legacy_main", FORK / "main.py")
        finally:
            _register_new_modules(patch, ("media_platform.xhs", "store.xhs", *LEGACY_BRIDGE_MODULES))
        return mods

    def install_root(self, patch, workdir: Path):
        return RootSide.install(self, patch, workdir)

    def install_hooks(self, mods) -> None:
        # 旧桥 E 的 XHS 修复安装点；其余 export/批次 hook 与本对照无关。
        importlib.import_module("mediacrawler_export_entrypoint").install_xhs_repair_resilience()

    def crawler_class(self, mods):
        legacy = importlib.import_module("media_platform.xhs")
        assert mods.legacy_main.CrawlerFactory.CRAWLERS["xhs"] is legacy.XiaoHongShuCrawler
        return lambda: mods.legacy_main.CrawlerFactory.create_crawler("xhs")


# ---------------------------------------------------------------------------
# 平台数据


NICK = {"u1": "青岛作者一", "u2": "崂山作者二", "u3": "作者三", "u6": "失联作者"}


def image_object(note_id: str, index: int, *keys: str) -> dict:
    item = {"width": 4, "height": 3, "info_list": []}
    for key in keys:
        item[key] = (
            "https://sns-webpic-qc.xhscdn.com/202610/"
            f"{note_id}/notes_pre_post/{note_id}-{index}-{key}!nd_dft_wlteh_webp_3"
        )
    return item


def note_payload(note_id: str, user: str, *, kind: str = "normal", images=None) -> dict:
    return {
        "note_id": note_id,
        "type": kind,
        "title": f"{note_id} 青岛游记",
        "desc": f"青岛旅游 {note_id}：栈桥看海、崂山徒步与八大关散步。",
        "time": 1_759_000_000_000,
        "last_update_time": 1_759_000_100_000,
        "ip_location": "山东",
        "user": {
            "user_id": user,
            "nickname": NICK.get(user, user),
            "avatar": f"https://sns-avatar-qc.xhscdn.com/avatar/{user}.jpg",
        },
        "interact_info": {
            "liked_count": "12", "collected_count": "3", "comment_count": "4", "share_count": "1",
        },
        "image_list": images if images is not None else [image_object(note_id, 0, "url_default", "url", "url_pre")],
        "tag_list": [{"name": "青岛", "type": "topic"}, {"name": "旅行", "type": "other"}],
    }


NOTES = {
    # 三个图片对象分别覆盖 url_default>url>url_pre 三种优先级。
    "n1": note_payload("n1", "u1", images=[
        image_object("n1", 0, "url_default", "url", "url_pre"),
        image_object("n1", 1, "url", "url_pre"),
        image_object("n1", 2, "url_pre"),
    ]),
    "n2": note_payload("n2", "u2"),
    "n3": note_payload("n3", "u1", kind="video"),
    "n4": note_payload("n4", "u1"),
    **{f"r{index}": note_payload(f"r{index}", "u6" if index == 6 else "u3", kind="video" if index == 3 else "normal")
       for index in range(1, 8)},
}


def search_item(note_id: str) -> dict:
    if note_id.startswith("rec"):
        return {"id": note_id, "model_type": "rec_query", "rec_query": {"title": "相关搜索"}}
    return {
        "id": note_id, "model_type": "note", "xsec_token": f"tok-{note_id}", "xsec_source": "pc_search",
        "note_card": {"display_title": f"{note_id} 搜索卡片标题", "type": "normal"},
    }


def creator_html(user: str, fans: str) -> str:
    state = {"user": {"userPageData": {
        "basicInfo": {
            "nickname": NICK.get(user, user), "desc": f"{NICK.get(user, user)}的主页",
            "imageb": f"https://sns-avatar-qc.xhscdn.com/avatar/{user}-b.jpg", "ipLocation": "山东", "gender": 0,
        },
        "interactions": [
            {"type": "follows", "count": "5"}, {"type": "fans", "count": fans}, {"type": "interaction", "count": "99"},
        ],
    }}}
    return (
        "<html><head><title>作者主页</title></head><body><script>window.__INITIAL_STATE__="
        + json.dumps(state, ensure_ascii=False) + "</script></body></html>"
    )


def note_html(note_id: str) -> str:
    state = {"note": {"noteDetailMap": {note_id: {"note": humps.camelize(NOTES[note_id])}}}}
    return "<html><body><script>window.__INITIAL_STATE__=" + json.dumps(state, ensure_ascii=False) + "</script></body></html>"


_PNG_CACHE: dict[str, bytes] = {}


def png_bytes(url: str) -> bytes:
    if url not in _PNG_CACHE:
        digest = sha256(url.encode()).digest()
        buffer = BytesIO()
        Image.new("RGB", (4, 3), color=(digest[0], digest[1], digest[2])).save(buffer, format="PNG")
        _PNG_CACHE[url] = buffer.getvalue()
    return _PNG_CACHE[url]


# ---------------------------------------------------------------------------
# 场景定义


@dataclasses.dataclass(frozen=True)
class Scenario:
    name: str
    login: str = "already"  # already/scan/scan_tab/qr_refresh/latched/never
    crawler_type: str = "search"
    pages: tuple = ((1, ("n1",), False),)
    start_page: int = 1
    refresh_pages: int = 0
    resume_cursor: str = ""
    env: tuple = ()
    detail_urls: tuple = ()
    second_start: bool = False
    concurrent_session: bool = False


SUCCESS_PAGES = (
    (1, ("n1", "rec-1", "n2", "n3", "n-db", "n-seen", "n-resume"), True),
    (2, ("n1", "n4"), True),
    (3, (), False),
)
REPAIR_URLS = tuple(
    f"https://www.xiaohongshu.com/explore/r{index}?xsec_token=tok-r{index}&xsec_source=pc_search"
    for index in range(1, 8)
)
SCENARIOS = {item.name: item for item in (
    Scenario("success", login="scan", pages=SUCCESS_PAGES, start_page=2, refresh_pages=1,
             resume_cursor="2fsavedsearchid"),
    Scenario("explore_blank", pages=((1, (), False),)),
    Scenario("explore_blank_fail"),
    Scenario("qr_expired_refresh", login="qr_refresh"),
    Scenario("scan_latched", login="latched"),
    Scenario("login_progress_tab", login="scan_tab"),
    Scenario("login_budget_exhausted", login="never", env=(("TRIPPOSTCOLLECT_XHS_QR_REFRESH_SECONDS", "60"),)),
    Scenario("qr_env_invalid", login="scan", env=(("TRIPPOSTCOLLECT_XHS_QR_REFRESH_SECONDS", "abc"),)),
    Scenario("captcha_pass"),
    Scenario("captcha_rate_limited"),
    Scenario("captcha_timeout", login="scan"),
    Scenario("login_expired"),
    Scenario("creator_primary_login", pages=((1, ("n2",), False),)),
    Scenario("network_recover", pages=((1, ("n1",), False),)),
    Scenario("network_timeout"),
    Scenario("security_300011"),
    Scenario("creator_300012"),
    Scenario("main_page_closed"),
    Scenario("cdp_disconnect"),
    Scenario("cdp_launch_failed", second_start=True),
    Scenario("cdp_concurrent_session", concurrent_session=True),
    Scenario("interaction_like", env=(("TRIPPOSTCOLLECT_XHS_POST_INTERACTION", "like-one"),)),
    Scenario("interaction_comment", env=(("TRIPPOSTCOLLECT_XHS_POST_INTERACTION", "comment-scroll"),)),
    Scenario("batch_publish_failure"),
    Scenario("continuity_verification"),
    Scenario("continuity_blocked"),
    Scenario("search_ready_verification"),
    Scenario("creator_page_security", pages=((1, ("n2",), False),)),
    Scenario("creator_page_verification", pages=((1, ("n2",), False),)),
    Scenario("selfinfo_verification"),
    Scenario("search_429"),
    Scenario("image_blocking"),
    Scenario("candidate_skips", pages=((1, ("n1", "n2"), False),)),
    Scenario("repair", crawler_type="detail", detail_urls=REPAIR_URLS),
    Scenario("repair_ip_blocked", crawler_type="detail", detail_urls=REPAIR_URLS),
    Scenario("repair_300011", crawler_type="detail", detail_urls=REPAIR_URLS),
)}


# ---------------------------------------------------------------------------
# 离线浏览器与 HTTP


@dataclasses.dataclass
class View:
    text: str = ""
    tokens: frozenset = frozenset()
    frames: tuple = ()
    rendered: bool = True
    cards: int = 0
    login_control: bool = False
    html: str = ""


def selector_token(selector: str) -> str:
    if selector == "body":
        return "body"
    if "qrcode-img" in selector:
        return "qr"
    if "点击刷新" in selector or "重新获取二维码" in selector:
        return "qr_refresh"
    if selector.startswith("xpath=//*[@id='app']"):
        return "login_button"
    if "/user/profile/" in selector or "normalize-space()='我'" in selector:
        return "profile"
    if "one-time-code" in selector or "验证码" in selector:
        return "sms_input"
    if any(marker in selector for marker in ("安全验证", "captcha", "verify", "verification", "slider", "geetest")):
        return "captcha_control"
    if "like" in selector or "点赞" in selector:
        return "like"
    if "comment" in selector or "评论" in selector:
        return "comments"
    return "other:" + selector


class Locator:
    def __init__(self, page, selector: str, frame_index: int):
        self.page = page
        self.selector = selector
        self.frame_index = frame_index

    @property
    def first(self):
        return self

    def nth(self, index):
        return self

    def _visible(self) -> bool:
        view = self.page.world.view(self.page)
        token = selector_token(self.selector)
        if self.frame_index == 0:
            return token in view.tokens
        return token in view.frames[self.frame_index - 1][1]

    async def count(self):
        if self.selector == "body":
            return 1
        visible = self._visible()
        self.page.world.record("visible", self.page.id, self.frame_index, self.selector, visible)
        return int(visible)

    async def is_visible(self, timeout=None):
        visible = self._visible()
        self.page.world.record("visible", self.page.id, self.frame_index, self.selector, visible)
        return visible

    async def inner_text(self, timeout=None):
        view = self.page.world.view(self.page)
        self.page.world.record("text", self.page.id, self.frame_index)
        if self.frame_index == 0:
            return view.text if view.rendered else ""
        return view.frames[self.frame_index - 1][0]

    async def click(self, timeout=None, delay=None):
        self.page.world.record("click", self.page.id, self.frame_index, self.selector, delay)
        self.page.world.on_click(self.page, selector_token(self.selector))

    async def scroll_into_view_if_needed(self, timeout=None):
        self.page.world.record("scroll_into_view", self.page.id, self.selector)

    async def hover(self, timeout=None):
        self.page.world.record("hover", self.page.id, self.selector)

    async def evaluate(self, script, arg=None):
        assert "aria_pressed" in script
        self.page.world.record("like_state", self.page.id, self.page.liked)
        return {
            "aria_pressed": "true" if self.page.liked else "false", "aria_label": "点赞", "title": "",
            "data_state": "", "class_name": "like-wrapper", "text": "12", "icon_ref": "#like",
            "active_descendant": False,
        }


class Frame:
    def __init__(self, page, index: int):
        self.page = page
        self.index = index

    def locator(self, selector):
        return Locator(self.page, selector, self.index)


class Mouse:
    def __init__(self, page):
        self.page = page

    async def move(self, x, y, steps=None):
        self.page.world.record("mouse_move", self.page.id, x, y, steps)

    async def wheel(self, dx, dy):
        self.page.scroll_y += int(dy)
        self.page.world.record("mouse_wheel", self.page.id, dx, dy)


class Response:
    def __init__(self, status):
        self.status = status


class Page:
    viewport_size = {"width": 1280, "height": 800}

    def __init__(self, world, url: str):
        self.world = world
        self.id = f"p{len(world.pages)}"
        world.pages.append(self)
        self._url = url
        self.closed = False
        self.context = world.context
        self.main_frame = Frame(self, 0)
        self.mouse = Mouse(self)
        self.navigations = Counter()
        self.scroll_y = 0
        self.liked = False
        self.goto_at = world.clock

    @property
    def url(self):
        self.world.tick()
        return self._url

    @property
    def frames(self):
        view = self.world.view(self)
        return [self.main_frame, *(Frame(self, index + 1) for index in range(len(view.frames)))]

    def is_closed(self):
        return self.closed

    def on(self, event, handler):
        self.world.record("page_on", self.id, event)

    def locator(self, selector):
        return Locator(self, selector, 0)

    async def is_visible(self, selector, timeout=None):
        return await Locator(self, selector, 0).is_visible(timeout)

    async def goto(self, url, wait_until=None, timeout=None):
        self.world.tick()
        self.world.record("goto", self.id, url, wait_until, timeout)
        self.world.before_goto(self, url)
        self._url = url
        self.goto_at = self.world.clock
        self.navigations[self.world.kind(url)] += 1
        self.world.after_goto(self, url)
        return Response(200)

    async def reload(self, wait_until=None, timeout=None):
        self.world.record("reload", self.id, wait_until, timeout)
        self.world.on_reload(self)

    async def bring_to_front(self):
        self.world.record("bring_to_front", self.id)

    async def close(self):
        self.world.record("close", self.id)
        self.closed = True
        if self in self.world.context._pages:
            self.world.context._pages.remove(self)

    async def wait_for_load_state(self, state, timeout=None):
        self.world.record("load_state", self.id, state, timeout)

    async def wait_for_timeout(self, milliseconds):
        self.world.record("wait_for_timeout", self.id, milliseconds)
        await REAL_SLEEP(0)
        self.world.clock += float(milliseconds) / 1000

    async def wait_for_selector(self, selector=None, **kwargs):
        visible = await Locator(self, selector, 0).is_visible()
        self.world.record("wait_for_selector", self.id, selector, visible)
        if not visible:
            raise PlaywrightTimeoutError("二维码元素未出现")
        return SimpleNamespace(get_property=self._qr_source)

    async def _qr_source(self, name):
        return "data:image/png;base64,UVItZmFrZQ=="

    async def content(self):
        view = self.world.view(self)
        self.world.record("content", self.id)
        return view.html or f"<html><body>{view.text}</body></html>"

    async def screenshot(self, path=None, **kwargs):
        self.world.record("screenshot", self.id, path, sorted(kwargs.items()))

    async def evaluate(self, script, arg=None):
        view = self.world.view(self)
        if "ready_state" in script:
            kind = "render"
            result = {
                "ready_state": "complete" if view.rendered else "loading", "title": "小红书",
                "dom_length": len(view.html or view.text) + 100,
                "body_text_length": len(view.text) if view.rendered else 0,
                "body_child_element_count": 3 if view.rendered else 0, "visibility_state": "visible",
            }
        elif "navigator.webdriver" in script and "brands" in script:
            kind = "identity"
            result = {
                "webdriver": None, "user_agent": UA, "language": "zh-CN", "platform": "macOS", "mobile": False,
                "brands": [{"brand": "Not)A;Brand", "version": "8"}, {"brand": "Chromium", "version": "138"},
                           {"brand": "Google Chrome", "version": "138"}],
            }
        elif "querySelectorAll(\"a, button" in script:
            kind = "login_control"
            result = view.login_control
        elif "card_count" in script:
            kind = "cards"
            result = {"card_count": view.cards, "profile_count": view.cards}
        elif "scrollingElement" in script:
            kind = "scroll"
            result = {"window_y": self.scroll_y, "scrollable_count": 1, "scroll_top_sum": self.scroll_y,
                      "max_scroll_top": self.scroll_y}
        elif "innerWidth" in script:
            kind = "size"
            result = {"width": 1280, "height": 800}
        else:
            raise AssertionError(f"未预期的 evaluate：{script[:80]}")
        # 记录脚本摘要：页面内 JS 原文同属行为，迁移后必须逐字相同。
        self.world.record("evaluate", self.id, kind, sha256(script.encode()).hexdigest()[:12])
        return result


class Context:
    def __init__(self, world):
        self.world = world
        self._pages: list[Page] = []
        self._handlers: dict[str, list] = {}

    @property
    def pages(self):
        return list(self._pages)

    def on(self, event, handler):
        self.world.record("context_on", event)
        self._handlers.setdefault(event, []).append(handler)

    def emit_page(self, page):
        for handler in self._handlers.get("page", []):
            handler(page)

    async def new_page(self):
        page = Page(self.world, "about:blank")
        self._pages.append(page)
        self.world.record("new_page", page.id)
        self.emit_page(page)
        return page

    async def cookies(self, urls=None):
        self.world.tick()
        self.world.record("cookies", urls)
        return self.world.cookie_list()

    async def add_cookies(self, cookies):
        self.world.record("add_cookies", cookies)

    async def add_init_script(self, script=None, path=None):
        self.world.record("init_script", sha256(str(script or path).encode()).hexdigest()[:12])

    async def close(self):
        self.world.record("context_close")


class AsyncClient:
    def __init__(self, world, kwargs):
        self.world = world
        world.record("http_client", sorted(kwargs.items()))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def request(self, method, url, **kwargs):
        return await self.world.http(method, url, kwargs)


# ---------------------------------------------------------------------------
# 场景世界


class World:
    def __init__(self, scenario: Scenario, out: Path):
        self.scenario = scenario
        self.name = scenario.name
        self.out = out
        self.clock = CLOCK_START
        self.trace: list[tuple] = []
        self.counts = Counter()
        self.pages: list[Page] = []
        self.context = Context(self)
        self.manager = None
        self.authenticated = scenario.login == "already"
        self.session = "ws-login" if self.authenticated else "ws-anon"
        self.login_started_at = None
        self.qr_generation = 0
        self.qr_born_at = None
        self.session_lost_at = None
        self.relogin_due = None
        self.captcha_started_at = None
        self.captcha_redirect = ""
        self.verified = False
        self.cdp_lost = False
        self.outages: dict[str, float] = {}
        self.popup_opened = False
        self.window: tuple[float, float] | None = None
        self.meta = {"ready_modules": [], "login_budgets": [], "budget_inits": 0, "publishes": 0}

    # -- 记录与时钟 --
    def record(self, kind, *values):
        self.trace.append((round(self.clock - CLOCK_START, 3), kind, *values))

    def monotonic(self):
        return self.clock

    def wall(self):
        return EPOCH + (self.clock - CLOCK_START)

    async def sleep(self, delay, result=None):
        # 先让同一时刻已就绪的任务运行（如新标签页守卫立即置前），再推进虚拟时钟。
        seconds = max(0.0, float(delay))
        self.record("sleep", round(seconds, 3))
        await REAL_SLEEP(0)
        self.clock += seconds
        return result

    # -- 页面视图 --
    @staticmethod
    def kind(url: str) -> str:
        parts = urlsplit(url)
        path = parts.path.rstrip("/")
        if not parts.netloc:
            return "blank"
        if "/website-login/captcha" in path:
            return "captcha"
        if "/website-login/verify" in path:
            return "verify"
        if path == "/explore":
            return "explore"
        if path == "/search_result":
            return "search"
        if path.startswith("/user/profile/"):
            return "profile"
        if path.startswith("/explore/"):
            return "note"
        return "other"

    def tick(self):
        if (self.scenario.login == "scan_tab" and self.login_started_at is not None and not self.popup_opened
                and self.login_phase() == "progress"):
            # 扫码后平台在新标签页展示安全验证，原页仍是旧二维码。
            self.popup_opened = True
            self.open_popup("https://www.xiaohongshu.com/website-login/verify?source=qrcode")
        if (self.login_started_at is not None and not self.authenticated and self.session_lost_at is None
                and self.login_phase() == "done"):
            self.authenticated = True
            self.session = "ws-login"
            self.record("operator_login_completed")
            for page in self.pages[1:]:
                if self.kind(page._url) == "verify":
                    page._url = "https://www.xiaohongshu.com/explore"
        if (self.relogin_due is not None and not self.authenticated and self.clock >= self.relogin_due):
            self.authenticated = True
            self.session = "ws-relogin"
            self.record("operator_relogin_completed")
        if (self.captcha_started_at is not None and not self.verified and self.name == "captcha_pass"
                and self.clock >= self.captcha_started_at + 20):
            self.verified = True
            page = self.pages[0]
            page._url = self.captcha_redirect
            self.record("operator_captcha_completed")

    def login_phase(self) -> str:
        mode = self.scenario.login
        now = self.clock
        if self.login_started_at is None:
            return "qr"
        elapsed = now - self.login_started_at
        age = now - (self.qr_born_at if self.qr_born_at is not None else self.login_started_at)
        if mode == "scan":
            return "qr" if elapsed < 40 else "progress" if elapsed < 60 else "done"
        if mode == "scan_tab":
            return "qr" if elapsed < 20 else "progress" if elapsed < 50 else "done"
        if mode == "latched":
            if elapsed < 30:
                return "qr"
            if elapsed < 45:
                return "progress"
            return "qr_expired" if elapsed < 400 else "done"
        if mode == "qr_refresh":
            if self.qr_generation == 0:
                return "qr" if age < 120 else "qr_expired"
            if self.qr_generation == 1:
                return "qr"
            return "qr" if age < 30 else "progress" if age < 50 else "done"
        return "qr"

    def login_view(self, kind: str) -> View:
        if self.session_lost_at is not None:
            # 操作人从原页面首次出现二维码起 25 秒后完成重新登录。
            if self.relogin_due is None:
                self.relogin_due = self.clock + 25
                self.record("relogin_qr_visible")
            return View("扫码登录 打开小红书扫一扫 青岛旅游", frozenset({"qr"}), cards=8 if kind == "search" else 0,
                        login_control=True)
        phase = self.login_phase()
        if phase == "progress" and self.scenario.login != "scan_tab":
            return View("已扫码 请在手机上确认登录", frozenset(), login_control=True)
        if phase == "qr_expired":
            return View("扫码登录 二维码已过期 点击刷新", frozenset({"qr", "qr_refresh"}), login_control=True)
        return View("扫码登录 打开小红书扫一扫", frozenset({"qr", "login_button"}), login_control=True)

    def view(self, page: Page) -> View:
        self.tick()
        url = page._url
        kind = self.kind(url)
        if kind == "blank":
            return View("", rendered=False)
        if kind == "captcha":
            if self.name == "captcha_rate_limited":
                frame = ("Requests too frequent, please try again later", frozenset())
            else:
                frame = ("请拖动滑块完成验证", frozenset({"captcha_control"}))
            return View("小红书 安全中心", frozenset(), frames=(frame,))
        if kind == "verify":
            return View("请通过验证 安全验证 手机确认", frozenset({"captcha_control"}))
        if kind == "profile":
            user = urlsplit(url).path.rsplit("/", 1)[-1]
            html = creator_html(user, "88") if user != "u6" else "<html><body>作者不存在</body></html>"
            if self.name == "creator_page_security" and user == "u2":
                return View("安全限制 账号存在安全风险 300011", html="<html><body>安全限制</body></html>")
            if self.name == "creator_page_verification" and user == "u2" and self.clock < page.goto_at + 20:
                return View("请完成验证 拖动滑块", frozenset({"captcha_control"}), html="<html><body>验证</body></html>")
            return View("作者主页 粉丝 关注 我", frozenset({"profile"}), html=html)
        if kind == "note":
            if url.endswith("popup-ad"):
                return View("活动推荐 青岛周末")
            tokens = {"profile"}
            if self.name == "interaction_like":
                tokens.add("like")
            if self.name == "interaction_comment":
                tokens.add("comments")
            return View("青岛游记 详情 评论区 我", frozenset(tokens))
        if kind in {"explore", "search"}:
            navigations = page.navigations[kind]
            if self.name == "explore_blank_fail" and kind == "explore":
                return View("", rendered=False)
            if self.name == "explore_blank" and navigations < 2:
                return View("", rendered=False)
            if not self.authenticated:
                return self.login_view(kind)
            if kind == "search":
                if self.window is not None and self.window[0] <= self.clock < self.window[1]:
                    if self.name == "continuity_blocked":
                        return View("访问频繁 请稍后再试 青岛旅游", frozenset({"profile"}), cards=8)
                    return View("请完成验证 青岛旅游 笔记", frozenset({"captcha_control", "profile"}), cards=8)
                return View("青岛旅游 综合 最新 笔记 青岛作者一 我", frozenset({"profile"}), cards=8)
            return View("发现 首页 推荐 我", frozenset({"profile"}))
        return View("小红书")

    # -- 页面事件 --
    def before_goto(self, page: Page, url: str):
        kind = self.kind(url)
        if self.name == "main_page_closed" and kind == "search" and page is self.pages[0]:
            page.closed = True
            if page in self.context._pages:
                self.context._pages.remove(page)
            raise TargetClosedError("Target page, context or browser has been closed")

    def after_goto(self, page: Page, url: str):
        if self.name == "search_ready_verification" and self.kind(url) == "search" and self.window is None:
            # 行为前搜索就绪检查遇到验证：操作人 10 秒后完成。
            self.window = (self.clock, self.clock + 10)
        if self.kind(url) == "captcha":
            self.captcha_started_at = self.clock
            self.captcha_redirect = parse_qs(urlsplit(url).query)["redirectPath"][0]

    def on_click(self, page: Page, token: str):
        if token == "qr_refresh" and self.login_phase() == "qr_expired":
            self.qr_generation += 1
            self.qr_born_at = self.clock
            self.record("qr_component_refreshed", self.qr_generation)
        if token == "like":
            page.liked = True

    def on_reload(self, page: Page):
        if not self.authenticated:
            self.qr_generation += 1
            self.qr_born_at = self.clock
            self.record("qr_page_reloaded", self.qr_generation)

    def open_popup(self, url: str):
        page = Page(self, url)
        self.context._pages.append(page)
        self.record("popup", page.id, url)
        self.context.emit_page(page)

    def cookie_list(self):
        cookies = [
            {"name": "a1", "value": "device-a1", "domain": ".xiaohongshu.com"},
            {"name": "webId", "value": "web-id-1", "domain": ".xiaohongshu.com"},
            {"name": "web_session", "value": self.session, "domain": ".xiaohongshu.com"},
        ]
        if self.verified:
            cookies.append({"name": "verify_ok", "value": "1", "domain": ".xiaohongshu.com"})
        return cookies

    def lose_session(self):
        if self.session_lost_at is None:
            self.session_lost_at = self.clock
            self.authenticated = False
            self.session = "ws-expired"
            self.record("session_lost")

    # -- HTTP --
    async def http(self, method, url, kwargs):
        self.tick()
        parts = urlsplit(url)
        path = parts.path
        body = kwargs.get("data")
        headers = dict(kwargs.get("headers") or {})
        self.record("http", method, url, body, sorted(headers.items()),
                    sorted((key, value) for key, value in kwargs.items() if key not in {"data", "headers"}))
        self.counts[path] += 1
        request = httpx.Request(method, url)
        if self.cdp_lost:
            raise httpx.ConnectError("CDP 会话已断开", request=request)
        if self.outage(path):
            raise httpx.ConnectError("网络暂时中断", request=request)
        if path == "/api/sns/web/v1/user/selfinfo":
            return self.selfinfo(request)
        if path == "/api/sns/web/v1/search/notes":
            return self.search(request, json.loads(body), headers)
        if path == "/api/sns/web/v1/feed":
            return self.feed(request, json.loads(body))
        if path.startswith("/user/profile/"):
            return self.creator(request, path.rsplit("/", 1)[-1])
        if path.startswith("/explore/"):
            note_id = path.rsplit("/", 1)[-1]
            if self.name == "candidate_skips" and note_id == "n2":
                return httpx.Response(200, request=request, text="<html><body>暂时无法浏览</body></html>")
            return httpx.Response(200, request=request, text=note_html(note_id))
        if parts.netloc.endswith("xhscdn.com"):
            if self.name == "repair" and "/r4-0-" in path:
                return httpx.Response(404, request=request, content=b"")
            if self.name == "image_blocking":
                return httpx.Response(403, request=request, content=b"")
            if self.name == "candidate_skips" and "/n1-1-" in path:
                return httpx.Response(503, request=request, content=b"")
            return httpx.Response(200, request=request, content=png_bytes(url))
        raise AssertionError(f"未预期的请求：{method} {url}")

    def outage(self, path: str) -> bool:
        if self.name == "network_timeout" and path.endswith("/search/notes"):
            return True
        if self.name != "network_recover":
            return False
        windows = {"/api/sns/web/v1/search/notes": 300.0, "/api/sns/web/v1/feed": 400.0}
        if path not in windows:
            return False
        started = self.outages.setdefault(path, self.clock)
        return self.clock < started + windows[path]

    def selfinfo(self, request):
        if self.name == "main_page_closed" and not self.popup_opened:
            self.popup_opened = True
            self.open_popup("https://www.xiaohongshu.com/explore/popup-ad")
        if self.name == "selfinfo_verification":
            return httpx.Response(461, request=request, json={"code": 461}, headers={"Verifytype": "102"})
        if not self.authenticated and self.login_started_at is None and self.session_lost_at is None:
            self.login_started_at = self.clock
            self.record("login_started")
        if self.authenticated:
            payload = {"code": 0, "success": True, "msg": "成功", "data": {"result": {"success": True}, "user_id": "self"}}
        else:
            payload = {"code": -101, "success": False, "msg": "未登录", "data": {"result": {"success": False}}}
        return httpx.Response(200, request=request, json=payload)

    def search(self, request, payload, headers):
        number = self.counts["/api/sns/web/v1/search/notes"]
        if self.name == "security_300011":
            return httpx.Response(200, request=request, json={"code": 300011, "success": False, "msg": "账号存在安全风险"})
        if self.name.startswith("captcha") and "verify_ok=1" not in headers.get("Cookie", ""):
            status = 471 if self.name == "captcha_rate_limited" else 461
            return httpx.Response(status, request=request, json={"code": status, "success": False},
                                  headers={"Verifyuuid": f"uuid-{number}", "Verifytype": "102"})
        if self.name == "login_expired":
            if number == 1:
                self.lose_session()
            if "web_session=ws-relogin" not in headers.get("Cookie", ""):
                return httpx.Response(200, request=request, json={"code": -100, "success": False, "msg": "登录已过期"})
        if self.name == "search_429":
            return httpx.Response(429, request=request, json={"code": 429})
        if self.name in {"continuity_verification", "continuity_blocked"} and self.window is None:
            # 搜索结果后的连续性行为遇到页面验证/频控：窗口持续 40 秒。
            self.window = (self.clock, self.clock + 40)
        page = payload["page"]
        for number_page, items, has_more in self.scenario.pages:
            if number_page == page:
                data = {"has_more": has_more, "items": [search_item(item) for item in items]}
                return httpx.Response(200, request=request, json={"code": 0, "success": True, "data": data})
        return httpx.Response(200, request=request, json={"code": 0, "success": True, "data": {"has_more": False, "items": []}})

    def feed(self, request, payload):
        note_id = payload["source_note_id"]
        if self.name == "success" and not self.popup_opened:
            self.popup_opened = True
            self.open_popup("https://www.xiaohongshu.com/explore/popup-ad")
        if self.name == "cdp_disconnect":
            self.cdp_lost = True
            raise httpx.ConnectError("CDP 会话已断开", request=request)
        if self.name.startswith("repair") and note_id == "r2":
            if self.name == "repair_ip_blocked":
                return httpx.Response(200, request=request, json={"code": 300012, "success": False, "msg": "网络异常"})
            return httpx.Response(200, request=request, json={"code": -510000, "success": False, "msg": "笔记不存在"})
        if self.name == "repair_300011" and note_id == "r5":
            return httpx.Response(200, request=request, json={"code": 300011, "success": False, "msg": "安全限制"})
        if note_id == "n2":
            return httpx.Response(200, request=request, json={"code": 0, "success": True, "data": {"items": []}})
        items = [{"id": note_id, "model_type": "note", "note_card": NOTES[note_id]}]
        return httpx.Response(200, request=request, json={"code": 0, "success": True, "data": {"items": items}})

    def creator(self, request, user):
        if self.name == "creator_300012" and user == "u1":
            return httpx.Response(200, request=request, json={"code": 300012, "success": False, "msg": "网络异常"})
        if self.name == "creator_primary_login" and user == "u2":
            if self.session_lost_at is None:
                self.lose_session()
                return httpx.Response(200, request=request, text="<html><body>请稍后再试</body></html>")
            return httpx.Response(200, request=request, text=creator_html(user, "321"))
        if user in {"u2", "u6"}:
            return httpx.Response(200, request=request, text="<html><body>请稍后再试</body></html>")
        return httpx.Response(200, request=request, text=creator_html(user, "1234"))


# ---------------------------------------------------------------------------
# 边界安装与驱动


def install_boundaries(patch, world: World) -> None:
    patch.setattr(sys, "path", list(sys.path))
    patch.setattr(asyncio.base_events.BaseEventLoop, "time", lambda self: REAL_MONOTONIC())
    patch.setattr(time, "monotonic", world.monotonic)
    patch.setattr(time, "time", world.wall)
    patch.setattr(asyncio, "sleep", world.sleep)
    # tenacity 的 AsyncRetrying 在装饰时捕获默认 sleep；保留 stop/wait/retry 条件，只截获等待。
    patch.setattr(tenacity._asyncio.AsyncRetrying.__init__, "__defaults__", (world.sleep,))
    patch.setattr(runtime_behavior, "REQUEST_RANDOM", random.Random(SEED + 1))
    patch.setattr(human_flow, "RANDOM", random.Random(SEED + 2))
    patch.setattr(events, "_batch_publisher", events._batch_publisher)

    class FakeCDPBrowserManager:
        def __init__(self, settings=None, *, project_browser_args=None):
            world.manager = self
            world.record(
                "cdp_manager",
                dataclasses.asdict(settings) if dataclasses.is_dataclass(settings) else repr(settings),
                project_browser_args() if project_browser_args else None,
            )

        async def launch_and_connect(self, playwright=None, playwright_proxy=None, user_agent=None, headless=False):
            world.record("launch", playwright is world.playwright, playwright_proxy, user_agent, headless)
            world.counts["launch"] += 1
            if world.name in {"cdp_launch_failed", "cdp_concurrent_session"}:
                await REAL_SLEEP(0)
                raise RuntimeError("Chrome 进程启动后立即退出")
            page = Page(world, "about:blank")
            world.context._pages.append(page)
            return world.context

        async def get_browser_info(self):
            world.record("browser_info")
            return {"version": "Chrome/138.0"}

        async def add_stealth_script(self, script_path=None):
            world.record("stealth", script_path)

        def assert_alive(self, stage):
            world.record("assert_alive", stage)
            if world.cdp_lost:
                raise runtime_browser.CDPBrowserLifecycleError(
                    {"code": "xhs_cdp_disconnected_unexpected", "detail": "websocket closed"}, stage=stage,
                )

        def mark_planned_cleanup(self, reason):
            world.record("planned_cleanup", reason)

        async def cleanup(self, force=False):
            world.record("cleanup", force)
            return {"status": "completed", "context": "closed", "browser": "closed",
                    "process": {"status": "exited"}, "errors": []}

    class PlaywrightManager:
        async def __aenter__(self):
            world.record("playwright_enter")
            return world.playwright

        async def __aexit__(self, *args):
            world.record("playwright_exit")
            return False

    world.playwright = SimpleNamespace(chromium=None)
    patch.setattr(runtime_browser, "CDPBrowserManager", FakeCDPBrowserManager)
    import playwright.async_api

    patch.setattr(playwright.async_api, "async_playwright", PlaywrightManager)
    patch.setattr(runtime_http, "make_async_client", lambda **kwargs: AsyncClient(world, kwargs))

    class FakeXhshow:
        def __init__(self):
            world.record("xhshow")

        def _sign(self, method, uri, cookies, data):
            digest = sha256(json.dumps([method, uri, cookies, data], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            world.record("sign", method, uri, cookies, data)
            return {"x-s": "XYS_" + digest[:24], "x-t": str(int(time.time() * 1000)), "x-s-common": "XSC_" + digest[24:40]}

        def sign_headers_post(self, uri, cookies, payload):
            return self._sign("POST", uri, cookies, payload)

        def sign_headers_get(self, uri, cookies, params):
            return self._sign("GET", uri, cookies, params)

    import xhshow

    patch.setattr(xhshow, "Xhshow", FakeXhshow)

    async def run_page_behavior(page, *, platform_key, evidence_path, profile_name="social_high_risk",
                                write_evidence, xhs_search_ready=None):
        # 通用行为阶段不属于本卡；只真实调用注入的 XHS 搜索就绪等待并写合格证据。
        world.record("human_behavior", page.id, platform_key, profile_name, str(evidence_path),
                     getattr(xhs_search_ready, "__name__", None))
        world.meta["ready_modules"].append(getattr(xhs_search_ready, "__module__", None))
        readiness_events: list = []
        readiness = await xhs_search_ready(page, readiness_events)
        evidence = {
            "schema_version": 1, "platform": platform_key, "phase": "pre_search_human_behavior",
            "profile": profile_name, "status": "completed" if readiness.get("ready") else "failed",
            "url": page.url,
            "events": [*readiness_events, {"event": "pause", "seconds": 1.0}, {"event": "mouse_moves", "count": 1},
                       {"event": "human_scroll_complete", "intent": "list", "passes": 1, "effective_passes": 1}],
            "runtime_fingerprint": {"webdriver": None, "languages": ["zh-CN"], "platform": "MacIntel",
                                    "user_agent": UA, "visibility_state": "visible",
                                    "viewport": {"width": 1280, "height": 800}},
            "page_readiness": readiness,
            "operator_verification_events": readiness.get("operator_verification_events") or [],
            "initial_visible_markers": {}, "visible_markers": {}, "challenge": "", "error": "",
        }
        write_evidence(evidence_path, evidence)
        if not readiness.get("ready"):
            raise RuntimeError(f"human_behavior_failed:{platform_key}:xhs_page_not_ready:{readiness.get('reason')}")
        return evidence

    patch.setattr(runtime_behavior, "run_page_behavior", run_page_behavior)


def golden_argv() -> list[str]:
    commands = json.loads((ROOT / "tests/golden/t02_worker_commands.json").read_text())
    return list(commands["xhs_search_qrcode"]["cmd"][4:])


def scenario_argv(scenario: Scenario, out: Path) -> list[str]:
    argv = golden_argv()
    values = dict(zip(argv[0::2], argv[1::2]))
    values["--save_data_path"] = str(out / "data")
    values["--start"] = str(scenario.start_page)
    values["--type"] = scenario.crawler_type
    result = [item for pair in values.items() for item in pair]
    if scenario.detail_urls:
        result += ["--specified_id", ",".join(scenario.detail_urls)]
    return result


def scenario_env(scenario: Scenario, out: Path) -> dict[str, str]:
    commands = json.loads((ROOT / "tests/golden/t02_worker_commands.json").read_text())
    env = {
        key: value.replace("<TMP>/xhs_search_qrcode", str(out)).replace("<TMP>", str(out)).replace("<ROOT>", str(ROOT))
        for key, value in commands["xhs_search_qrcode"]["extra_env"].items()
    }
    env.update({
        "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE": "xhs_guarded",
        "TRIPPOSTCOLLECT_EXECUTION_STATE_PATH": str(out / "state.json"),
        "TRIPPOSTCOLLECT_DB_PATH": str(out / "content.sqlite"),
        "TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH": str(out / "resume.json"),
        "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES": str(scenario.refresh_pages),
        "TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR": scenario.resume_cursor,
        "TRIPPOSTCOLLECT_DISCOVERY_RESUME_PAGE": str(scenario.start_page),
        "TRIPPOSTCOLLECT_XHS_BATCH_CHECKPOINT_ENABLED": "0",
        "TRIPPOSTCOLLECT_XHS_REPAIR": "1" if scenario.crawler_type == "detail" else "0",
        "TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS": "600",
    })
    env.update(dict(scenario.env))
    return env


def prepare_inputs(out: Path) -> None:
    out.mkdir(parents=True)
    (out / "state.json").write_text('{"events": []}\n')
    (out / "resume.json").write_text('["n-resume"]\n')
    (out / "xhs-profile").mkdir()
    with sqlite3.connect(out / "content.sqlite") as conn:
        conn.execute("CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)")
        conn.execute("CREATE TABLE xhs_discovery_seen_candidates "
                     "(target_key TEXT, account_id TEXT, query_fingerprint TEXT, platform_post_id TEXT)")
        conn.execute("INSERT INTO web_posts VALUES ('xhs', 'n-db', 'https://www.xiaohongshu.com/explore/n-db')")
        conn.executemany("INSERT INTO xhs_discovery_seen_candidates VALUES (?, ?, ?, ?)", [
            ("target", "xhs-a01", "fp", "n-seen"),
            # 其他账号的 seen 不得过滤本账号候选。
            ("target", "xhs-b02", "fp", "n2"),
        ])


async def capture(awaitable):
    try:
        await awaitable
    except BaseException as exc:  # noqa: BLE001 - 记录两侧原样异常
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return {"type": type(exc).__name__, "message": str(exc), "code": getattr(exc, "code", None)}
    return None


async def run_scenario(side, scenario_name: str, workdir: Path, patch) -> dict:
    scenario = SCENARIOS[scenario_name]
    out = workdir / side.label
    prepare_inputs(out)
    world = World(scenario, out)
    install_boundaries(patch, world)
    for name in list(os.environ):
        if name.startswith("TRIPPOSTCOLLECT_"):
            patch.delenv(name)
    for name, value in scenario_env(scenario, out).items():
        patch.setenv(name, value)
    mods = side.install(patch, workdir)
    config = importlib.import_module("config")
    for name in dir(config):
        if name.isupper():
            patch.setattr(config, name, getattr(config, name))
    argv = scenario_argv(scenario, out)
    mods.entry.configure(argv)
    # T12：新入口只写根配置对象；旧桥两侧仍读 fork config，按原 configure 语义同步写回。
    from trippostcollect.application import worker_inputs
    worker_inputs.apply_to_config(worker_inputs.parse_cmd(argv), config)
    # fork config 在本进程可能早已导入；按新进程首次导入的语义从当前 env 重读这两个键。
    patch.setattr(config, "COOKIES", os.environ.get("TRIPPOSTCOLLECT_COOKIES", ""))
    patch.setattr(config, "CUSTOM_BROWSER_PATH", os.environ.get("TRIPPOSTCOLLECT_CUSTOM_BROWSER_PATH")
                  or os.environ.get("CUSTOM_BROWSER_PATH", ""))
    side.install_hooks(mods)

    def publish(details):
        payload = json.loads((out / "state.json").read_text())
        last = payload["events"][-1]
        world.meta["publishes"] += 1
        world.record("publish", len(payload["events"]), last["type"], last["details"] == details,
                     details.get("batch_no"), details.get("source_page"))
        if scenario.name == "batch_publish_failure":
            raise RuntimeError("xhs_batch_checkpoint_ack_timeout")

    events.configure_batch_checkpoint(publish)
    budget_class = mods.manual_wait.XHSManualWaitBudget
    original_init = budget_class.__init__

    def counted_init(self, *args, **kwargs):
        world.meta["budget_inits"] += 1
        original_init(self, *args, **kwargs)

    patch.setattr(budget_class, "__init__", counted_init)
    login_class = mods.login.XiaoHongShuLogin
    original_begin = login_class.begin

    async def observed_begin(self):
        world.meta["login_budgets"].append(self._manual_wait_budget)
        return await original_begin(self)

    patch.setattr(login_class, "begin", observed_begin)
    crawler_class = side.crawler_class(mods)
    random.seed(SEED)
    crawler = crawler_class()
    outcome = {}
    if scenario.concurrent_session:
        results = await asyncio.gather(
            crawler._run_browser_session(world.playwright, None, None),
            crawler._run_browser_session(world.playwright, None, None),
            return_exceptions=True,
        )
        outcome["sessions"] = [
            None if item is None else {"type": type(item).__name__, "message": str(item)} for item in results
        ]
    else:
        outcome["start"] = await capture(crawler.start())
        if scenario.second_start:
            outcome["second_start"] = await capture(crawler.start())
    outcome["close"] = await capture(crawler.close())
    budget = getattr(crawler, "_manual_wait_budget", None)
    client = getattr(crawler, "xhs_client", None)
    state = {
        "session_started": crawler._browser_session_started,
        "cdp_manager_cleared": crawler.cdp_manager is None,
        "budget_elapsed": None if budget is None else round(budget.manual_elapsed_seconds, 3),
        "budget_inits": world.meta["budget_inits"],
        "client_shares_budget": None if client is None else client._manual_wait_budget is budget,
        "login_shares_budget": [item is budget for item in world.meta["login_budgets"]],
        "context_page": getattr(getattr(crawler, "context_page", None), "id", None),
        "creator_cache": sorted(getattr(crawler, "creator_profile_cache", {}) or {}),
        "launches": world.counts["launch"],
        "pages": [(page.id, page._url, page.closed) for page in world.pages],
    }
    files = {}
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.suffix not in {".sqlite"} and path.name != "resume.json":
            files[str(path.relative_to(out))] = path.read_bytes()
    return {"trace": world.trace, "outcome": outcome, "state": state, "files": files,
            "ready_modules": world.meta["ready_modules"], "out": str(out), "world": world,
            "crawler": crawler}


def normalized(result: dict) -> dict:
    out = result["out"]

    def clean(text: str) -> str:
        return ADDRESS_RE.sub("0x?", TS_RE.sub("<TS>", text.replace(out, "<OUT>")))

    files = {}
    for name, data in result["files"].items():
        key = DATE_RE.sub("<DATE>", name)
        if name.startswith("data/"):
            # JSONL、manifest 与 staging 图片逐字节比较，只替换两侧不同的输出根目录。
            files[key] = data.replace(out.encode(), b"<OUT>")
        else:
            files[key] = clean(data.decode("utf-8"))
    payload = json.loads(clean(json.dumps(
        {"trace": result["trace"], "outcome": result["outcome"], "state": result["state"]},
        ensure_ascii=False, default=repr,
    )))
    payload["files"] = files
    return payload


def assert_equivalent(old: dict, new: dict) -> None:
    left, right = normalized(old), normalized(new)
    old_trace, new_trace = left.pop("trace"), right.pop("trace")
    for index, (a, b) in enumerate(zip(old_trace, new_trace)):
        if a != b:
            window = (old_trace[max(0, index - 3):index + 3], new_trace[max(0, index - 3):index + 3])
            raise AssertionError(f"边界事件序列第 {index} 项不同：\n旧 {window[0]}\n新 {window[1]}")
    assert len(old_trace) == len(new_trace), f"边界事件数量不同：旧 {len(old_trace)} 新 {len(new_trace)}"
    old_files, new_files = left.pop("files"), right.pop("files")
    assert sorted(old_files) == sorted(new_files), "产物文件集合不同"
    for name in old_files:
        assert old_files[name] == new_files[name], f"产物 {name} 内容不同"
    assert left == right


# ---------------------------------------------------------------------------
# 显式断言


def ops(result, kind, *prefix):
    return [item for item in result["trace"] if item[1] == kind and tuple(item[2:2 + len(prefix)]) == prefix]


def first_index(result, predicate) -> int:
    return next(index for index, item in enumerate(result["trace"]) if predicate(item))


def execution_events(result) -> list[dict]:
    return json.loads(result["files"]["state.json"])["events"]


def stop_event(result) -> dict:
    stops = [event for event in execution_events(result) if event["type"] == "adaptive_search_stopped"]
    assert len(stops) == 1, [event["type"] for event in execution_events(result)]
    return stops[0]["details"]


def jsonl_records(result) -> list[dict]:
    records = []
    for name, data in result["files"].items():
        if "/jsonl/" in name:
            records += [json.loads(line) for line in data.decode().splitlines()]
    return records


def navigation_events(result) -> list[dict]:
    name = "logs/xhs/behavior_evidence.navigation.json"
    return json.loads(result["files"][name])["events"] if name in result["files"] else []


def check_common(result, scenario: Scenario) -> None:
    state = result["state"]
    # 唯一浏览器：整轮只有一次 launch、一个 BrowserContext。
    assert state["launches"] <= 1
    assert len(ops(result, "cdp_manager")) <= 1
    assert state["session_started"] is True
    # 600 秒共享人工预算：全轮只构造一个实例，login/client 借用同一对象。
    assert state["budget_inits"] <= 1
    assert all(state["login_shares_budget"])
    if state["client_shares_budget"] is not None:
        assert state["client_shares_budget"] is True
    if state["budget_elapsed"] is not None:
        assert state["budget_elapsed"] <= 600.0 + 1e-6
    # 任何新标签页：立即置前，并且任何主动关闭都不早于出现后 30 秒。
    opened = {}
    for item in result["trace"]:
        if item[1] in {"new_page", "popup"}:
            opened[item[2]] = item[0]
    for page_id, opened_at in opened.items():
        fronts = [item for item in ops(result, "bring_to_front", page_id)]
        assert fronts and fronts[0][0] == opened_at, page_id
        for close in ops(result, "close", page_id):
            assert close[0] - opened_at >= 30.0 - 1e-6, (page_id, close[0], opened_at)
    assert len(ops(result, "context_on")) <= 1
    # 平台弹出的标签页只受新页守卫管理，不被导航或接管为主页面。
    for item in ops(result, "popup"):
        assert not ops(result, "goto", item[2]) or scenario.login == "scan_tab"
    if state["context_page"] is not None and scenario.login != "scan_tab":
        assert state["context_page"] == "p0"
    # 计划内关闭证据：Playwright 退出前先标记 planned cleanup。
    if state["launches"] == 1 and "start" in result["outcome"] and result["outcome"]["start"] is None:
        names = [item[1] for item in result["trace"]]
        assert names.index("planned_cleanup") < names.index("playwright_exit")
        assert ops(result, "planned_cleanup")[0][2] == "playwright_context_exit"
    # 批次发布出口只在 adaptive_batch_completed 持久化后调用；其他事件（含登录终态）不经发布。
    batch_events = [event for event in execution_events(result) if event["type"] == "adaptive_batch_completed"]
    publishes = ops(result, "publish")
    assert len(publishes) == len(batch_events)
    assert all(item[3] == "adaptive_batch_completed" and item[4] for item in publishes)
    # 头像不得写入任何项目产物。
    for name, data in result["files"].items():
        if not name.endswith(".png"):
            assert b"sns-avatar" not in data, name
    # 签名：每次签名新建一个 Xhshow；API 请求都带完整签名头与 trace id。
    assert len(ops(result, "xhshow")) == len(ops(result, "sign"))
    for item in ops(result, "http"):
        headers = dict(item[5])
        if "edith.xiaohongshu.com" in item[3]:
            assert headers["X-S"].startswith("XYS_") and headers["x-S-Common"].startswith("XSC_")
            assert re.fullmatch(r"[0-9a-f]{16}", headers["X-B3-Traceid"])
            assert headers["X-T"].isdigit()


def check_success(result) -> None:
    trace = result["trace"]
    # /explore 预热先于任何搜索页；同一主页面完成预热、登录与搜索。
    gotos = ops(result, "goto")
    assert gotos[0][2:4] == ("p0", "https://www.xiaohongshu.com/explore")
    search_url = "https://www.xiaohongshu.com/search_result?keyword=" + quote("青岛旅游")
    assert ("p0", search_url) in [item[2:4] for item in gotos]
    # 扫码进入人工处理中后锁存：不再整页 reload 或点击二维码组件。
    assert not ops(result, "reload") and not ops(result, "qr_component_refreshed")
    # 只有未知笔记进入详情：数据库、账号 seen、累计摘要与本轮已见 ID 在详情前过滤。
    feeds = [json.loads(item[4])["source_note_id"] for item in ops(result, "http")
             if item[3].endswith("/api/sns/web/v1/feed")]
    assert feeds == ["n1", "n2", "n3", "n4"]
    searches = [json.loads(item[4]) for item in ops(result, "http") if item[3].endswith("/search/notes")]
    assert [(item["page"], item["search_id"] == "2fsavedsearchid") for item in searches] == [
        (1, False), (2, True), (3, True),
    ]
    # 详情 API 为空时回退 HTML，作者补全先走无 token API，再暂停、检查原页、打开作者页。
    html_detail = [item for item in ops(result, "http") if "/explore/n2?" in item[3]]
    assert len(html_detail) == 1
    creator_api = first_index(result, lambda item: item[1] == "http" and item[3].endswith("/user/profile/u2"))
    pause = first_index(result, lambda item: item[1] == "sleep" and item[0] >= trace[creator_api][0] and item[2] >= 12.0)
    author_page = next(item[2] for item in trace if item[1] == "new_page")
    original_check = first_index(result, lambda item: item[1] == "text" and item[2] == "p0" and item[0] >= trace[pause][0])
    author_goto = first_index(result, lambda item: item[1] == "goto" and item[2] == author_page)
    assert creator_api < pause < original_check < author_goto
    assert trace[author_goto][3] == "https://www.xiaohongshu.com/user/profile/u2"
    assert not [item for item in ops(result, "http") if item[3].endswith("/user/profile/u1")][1:], "作者缓存"
    # 正文图只取 url_default>url>url_pre，每个对象一个 URL。
    image_requests = [item for item in ops(result, "http") if "xhscdn.com" in item[3]]
    # 现状：正文图经短会话 GET，不带本轮 Cookie/headers（与文档“复用 Cookie”不一致，保持现状）。
    assert all(item[5] == [] and item[6] == [("timeout", 60)] for item in image_requests)
    images = [item[3] for item in image_requests]
    assert [url.rsplit("/", 1)[-1].split("!")[0] for url in images if "/n1/" in url] == [
        "n1-0-url_default", "n1-1-url", "n1-2-url_pre",
    ]
    records = jsonl_records(result)
    assert [record["note_id"] for record in records] == ["n1", "n2", "n4"]
    assert all(record["followers_observed"] is True and record["author_followers_source"] == "creator_profile"
               for record in records)
    assert all(record["content_detail_status"] == "detail_observed" for record in records)
    details = stop_event(result)
    assert details["stop_reason"] == "source_exhausted"
    # 登录后 API Cookie 已刷新为本轮登录会话。
    for item in ops(result, "http"):
        if item[3].endswith(("/search/notes", "/feed")):
            assert "web_session=ws-login" in dict(item[5])["Cookie"]
    # 批次发布只发生在 adaptive_batch_completed 已持久化之后。
    publishes = ops(result, "publish")
    assert publishes and all(item[3] == "adaptive_batch_completed" and item[4] for item in publishes)


def check_qr_refresh(result) -> None:
    trace = result["trace"]
    clicks = [item for item in ops(result, "click") if selector_token(item[4]) == "qr_refresh"]
    assert len(clicks) == 1
    click_index = trace.index(clicks[0])
    # 组件刷新前必须连续两次观察到纯过期二维码（两次二维码过期文本读取之间没有等待）。
    reads_before = [index for index, item in enumerate(trace[:click_index]) if item[1] == "text" and item[2] == "p0"]
    last_sleep = max(index for index, item in enumerate(trace[:click_index]) if item[1] == "sleep")
    assert len([index for index in reads_before if index > last_sleep]) >= 2
    reloads = ops(result, "reload")
    assert len(reloads) == 1
    # 新二维码出现后重新计算 180 秒整页 reload 下限。
    refreshed_at = ops(result, "qr_component_refreshed")[0][0]
    assert reloads[0][0] - refreshed_at >= 180.0
    assert reloads[0][3:] == ("domcontentloaded", 30000)


def check_latched(result) -> None:
    assert not ops(result, "reload")
    assert not [item for item in ops(result, "click") if selector_token(item[4]) == "qr_refresh"]
    assert ops(result, "operator_login_completed")


def check_budget_exhausted(result) -> None:
    reloads = [item[0] for item in ops(result, "reload")]
    login_started = ops(result, "login_started")[0][0]
    # 未扫码二维码页面至少等待 180 秒才 reload；环境值只能上调下限。
    assert reloads and reloads[0] - login_started >= 180.0
    assert all(b - a >= 180.0 for a, b in zip(reloads, reloads[1:]))
    terminal = [event for event in execution_events(result) if event["type"] == "xhs_runtime_terminal"]
    assert terminal and terminal[-1]["details"]["failure_type"] == "manual_checkpoint_timeout"
    assert terminal[-1]["details"]["stop_detail"] == "xhs_manual_checkpoint_budget_exhausted"
    assert result["state"]["budget_elapsed"] == pytest.approx(600.0, abs=1.5)


def check_captcha_pass(result) -> None:
    gotos = [item for item in ops(result, "goto") if "/website-login/captcha" in item[3]]
    assert len(gotos) == 1 and gotos[0][2] == "p0"
    query = parse_qs(urlsplit(gotos[0][3]).query)
    assert query["verifyUuid"] == ["uuid-1"] and query["verifyType"] == ["102"] and query["verifyBiz"] == ["461"]
    evidence = json.loads(result["files"]["logs/xhs/behavior_evidence.json"])
    verification = evidence["operator_verification_events"][-1]
    # 顶层页与子 frame 都检查；回到原路由后连续两次就绪观察才完成。
    assert verification["status"] == "completed" and verification["ready_observations"] == 2
    frame_reads = [item for item in ops(result, "text", "p0") if item[3] == 1]
    assert frame_reads
    searches = [item for item in ops(result, "http") if item[3].endswith("/search/notes")]
    # 验证完成后刷新 Cookie 并重试原请求。
    assert "verify_ok=1" in dict(searches[1][5])["Cookie"]
    assert stop_event(result)["stop_reason"] == "source_exhausted"
    assert not ops(result, "new_page")


def check_captcha_rate_limited(result) -> None:
    # 可见频控优先于验证页分类：不点击、不刷新，按运行级失败停止。
    assert not ops(result, "click") and not ops(result, "reload")
    evidence = json.loads(result["files"]["logs/xhs/behavior_evidence.json"])
    assert evidence["challenge"] == "rate_limited"
    assert evidence["operator_verification_events"][0]["verify_biz"] == 471
    assert evidence["operator_verification_events"][0]["error"] == "rate_limited_during_operator_verification"
    assert stop_event(result)["stop_reason"] == "runtime_failed"


def check_captcha_timeout(result) -> None:
    login_done = ops(result, "operator_login_completed")[0][0]
    captcha = [item[0] for item in ops(result, "goto") if "/website-login/captcha" in item[3]]
    assert len(captcha) == 1
    details = stop_event(result)
    assert details["stop_reason"] == "runtime_failed"
    assert details["stop_detail"] == "xhs_manual_checkpoint_budget_exhausted"
    # 验证码等待只能使用登录后剩余的共享人工预算。
    assert result["state"]["budget_elapsed"] == pytest.approx(600.0, abs=2.5)
    assert ops(result, "planned_cleanup")[-1][0] - captcha[0] < 600.0 - (login_done - 40.0)


def check_login_expired(result) -> None:
    trace = result["trace"]
    lost = first_index(result, lambda item: item[1] == "session_lost")
    relogin = first_index(result, lambda item: item[1] == "operator_relogin_completed")
    # 登录过期：暂停原请求，在原抓取页等待；不新开页、不导航、不刷新原页。
    assert not ops(result, "new_page") and not ops(result, "reload")
    assert not [item for item in trace[lost:relogin] if item[1] == "goto"]
    fronts = [item for item in trace[lost:relogin] if item[1] == "bring_to_front"]
    assert fronts and {item[2] for item in fronts} == {"p0"}
    after = [item for item in trace[relogin:] if item[1] == "goto"]
    assert after[0][2] == "p0" and "/search_result?keyword=" in after[0][3]
    probe = first_index(result, lambda item: item[1] == "http" and item[3].endswith("/user/selfinfo")
                        and item[0] >= trace[relogin][0])
    window = trace[relogin:probe]
    assert len([item for item in window if item[1] == "text" and item[2] == "p0" and item[3] == 0]) >= 2
    assert [item for item in window if item[1] == "sleep"]
    # 恢复后刷新内存 Cookie 并重试同一来源页。
    retried = [json.loads(item[4]) for item in trace[relogin:] if item[1] == "http" and item[3].endswith("/search/notes")]
    assert retried[0]["page"] == 1
    assert stop_event(result)["stop_reason"] == "source_exhausted"


def check_creator_primary_login(result) -> None:
    # 原抓取页已出现登录时直接在原页等待，不创建作者辅助页。
    assert not ops(result, "new_page")
    calls = [item for item in ops(result, "http") if item[3].endswith("/user/profile/u2")]
    assert len(calls) == 2 and "web_session=ws-relogin" in dict(calls[1][5])["Cookie"]
    assert [record["note_id"] for record in jsonl_records(result)] == ["n2"]


def pause_delays(result) -> list[list[float]]:
    """网络暂停退避：每次 network_paused 诊断写出后紧接的等待；按操作分组。"""
    trace = result["trace"]
    groups: list[list[float]] = []
    for index in range(1, len(trace) - 1):
        # 暂停等待前写 network_paused 诊断，等待后立即复核同一浏览器会话仍存活。
        if (trace[index][1] == "sleep" and trace[index - 1][1:4] == ("evaluate", "p0", "render")
                and trace[index + 1][1] == "assert_alive"):
            if not groups or trace[index][2] == 2.0 and groups[-1] and groups[-1][-1] != 2.0 and len(groups[-1]) > 1:
                groups.append([])
            groups[-1].append(trace[index][2])
    return groups


def check_network_recover(result) -> None:
    # 导航诊断只保留最近 30 条，此处只核对两次恢复都已记录。
    recovered = [event for event in navigation_events(result) if event["outcome"] == "network_recovered"]
    assert [event["stage"].split(":")[0] for event in recovered] == ["search", "note_detail_api"]
    groups = pause_delays(result)
    assert len(groups) == 2
    # 退避 min(30, 2*2^(n-1), 剩余)；600 秒预算按每次操作分别计算（两段中断合计超过 600 秒仍恢复）。
    for group in groups:
        assert group[:5] == [2.0, 4.0, 8.0, 16.0, 30.0]
        assert all(value == 30.0 for value in group[5:])
    assert sum(groups[0]) + sum(groups[1]) > 600.0
    # 网络暂停不消耗人工预算，也不重启浏览器或开新页。
    assert result["state"]["budget_elapsed"] == 0.0
    assert result["state"]["launches"] == 1 and not ops(result, "new_page")
    assert stop_event(result)["stop_reason"] == "source_exhausted"


def check_network_timeout(result) -> None:
    details = stop_event(result)
    assert details["stop_reason"] == "runtime_failed"
    assert details["stop_detail"] == "network_recovery_timeout"
    timeout = [event for event in navigation_events(result) if event["outcome"] == "network_recovery_timeout"]
    assert len(timeout) == 1
    (group,) = pause_delays(result)
    assert group[:5] == [2.0, 4.0, 8.0, 16.0, 30.0] and max(group) == 30.0
    requests = [item[0] for item in ops(result, "http") if item[3].endswith("/search/notes")]
    # 首轮 tenacity 失败后开始计时，耗尽 600 秒即停止，不再新开轮次。
    assert 600.0 - 30.0 <= requests[-1] - (requests[0] + 2.0) <= 600.0 + 2.0
    assert not ops(result, "new_page") and result["state"]["launches"] == 1


def check_security(result, detail: str, path: str) -> None:
    details = stop_event(result)
    assert details["stop_reason"] == "runtime_failed" and details["stop_detail"] == detail
    # 运行级阻断不被 tenacity 或网络恢复重试。
    assert len([item for item in ops(result, "http") if item[3].split("?")[0].endswith(path)]) == 1


def check_main_page_closed(result) -> None:
    start = result["outcome"]["start"]
    assert start["type"] == "RuntimeError"
    assert start["message"] == "xhs_main_page_closed_unexpected:stage=search_navigation"
    popup = ops(result, "popup")[0][2]
    assert not ops(result, "goto", popup)
    assert result["state"]["launches"] == 1


def check_cdp_disconnect(result) -> None:
    details = stop_event(result)
    assert details["stop_reason"] == "runtime_failed" and details["stop_detail"] == "cdp_disconnected"
    assert result["state"]["launches"] == 1


def check_launch_failed(result) -> None:
    outcome = result["outcome"]
    assert outcome["start"]["message"].startswith("xhs_cdp_browser_launch_failed:RuntimeError:")
    assert outcome["second_start"]["message"] == "xhs_browser_session_already_started"
    assert result["state"]["launches"] == 1 and result["state"]["cdp_manager_cleared"] is True


def check_concurrent(result) -> None:
    sessions = result["outcome"]["sessions"]
    assert result["state"]["launches"] == 1
    assert sorted(item["message"].split(":")[0] for item in sessions) == [
        "xhs_browser_session_already_started", "xhs_cdp_browser_launch_failed",
    ]


def check_interaction(result, action: str) -> None:
    interactions = json.loads(result["files"]["logs/xhs/behavior_evidence.json"])["post_interactions"]
    assert len(interactions) == 1 and interactions[0]["selected_mode"] == action
    assert interactions[0]["status"] == "completed"


def check_repair(result) -> None:
    report = json.loads(result["files"]["logs/xhs/repair_report.json"])
    assert report["batch_size"] == 5 and report["batch_count"] == 2
    assert [batch["target_count"] for batch in report["batches"]] == [5, 2]
    assert report["successful_ids"] == ["r1", "r5", "r7"]
    assert {item["platform_post_id"]: item["failure_scope"] for item in report["candidate_failures"]} == {
        "r2": "detail", "r3": "content_policy", "r4": "image", "r6": "creator",
    }
    assert report["runtime_blocker"] is None
    assert result["outcome"]["start"] is None


def check_repair_blocked(result, blocker: dict, error_type: str) -> None:
    report = json.loads(result["files"]["logs/xhs/repair_report.json"])
    assert report["runtime_blocker"] == blocker
    assert result["outcome"]["start"]["type"] == error_type
    assert not report["batches"]


def check_explore_blank(result) -> None:
    search_url = "https://www.xiaohongshu.com/search_result?keyword=" + quote("青岛旅游")
    explore = "https://www.xiaohongshu.com/explore"
    assert [item[3] for item in ops(result, "goto", "p0")] == [explore, explore, search_url, explore, search_url]
    stages = [(event["stage"], event["outcome"]) for event in navigation_events(result)]
    assert ("initial_explore", "visible_shell_timeout") in stages
    assert ("initial_explore_retry", "visible_shell_ready") in stages
    assert ("behavior_search_recovery", "explore_fallback_completed") in stages


def check_explore_blank_fail(result) -> None:
    assert [item[3] for item in ops(result, "goto", "p0")] == ["https://www.xiaohongshu.com/explore"] * 2
    assert result["outcome"]["start"]["message"] == "xhs_initial_explore_not_rendered"


def check_login_progress_tab(result) -> None:
    popup = ops(result, "popup")[0][2]
    # 登录状态机把人工进度页置前并切换；core 采用该页继续本轮，不新开浏览器。
    assert result["state"]["context_page"] == popup
    search = [item for item in ops(result, "goto") if "/search_result" in item[3]]
    assert search and {item[2] for item in search} == {popup}
    assert not ops(result, "reload") and not ops(result, "qr_component_refreshed")


def evidence_of(result) -> dict:
    return json.loads(result["files"]["logs/xhs/behavior_evidence.json"])


def check_batch_publish_failure(result) -> None:
    types = [(event["type"], event["details"].get("phase"), event["details"].get("stop_detail"))
             for event in execution_events(result)]
    # 先持久化批次事件再发布；发布失败写批次终态并以运行级失败结束。
    assert types[:2] == [("adaptive_batch_completed", None, None),
                         ("xhs_runtime_terminal", "batch_checkpoint", "xhs_batch_checkpoint_ack_timeout")]
    assert result["outcome"]["start"]["message"] == "xhs_batch_checkpoint_ack_timeout"


def check_continuity_verification(result) -> None:
    evidence = evidence_of(result)
    waits = [event for event in evidence["operator_verification_events"] if event["stage"] == "search_results"]
    assert len(waits) == 1 and waits[0]["status"] == "completed" and waits[0]["timeout_seconds"] == 600.0
    assert evidence["continuity_events"][0]["status"] == "completed"
    # 现状：连续性验证使用独立 600 秒等待，不扣共享人工预算（与文档不一致，保持现状）。
    assert result["state"]["budget_elapsed"] == 0.0
    assert stop_event(result)["stop_reason"] == "source_exhausted"


def check_continuity_blocked(result) -> None:
    assert result["outcome"]["start"]["message"] == "rate_limited_detected_during_xhs_continuity:search_results"
    assert evidence_of(result)["continuity_events"][-1]["status"] == "failed"


def check_search_ready_verification(result) -> None:
    readiness = evidence_of(result)["page_readiness"]
    assert readiness["ready"] is True
    assert readiness["operator_verification_events"][0]["status"] == "completed"


def check_creator_page_security(result) -> None:
    details = stop_event(result)
    assert details["stop_reason"] == "runtime_failed" and details["stop_detail"] == "platform_security_limit_300011"
    events = evidence_of(result)["platform_security_limit_events"]
    assert len(events) == 1 and events[0]["observed_error_code"] == "300011"
    author = ops(result, "new_page")[0][2]
    assert ops(result, "screenshot", author)


def check_creator_page_verification(result) -> None:
    author = ops(result, "new_page")[0][2]
    # 作者专属验证保留作者页并置前等待，计入同一人工预算。
    assert len(ops(result, "bring_to_front", author)) >= 2
    assert result["state"]["budget_elapsed"] > 0.0
    assert [record["fans_count"] for record in jsonl_records(result)] == ["88"]


def check_selfinfo_verification(result) -> None:
    start = result["outcome"]["start"]
    assert start["type"] == "PlatformRuntimeError" and start["code"] == "verification_required"
    assert not ops(result, "login_started")


def check_image_blocking(result) -> None:
    # 现状：图片运行级阻断先写停止事件，批次收尾时再写一次停止事件。
    stops = [event["details"] for event in execution_events(result) if event["type"] == "adaptive_search_stopped"]
    assert stops[0]["stop_reason"] == "runtime_failed" and stops[0]["stop_detail"] == "image_auth_required"
    assert jsonl_records(result) == []


def check_candidate_skips(result) -> None:
    skipped = [event["details"] for event in execution_events(result) if event["type"] == "candidate_skipped"]
    assert sorted((item["failure_scope"], item["error_code"]) for item in skipped) == [
        ("image", "image_download_retryable"), ("post", "api_and_html_empty"),
    ]
    assert jsonl_records(result) == []
    assert stop_event(result)["stop_reason"] == "source_exhausted"


CHECKS = {
    "success": check_success,
    "batch_publish_failure": check_batch_publish_failure,
    "continuity_verification": check_continuity_verification,
    "continuity_blocked": check_continuity_blocked,
    "search_ready_verification": check_search_ready_verification,
    "creator_page_security": check_creator_page_security,
    "creator_page_verification": check_creator_page_verification,
    "selfinfo_verification": check_selfinfo_verification,
    "search_429": lambda result: check_security(result, "rate_limited", "/search/notes"),
    "image_blocking": check_image_blocking,
    "candidate_skips": check_candidate_skips,
    "explore_blank": check_explore_blank,
    "explore_blank_fail": check_explore_blank_fail,
    "login_progress_tab": check_login_progress_tab,
    "qr_expired_refresh": check_qr_refresh,
    "scan_latched": check_latched,
    "login_budget_exhausted": check_budget_exhausted,
    "captcha_pass": check_captcha_pass,
    "captcha_rate_limited": check_captcha_rate_limited,
    "captcha_timeout": check_captcha_timeout,
    "login_expired": check_login_expired,
    "creator_primary_login": check_creator_primary_login,
    "network_recover": check_network_recover,
    "network_timeout": check_network_timeout,
    "security_300011": lambda result: check_security(result, "platform_security_limit_300011", "/search/notes"),
    "creator_300012": lambda result: check_security(result, "ip_blocked_300012", "/user/profile/u1"),
    "main_page_closed": check_main_page_closed,
    "cdp_disconnect": check_cdp_disconnect,
    "cdp_launch_failed": check_launch_failed,
    "cdp_concurrent_session": check_concurrent,
    "interaction_like": lambda result: check_interaction(result, "like-one"),
    "interaction_comment": lambda result: check_interaction(result, "comment-scroll"),
    "repair": check_repair,
    "repair_ip_blocked": lambda result: check_repair_blocked(
        result, {"error_type": "IPBlockError", "error_code": "ip_blocked"}, "IPBlockError"),
    "repair_300011": lambda result: check_repair_blocked(
        result, {"error_type": "PlatformRuntimeError", "error_code": "platform_security_limit_300011"},
        "PlatformRuntimeError"),
}


def check_scenario(result, scenario_name: str) -> None:
    scenario = SCENARIOS[scenario_name]
    check_common(result, scenario)
    check = CHECKS.get(scenario_name)
    if check is not None:
        check(result)


async def compare_sides(old_side, new_side, scenario_name, tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        old = await run_scenario(old_side, scenario_name, tmp_path, patch)
    with monkeypatch.context() as patch:
        new = await run_scenario(new_side, scenario_name, tmp_path, patch)
    assert_equivalent(old, new)
    check_scenario(old, scenario_name)
    check_scenario(new, scenario_name)
    return old, new


# ---------------------------------------------------------------------------
# 对照用例


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_name", sorted(SCENARIOS))
async def test_root_xhs_matches_frozen_legacy(tmp_path, monkeypatch, scenario_name):
    _, new = await compare_sides(LegacySide(), RootSide(), scenario_name, tmp_path, monkeypatch)
    # 搜索就绪等待由平台 behavior 提供，不再经 scripts 注入。
    assert set(new["ready_modules"]) <= {"trippostcollect.platforms.xhs.behavior"}


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_name", ["success", "qr_expired_refresh", "network_recover", "repair"])
async def test_frozen_legacy_harness_is_deterministic(tmp_path, monkeypatch, scenario_name):
    """比较器自检：同一冻结旧实现跑两次必须逐项一致。"""
    await compare_sides(LegacySide(label="legacy-a"), LegacySide(label="legacy-b"), scenario_name,
                        tmp_path, monkeypatch)


LEGACY_FACTORY_SCENARIOS = ["success", "qr_expired_refresh", "captcha_pass", "network_recover",
                            "cdp_launch_failed", "repair"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_name", LEGACY_FACTORY_SCENARIOS)
async def test_legacy_factory_matches_root_entry(tmp_path, monkeypatch, scenario_name):
    """旧桥 fork main 工厂与新 worker 入口在同一 fake 下逐项相等。"""
    await compare_sides(LegacyFactorySide(), RootSide(), scenario_name, tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# 单元级接缝


def side_modules(side, patch, tmp_path):
    world = World(SCENARIOS["success"], tmp_path / side.label)
    install_boundaries(patch, world)
    return world, side.install(patch, tmp_path)


def construct_crawler(mods, patch, out: Path):
    """root 侧经 configure 与 load_crawler 装配构造；旧侧直接无参构造冻结类。"""
    if not isinstance(mods.side, RootSide):
        return mods.core.XiaoHongShuCrawler()
    config = importlib.import_module("config")
    for name in dir(config):
        if name.isupper():
            patch.setattr(config, name, getattr(config, name))
    mods.entry.configure(scenario_argv(SCENARIOS["success"], out))
    return mods.side.crawler_class(mods)()


@pytest.mark.parametrize("side_factory", [LegacySide, RootSide], ids=["legacy", "root"])
def test_manual_wait_budget_charges_overlap_once(tmp_path, monkeypatch, side_factory):
    with monkeypatch.context() as patch:
        _, mods = side_modules(side_factory(), patch, tmp_path)
        module = mods.manual_wait
        now = [0.0]
        budget = module.XHSManualWaitBudget(limit_seconds=600, monotonic=lambda: now[0])
        first = budget.start("initial_qrcode_login")
        now[0] = 10.0
        second = budget.start("api_captcha_verification")
        now[0] = 30.0
        first.close()
        now[0] = 50.0
        with second.paused():
            now[0] = 80.0
        second.close()
        assert budget.manual_elapsed_seconds == 50.0
        overlap = module.XHSManualWaitBudget(limit_seconds=600, monotonic=lambda: now[0])
        waiter = overlap.start("midrun_login_recovery")
        other = overlap.start("creator_profile_verification")
        with waiter.paused():
            now[0] = 100.0
        assert overlap.manual_elapsed_seconds == 20.0
        waiter.close()
        other.close()
        for raw, expected in (("bad", 600.0), ("inf", 600.0), ("900", 600.0), ("120", 120.0), ("-5", 0.0)):
            patch.setenv("TRIPPOSTCOLLECT_XHS_LOGIN_WAIT_SECONDS", raw)
            assert module.XHSManualWaitBudget.from_environment(monotonic=lambda: 0.0).limit_seconds == expected
        exhausted = module.XHSManualWaitBudget(limit_seconds=5, monotonic=lambda: now[0])
        ticket = exhausted.start("x")
        now[0] += 5
        with pytest.raises(module.XHSManualWaitBudgetExhausted) as error:
            ticket.raise_if_exhausted()
        assert error.value.code == "xhs_manual_checkpoint_budget_exhausted"
        assert isinstance(error.value, mods.errors.PlatformRuntimeError)


@pytest.mark.asyncio
@pytest.mark.parametrize("side_factory", [LegacySide, RootSide], ids=["legacy", "root"])
async def test_popup_seams_are_instance_attributes(tmp_path, monkeypatch, side_factory):
    """fork 测试替换的 _popup_sleep/_popup_monotonic 仍是实例级接缝，预算与守卫都经它们计时。"""
    with monkeypatch.context() as patch:
        world, mods = side_modules(side_factory(), patch, tmp_path)
        for name, value in scenario_env(SCENARIOS["success"], tmp_path / "env").items():
            patch.setenv(name, value)
        crawler = construct_crawler(mods, patch, tmp_path / "env")
        slept = []
        clock = [5000.0]

        async def fake_sleep(seconds):
            slept.append(seconds)
            clock[0] += seconds

        crawler._popup_sleep = fake_sleep
        crawler._popup_monotonic = lambda: clock[0]
        assert crawler._get_manual_wait_budget().now() == 5000.0
        assert crawler._get_manual_wait_budget() is crawler._get_manual_wait_budget()
        page = Page(world, "https://www.xiaohongshu.com/explore/popup-ad")
        crawler._register_new_page(page, source="platform_opened")
        clock[0] += 12.0
        await crawler._close_page_with_deadline(page, reason="unit")
        # 守卫从登记时刻计 30 秒；剩余 18 秒经实例级 _popup_sleep 等待，关闭不早于 30 秒。
        assert slept == [18.0]
        assert page.closed and clock[0] == 5030.0
        assert ops({"trace": world.trace}, "bring_to_front", page.id)


def bound_paths(value, target, *, depth=3, prefix=""):
    """在 crawler 实例及其端口对象中按身份查找注入的可调用对象，返回属性路径。"""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        items = [(item.name, getattr(value, item.name)) for item in dataclasses.fields(value)]
    elif isinstance(value, SimpleNamespace) or type(value).__module__.startswith("trippostcollect."):
        items = list(vars(value).items()) if hasattr(value, "__dict__") else []
    else:
        return []
    found = []
    for name, item in items:
        if item is target:
            found.append(prefix + name)
        elif depth > 0:
            found += bound_paths(item, target, depth=depth - 1, prefix=prefix + name + ".")
    return found


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_name", ["login_budget_exhausted", "qr_env_invalid"])
@pytest.mark.parametrize("side_factory", [LegacySide, RootSide], ids=["legacy", "root"])
async def test_login_terminal_writes_use_worker_event_exit(tmp_path, monkeypatch, side_factory, scenario_name):
    """接缝 5：登录两处终态写出经 worker 出口（旧实现模块全局按名绑定；新实现经 entry 注入端口）。"""
    side = side_factory()
    with monkeypatch.context() as patch:
        routed = []
        original = events.append_and_publish

        def recording(event_type, details, **kwargs):
            routed.append(event_type)
            return original(event_type, details, **kwargs)

        # worker 出口在调用时按名查找 append_and_publish；legacy 直写出口不会经过这里。
        patch.setattr(events, "append_and_publish", recording)
        result = await run_scenario(side, scenario_name, tmp_path, patch)
        crawler = result["crawler"]
        module = sys.modules[type(crawler)._run_qrcode_login.__module__]
        xhs_modules = {name: item for name, item in sys.modules.items()
                       if name.startswith("trippostcollect.platforms.xhs")}
    terminal = [event for event in execution_events(result) if event["type"] == "xhs_runtime_terminal"]
    assert len(terminal) == 1 and routed.count("xhs_runtime_terminal") == 1
    method = next(node for node in ast.walk(ast.parse(Path(module.__file__).read_text()))
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_qrcode_login")
    calls = [node for node in ast.walk(method) if isinstance(node, ast.Call) and node.args
             and isinstance(node.args[0], ast.Constant) and node.args[0].value == "xhs_runtime_terminal"]
    assert len(calls) == 2 and len({ast.dump(call.func) for call in calls}) == 1
    if isinstance(side, RootSide):
        assert module.__name__ == "trippostcollect.platforms.xhs.login"
        # 两处写出都调用注入端口，端口与旧实现导入时绑定的是同一函数对象。
        assert isinstance(calls[0].func, ast.Attribute)
        assert bound_paths(crawler, events.append_worker_execution_event)
        for name, item in xhs_modules.items():
            assert not hasattr(item, "append_execution_event"), name
            assert not hasattr(item, "append_worker_execution_event"), name
    else:
        assert isinstance(calls[0].func, ast.Name)
        assert module.append_execution_event is events.append_worker_execution_event


@pytest.mark.parametrize("side_factory", [LegacySide, RootSide], ids=["legacy", "root"])
def test_repair_blocking_uses_exception_types_not_text(tmp_path, monkeypatch, side_factory):
    with monkeypatch.context() as patch:
        _, mods = side_modules(side_factory(), patch, tmp_path)
        repair, errors = mods.repair, mods.errors
        if isinstance(mods.side, RootSide):
            # 根实现显式导入 errors；不得再按 fork 模块路径字符串查找异常类型。
            patch.delitem(sys.modules, "media_platform.xhs.core", raising=False)
        crawler = SimpleNamespace(_request_failure_exception=lambda exc: exc)
        plain_ip = errors.IPBlockError("Network connection error, please check network settings or restart")
        plain_runtime = errors.PlatformRuntimeError("XHS platform security limit, code 300011",
                                                    code="platform_security_limit_300011")
        assert repair._repair_exception_is_blocking(crawler, plain_ip) is True
        assert repair._repair_exception_is_blocking(crawler, plain_runtime) is True
        assert repair._repair_exception_is_blocking(crawler, errors.DataFetchError("普通失败")) is False
        assert repair._repair_exception_is_blocking(crawler, RuntimeError("captcha visible")) is True
        assert repair._xhs_repair_blocker(crawler, plain_ip) == {"error_type": "IPBlockError", "error_code": "ip_blocked"}
        assert repair._xhs_repair_failure_scope(RuntimeError()) == "candidate"
        failure = repair._xhs_repair_failure(note_id="r9", batch=2, failure_scope="detail", error_code="note_not_found")
        assert failure["retryable"] is False and failure["identity"] == "xhs:id:r9"


def test_root_repair_imports_root_errors_explicitly():
    path = ROOT / "src/trippostcollect/platforms/xhs/repair.py"
    tree = ast.parse(path.read_text())
    imported = {
        alias.name
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        and node.module == "trippostcollect.platforms.xhs.errors"
        for alias in node.names
    }
    assert {"IPBlockError", "PlatformRuntimeError"} <= imported
    assert "sys.modules" not in path.read_text()


@pytest.mark.asyncio
async def test_root_repair_is_explicit_branch_without_class_patch(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        world, mods = side_modules(RootSide(), patch, tmp_path)
        for name, value in scenario_env(SCENARIOS["repair"], tmp_path / "env").items():
            patch.setenv(name, value)
        config = importlib.import_module("config")
        for name in dir(config):
            if name.isupper():
                patch.setattr(config, name, getattr(config, name))
        for name in ("media_platform.xhs", "media_platform.xhs.core", "store.xhs"):
            patch.delitem(sys.modules, name, raising=False)
        mods.entry.configure(scenario_argv(SCENARIOS["repair"], tmp_path / "env"))
        mods.entry.install_hooks()
        cls = mods.entry.load_crawler("xhs")
        assert not hasattr(cls, "_trippostcollect_repair_resilience")
        assert cls.get_specified_notes.__module__ == "trippostcollect.platforms.xhs.detail"
        assert "media_platform.xhs" not in sys.modules and "media_platform.xhs.core" not in sys.modules


@pytest.mark.parametrize("side_factory", [LegacySide, RootSide], ids=["legacy", "root"])
def test_pure_parsers_and_signer(tmp_path, monkeypatch, side_factory):
    with monkeypatch.context() as patch:
        world, mods = side_modules(side_factory(), patch, tmp_path)
        parser = mods.parser
        info = parser.help.parse_note_info_from_note_url(REPAIR_URLS[0])
        assert (info.note_id, info.xsec_token, info.xsec_source) == ("r1", "tok-r1", "pc_search")
        assert type(info).__name__ == "NoteUrlInfo" and set(type(info).model_fields) == {
            "note_id", "xsec_token", "xsec_source"}
        assert parser.help.base36encode(36 ** 3 + 35) == "100Z"
        extractor = parser.extractor.XiaoHongShuExtractor()
        assert extractor.extract_note_detail_from_html("n2", note_html("n2"))["note_id"] == "n2"
        assert extractor.extract_note_detail_from_html("n2", "<html>验证</html>") is None
        creator = extractor.extract_creator_info_from_html(creator_html("u1", "77"))
        assert creator["basicInfo"]["nickname"] == NICK["u1"]
        assets = parser.store._xhs_image_assets(NOTES["n1"])
        assert [item["url"].rsplit("/", 1)[-1].split("!")[0] for item in assets] == [
            "n1-0-url_default", "n1-1-url", "n1-2-url_pre"]
        assert parser.manifest.xhs_source_asset_key(assets[0]["url"]).startswith("xhs:path:/notes_pre_post/")
        crawler_class = mods.core.XiaoHongShuCrawler
        assert crawler_class.is_video_note({"type": "video"}) and not crawler_class.is_video_note({"type": "normal"})
        random.seed(SEED)
        world.clock = 2000.0
        signed = [mods.signer.sign_with_xhshow("/api/x", {"a": 1}, "c=1", "POST"),
                  mods.signer.sign_with_xhshow("/api/y", {"b": 2}, "c=2", "GET")]
        assert len(ops({"trace": world.trace}, "xhshow")) == 2
        assert [re.fullmatch(r"[0-9a-f]{16}", item["x-b3-traceid"]) is not None for item in signed] == [True, True]
        enums = mods.parser.models
        assert enums.SearchSortType("popularity_descending").name == "MOST_POPULAR"
        assert [item.value for item in enums.SearchNoteType] == [0, 1, 2]


def test_legacy_fork_package_delegates_to_root(tmp_path, monkeypatch):
    """fork 旧位置只留薄转发：类、异常与枚举都是根对象，不存在第二份实现。"""
    import inspect

    with monkeypatch.context() as patch:
        _, mods = side_modules(LegacyFactorySide(), patch, tmp_path)
        legacy = importlib.import_module("media_platform.xhs")
        root_class = mods.core.XiaoHongShuCrawler
        assert issubclass(legacy.XiaoHongShuCrawler, root_class)
        for name in dir(root_class):
            if name.startswith("__"):
                continue
            value = inspect.getattr_static(root_class, name)
            if callable(getattr(root_class, name)):
                assert inspect.getattr_static(legacy.XiaoHongShuCrawler, name) is value, name
        for old_module, new_module, names in (
            ("client", mods.client, ["XiaoHongShuClient"]),
            ("login", mods.login, ["XiaoHongShuLogin"]),
            ("manual_wait", mods.manual_wait, ["XHSManualWaitBudget", "XHSManualWaitBudgetExhausted"]),
            ("exception", mods.errors, ["DataFetchError", "IPBlockError", "PlatformRuntimeError", "NoteNotFoundError"]),
            ("field", mods.parser.models, ["SearchSortType", "SearchNoteType"]),
            ("extractor", mods.parser.extractor, ["XiaoHongShuExtractor"]),
        ):
            old = importlib.import_module("media_platform.xhs." + old_module)
            for name in names:
                assert getattr(old, name) is getattr(new_module, name), (old_module, name)
        assert importlib.import_module("model.m_xiaohongshu").NoteUrlInfo is mods.parser.models.NoteUrlInfo
