"""OS lane：本地 Chromium headless shell + `context.route` 离线回放小红书全量扫描（设计 §11.2）。

只在 macOS、`TPC_BROWSER_REPLAY=1` 且找得到 Playwright 管理的 headless shell 时执行；VPS 与 component lane
跳过（component lane 按标记直接排除）。浏览器由 Playwright driver（node）经管道拉起，不经 Python 子进程；
页面的全部请求由离线站点的 route 用 fixture 回应或拒绝，结束时断言没有请求逃逸路由。

真实事件时序是这里要验证的：首图先于 feed 到达仍被窗口认领、`response.body()` 取到 PNG 字节、
target=_blank 作者页、虚拟列表回收、feed 461 后的验证层与重做、同一文档重开时图片走内存不再请求、
视口未填满时前端自行补页。人工停顿与行为阶段换成即时桩（与 component 用例相同），其余走真实代码。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote, urlsplit

import pytest
from playwright.async_api import async_playwright

from support.xhs_offline_site import (
    AVATAR_HOST,
    EXPLORE_URL,
    HOT_QUERY_ID,
    PNG_SIGNATURE,
    TARGET_FILTERS,
    XhsOfflineSite,
    image_url,
    search_item,
)
from support.xhs_fake_site import detail_card
from trippostcollect.application.reporting import _url_contains_keyword
from trippostcollect.application.visit_gate import validate_content_visits
from trippostcollect.artifacts.image_manifest import ImageManifestEntry, write_manifest_atomic
from trippostcollect.platforms.xhs import author as xhs_author
from trippostcollect.platforms.xhs import core as xhs_core
from trippostcollect.platforms.xhs import scan as xhs_scan
from trippostcollect.platforms.xhs import session as xhs_session
from trippostcollect.platforms.xhs.page_scripts import DOM_SCRIPTS
from trippostcollect.records.sanitization import sanitize_export_item
from trippostcollect.runtime.human_pointer import pointer_for
from trippostcollect.runtime.visit_ledger import iter_ledger_lines
from trippostcollect.xhs.search_target import is_target_search

from .support import XiaoHongShuCrawler, assert_no_avatar_log_lines, config, crawler_log_lines, make_capture

ROOT = Path(__file__).resolve().parents[3]
KEYWORD = "青岛旅游"
VIEWPORT = {"width": 1280, "height": 900}
# 程序内的随机等待按真实值执行但截到此上限：滚动后留出渲染时间，又不让回放过长。
ACTION_SLEEP_CAP_SECONDS = 0.3
REQUIRED_STAGES = ["creator_profile", "note_detail", "search_page", "search_results"]
# #84：在每个文档里记下指针、点击与滚轮事件（经 expose_function 交回 Python，跨导航与作者标签页都在），回放后对照
# 操作人的 Mac 触控板样本核对合成事件的结构。
POINTER_RECORDER = """(() => {
    if (window.__tpcPointerRecorder) return;
    window.__tpcPointerRecorder = true;
    const send = (record) => { try { window.__tpcPointerEvent({ ...record, doc: location.pathname }); } catch (error) {} };
    const at = (event) => performance.timeOrigin + event.timeStamp;
    const options = { capture: true, passive: true };
    addEventListener('pointermove', (event) => send({ type: 'move', t: at(event), x: event.clientX, y: event.clientY }), options);
    addEventListener('pointerdown', (event) => {
        const rect = event.target instanceof Element ? event.target.getBoundingClientRect() : null;
        send({
            type: 'down', t: at(event), x: event.clientX, y: event.clientY,
            pressure: event.pressure, width: event.width, height: event.height,
            rect: rect ? { left: rect.left, top: rect.top, width: rect.width, height: rect.height } : null,
        });
    }, options);
    addEventListener('pointerup', (event) => send({ type: 'up', t: at(event) }), options);
    addEventListener('click', (event) => send({ type: 'click', t: at(event), x: event.clientX, y: event.clientY }), options);
    addEventListener('wheel', (event) => send({
        type: 'wheel', t: at(event), dx: event.deltaX, dy: event.deltaY, mode: event.deltaMode,
    }), options);
})()"""


def _headless_shell_installed() -> bool:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    root = Path(configured) if configured else Path.home() / "Library" / "Caches" / "ms-playwright"
    return root.is_dir() and any(root.glob("chromium_headless_shell-*"))


def _skip_reason() -> str:
    if sys.platform != "darwin":
        return "browser_replay 只在 macOS OS lane 执行"
    if os.environ.get("TPC_BROWSER_REPLAY") != "1":
        return "未设置 TPC_BROWSER_REPLAY=1"
    if not _headless_shell_installed():
        return "找不到 Playwright 管理的 Chromium headless shell"
    return ""


SKIP_REASON = _skip_reason()
pytestmark = [
    pytest.mark.browser_replay,
    pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON or "browser replay available"),
]


@asynccontextmanager
async def replay_browser(site: XhsOfflineSite):
    """一个浏览器、一个 Context、一个主页面；Context 内的全部请求都交给离线站点的路由。第三项是页面记下的指针事件。"""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--disable-smooth-scrolling"])
        try:
            context = await browser.new_context(viewport=VIEWPORT, locale="zh-CN", service_workers="block")
            site.watch(context)
            await context.route("**/*", site.handle)
            pointer_events: list[dict] = []
            await context.expose_function("__tpcPointerEvent", pointer_events.append)
            await context.add_init_script(POINTER_RECORDER)
            page = await context.new_page()
            yield context, page, pointer_events
            await context.close()
        finally:
            await browser.close()


async def capped_sleep(seconds: float) -> None:
    await asyncio.sleep(min(float(seconds), ACTION_SLEEP_CAP_SECONDS))


def prepare_crawler(monkeypatch, tmp_path, context, page, *, known: list[str]):
    """与 component 的发现用例相同的装配，页面与 Context 换成真实浏览器；返回 (crawler, 状态文件)。

    `known` 写入临时 SQLite 的 web_posts，扫描时作为已知候选滑过。人工停顿只写账本（search 阶段记连续性完成），
    行为阶段桩在真实页面上滚轮下上各一次并写出最小行为证据；暂存与写出换成内存记录。
    """
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    evidence_path = tmp_path / "behavior_evidence.json"
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)")
        conn.executemany("INSERT INTO web_posts VALUES ('xhs', ?, NULL)", [(note_id,) for note_id in known])
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(db_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(evidence_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", str(ROOT / "scripts"))
    monkeypatch.setattr(config, "KEYWORDS", KEYWORD)
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(xhs_scan, "merge_pending_behavior_evidence", lambda pending, ports: None)
    monkeypatch.setattr(xhs_session, "XHS_NEW_PAGE_MIN_HOLD_SECONDS", 1.0)
    monkeypatch.setattr(xhs_author, "XHS_AUTHOR_TAB_HOLD_SECONDS", (1.0, 1.0))

    crawler = XiaoHongShuCrawler()
    crawler.context_page = page
    crawler.browser_context = context
    crawler.capture = make_capture(tmp_path, context=context)
    crawler.capture.install(page)
    crawler._scan_sleep = capped_sleep
    crawler.behavior_urls = []
    crawler.staged = {}
    crawler.records = []

    async def guarded_pause(stage, minimum, maximum):
        start = crawler.capture.pause_started()
        continuity = "completed" if stage in {"search_results", "search_page"} else "n/a"
        crawler.capture.pause_finished(stage, start, 0.0, continuity)
        return 0.0

    async def behavior(keyword):
        crawler.behavior_urls.append(page.url)
        pointer = pointer_for(page)  # 与正式行为阶段（xhs_guarded）相同：轨迹移动、触控板手势滚动
        await pointer.move_to(640, 520)
        await pointer.scroll(600)
        await asyncio.sleep(0.5)
        await pointer.scroll(-600)
        await asyncio.sleep(0.5)
        evidence_path.write_text(
            json.dumps({"status": "completed", "url": page.url, "events": [], "operator_verification_events": []}),
            encoding="utf-8",
        )
        return {"status": "completed"}

    async def stage_images(note_id, items):
        crawler.staged[note_id] = items

    async def record_image_failure(note_id, asset):
        raise AssertionError(f"unexpected image failure for {note_id}: {asset.get('error_code')}")

    class MemorySink:
        async def store_content(self, item):
            crawler.records.append(item)

    async def store(note_item):
        await xhs_core.store_xhs_note(
            note_item, source_keyword=KEYWORD, crawler_type="search", save_data_option="jsonl",
            current_timestamp=lambda: 0, content_sink_factory=lambda _crawler_type: MemorySink(),
        )

    crawler._guarded_pause = guarded_pause
    crawler._run_human_behavior_on_primary_page = behavior
    crawler.update_xhs_note_images = stage_images
    crawler.record_xhs_note_image_failure = record_image_failure
    monkeypatch.setattr(crawler, "update_xhs_note", store)
    return crawler, state_path


def ledger_lines(crawler) -> list[dict]:
    return list(iter_ledger_lines(crawler.capture.ledger.path))


def actions(crawler, kind: str) -> list[dict]:
    return [line for line in ledger_lines(crawler) if line.get("kind") == "action" and line["action"] == kind]


def visit(crawler, visit_id: str) -> dict:
    return next(line for line in ledger_lines(crawler) if line.get("id") == visit_id)


def last_stop(state_path) -> dict:
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    return [event["details"] for event in events if event["type"] == "adaptive_search_stopped"][-1]


def run_gate(crawler, state_path, tmp_path) -> dict:
    """按交给暂存的字节写出与正式暂存同构的 manifest，再用真实门禁回读账本与记录。"""
    manifest = tmp_path / "image_manifest.jsonl"
    write_manifest_atomic(manifest, [
        ImageManifestEntry(
            schema_version=1, platform_key="xhs", platform_post_id=note_id, image_role="content",
            source_index=item["source_index"], source_key="image_list", source_asset_key=item["source_asset_key"],
            source_url=item["url"], fetch_status="downloaded", attempts=1, http_status=200,
            staging_path=f"images/{note_id}/{item['source_index']}.png", size_bytes=len(item["content"]),
            mime_type="image/png", width=2, height=2, sha256=sha256(item["content"]).hexdigest(), error_code=None,
        )
        for note_id, items in crawler.staged.items()
        for item in items
    ])
    return validate_content_visits(
        crawler.records,
        [crawler.capture.ledger.path],
        platform="xhs",
        mode="discovery",
        manifests=[str(manifest)],
        stop_event=last_stop(state_path),
    )


def assert_pointer_events_match_the_sample(events: list[dict], crawler) -> None:
    """合成事件的结构与 Mac 触控板样本一致：移动约每帧一个、落点不在几何中心、按住 81–132ms、pressure 0.5 与
    1×1 接触面、整数点击坐标；滚轮每个手势至少 8 个事件、deltaMode 0、|deltaY| 不超过 60。"""
    events = sorted(events, key=lambda event: event["t"])
    moves = [event for event in events if event["type"] == "move"]
    gaps = [later["t"] - earlier["t"] for earlier, later in zip(moves, moves[1:]) if later["doc"] == earlier["doc"]]
    assert len(gaps) > 50 and 12 <= sorted(gaps)[len(gaps) // 2] <= 25
    downs = [event for event in events if event["type"] == "down"]
    ups = [event for event in events if event["type"] == "up"]
    assert len(downs) == len(ups) >= len(actions(crawler, "click_card")) + len(actions(crawler, "apply_filter"))
    for down, up in zip(downs, ups):
        assert down["pressure"] == 0.5 and down["width"] == 1 and down["height"] == 1
        assert float(down["x"]).is_integer() and float(down["y"]).is_integer()
        assert 70 <= up["t"] - down["t"] <= 200, up["t"] - down["t"]
    centered = [
        down for down in downs if down["rect"] is not None
        and abs(down["x"] - (down["rect"]["left"] + down["rect"]["width"] / 2)) < 1
        and abs(down["y"] - (down["rect"]["top"] + down["rect"]["height"] / 2)) < 1
    ]
    assert len(centered) < len(downs)
    clicks = [event for event in events if event["type"] == "click"]
    assert clicks and all(float(event["x"]).is_integer() and float(event["y"]).is_integer() for event in clicks)
    wheels = [event for event in events if event["type"] == "wheel"]
    gestures: list[list[dict]] = []
    for event in wheels:
        if not gestures or event["t"] - gestures[-1][-1]["t"] > 200 or event["doc"] != gestures[-1][-1]["doc"]:
            gestures.append([])
        gestures[-1].append(event)
    assert gestures and all(len(gesture) >= 8 for gesture in gestures), [len(gesture) for gesture in gestures]
    assert {event["mode"] for event in wheels} == {0}
    assert all(abs(event["dy"]) <= 60 and float(event["dy"]).is_integer() for event in wheels)


def assert_staged_png(site: XhsOfflineSite, crawler, note_id: str, count: int) -> None:
    staged = crawler.staged[note_id]
    assert [item["url"] for item in staged] == [image_url(note_id, index) for index in range(count)]
    for item in staged:
        assert item["content"].startswith(PNG_SIGNATURE)
        assert item["content"] == site.image_body(item["url"])


@pytest.mark.asyncio
async def test_full_scan_in_local_chromium_passes_the_visit_gate(monkeypatch, tmp_path):
    """AI 对话布局下 3 页（每页 12 项、共 36 项）末页 has_more=false：等延迟渲染、位于对话栏内的筛选入口，按文字
    点选“图文”的真标签（不是重叠的 aria-hidden 副本）、收面板（面板从 DOM 移除）；列表在页头下方的容器内滚动、
    window 不动，滚轮只作用在结果列表上，对话栏里与结果 ID 重叠的笔记链接不计入、不点击，点卡片都命中封面；
    虚拟列表回收下的全量扫描，第 1 页中间的相关搜索卡（hot_query，没有封面）跳过、不停止；浮层详情与翻图截获 PNG
    （首图先于 feed 到达）；作者链接按作者 ID 定位：旧位置的隐藏作者名与评论区（含作者本人的评论）都不点，点中
    interaction-container 里的作者名打开 target=_blank 作者页（同一作者只点一次），经真实门禁通过。"""
    pages = [[search_item(f"p{number}-{index:02d}") for index in range(12)] for number in (1, 2, 3)]
    pages[0][6] = search_item("p1-06", card_type="video")
    images = {"p1-02": 3, "p2-05": 2, "p3-09": 1}
    site = XhsOfflineSite(KEYWORD, pages, hot_query_at=6)
    site.details["p1-02"] = detail_card("p1-02", images=3, user_id="author-a")
    site.details["p2-05"] = detail_card("p2-05", images=2, user_id="author-a")
    site.details["p3-09"] = detail_card("p3-09", images=1, user_id="author-b")
    known = [item["id"] for items in pages for item in items if item["id"] not in {*images, "p1-06"}]

    async with replay_browser(site) as (context, page, pointer_events):
        crawler, state_path = prepare_crawler(monkeypatch, tmp_path, context, page, known=known)
        await page.goto(EXPLORE_URL)
        with crawler_log_lines() as log_lines:
            await crawler.search()
        audit = await page.evaluate("() => window.__tpcOffline")
        layout = await page.evaluate("""() => ({
            ai_chat: document.querySelectorAll('.ai-feeds-page.with-ai-chat').length,
            entries: document.querySelectorAll('div.filter').length,
            layout_entries: document.querySelectorAll('.search-layout div.filter').length,
            chat_entries: document.querySelectorAll('.ai-chat-inner > div.filter.ai-chat-filter').length,
            panels: document.querySelectorAll('.filters-wrapper').length,
            entry_active: document.querySelectorAll('div.filter.active').length,
            chat_scroll_top: document.querySelector('.ai-chat-messages').scrollTop,
        })""")
        lists = await page.evaluate("""() => ({
            chat_note_ids: Array.from(document.querySelectorAll('.ai-chat-section [data-note-id]'))
                .map((node) => node.getAttribute('data-note-id')),
            chat_explore_links: document.querySelectorAll('.ai-chat-section a[href^="/explore/"]').length,
            list_cards: document.querySelectorAll('.search-layout section.note-item').length,
            window_scroll_y: window.scrollY,
        })""")
        overlay_links = await page.evaluate("""() => {
            const mask = document.querySelector('div.note-detail-mask');
            const comments = Array.from(mask.querySelectorAll('.comment-item a.name'));
            return {
                old_names: mask.querySelectorAll('.note-container > .author a.name').length,
                author_names: mask.querySelectorAll('.interaction-container .author-container a.name').length,
                avatar_links: mask.querySelectorAll('.author-container .avatar-container a').length,
                comment_links: comments.length,
                own_comment_links: comments.filter(
                    (node) => new URL(node.href).pathname === '/user/profile/author-b'
                ).length,
            };
        }""")
        metrics = await page.evaluate(DOM_SCRIPTS["scroll_metrics"])
        counts = await page.evaluate(DOM_SCRIPTS["search_ready_counts"])
        first_row_rendered = await page.evaluate("() => document.querySelectorAll('section.note-item[data-index=\"0\"]').length")
        final_path = urlsplit(page.url).path
        open_pages = len(context.pages)

    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted" and stopped["stop_detail"] == "has_more_false"
    assert stopped["source_page"] == 3 and stopped["batch_complete"] is True
    assert site.feed_requests == ["p1-02", "p2-05", "p3-09"]  # 视频在点击前跳过，已知项滑过
    assert [record["note_id"] for record in crawler.records] == ["p1-02", "p2-05", "p3-09"]
    assert final_path == "/search_result_ai"
    # 整轮日志（含写出记录与作者页的日志）不命中头像正则；失败时只报模块标签（#80）。
    assert any(line.startswith("[store.xhs.update_xhs_note]") for line in log_lines)
    assert_no_avatar_log_lines(log_lines)

    # 回车会话不带 filters；按文字点选“图文”后目标会话第 1 页带 5 项 filters；面板收起后才滚动。
    assert "filters" not in site.search_requests[0] and site.search_requests[0]["keyword"] == KEYWORD
    target = [body for body in site.search_requests if "filters" in body]
    assert target[0]["filters"] == TARGET_FILTERS and is_target_search(target[0])
    assert [body["page"] for body in target] == [1, 2, 3]
    assert audit["clickedOptions"] == [["filter_note_type", "图文"]]
    assert audit["hiddenTagClicks"] == 0  # 点中真标签，不是 aria-hidden 副本
    assert layout == {
        "ai_chat": 1, "entries": 1, "layout_entries": 0, "chat_entries": 1,
        "panels": 0, "entry_active": 0, "chat_scroll_top": 0,
    }
    assert audit["chatScrolls"] == 0  # 滚轮全部作用在结果列表上
    # 列表在容器内滚动：window 始终不动，账本中的滚动位置取自容器；第 2、3 页由滚到容器底部附近触发。
    assert audit["maxWindowScrollY"] == 0 and lists["window_scroll_y"] == 0
    # 行为阶段的 scroll 动作只记 phase，没有滚动位置；这里只看扫描滚动。
    scan_scrolls = [line["target"] for line in actions(crawler, "scroll") if line["target"].get("phase") != "behavior_stage"]
    assert audit["maxListScrollTop"] > 0 and max(target["scroll_y_before"] for target in scan_scrolls) > 0
    assert audit["scrollLoads"] == 2 and audit["selfLoads"] == 0
    assert metrics["scroll_container"] == "element" and metrics["view_rect"]["top"] == 116
    assert metrics["list_rect"]["right"] <= VIEWPORT["width"] * 0.65 + 1
    # 对话栏里的笔记链接与结果 ID 重叠，但不计入列表、从未被点；点卡片的落点都在封面内。
    assert lists["chat_note_ids"] == ["p1-00", "p1-02", "p1-03"] and lists["chat_explore_links"] == 2
    assert counts["card_count"] == lists["list_cards"] > 0
    assert audit["chatNoteClicks"] == 0 and audit["strayClicks"] == []
    assert audit["coverClicks"] == len(actions(crawler, "click_card")) == audit["overlaysOpened"] == 3
    # 第 1 页中间的相关搜索卡渲染过、从未被点，作为本会话已知的非笔记条目跳过。
    assert audit["queryCardsRendered"] >= 1 and audit["queryCardClicks"] == 0
    assert crawler._scan.target.pages[1].non_notes_seen == {HOT_QUERY_ID}
    # 浮层作者区与实测一致；只点了笔记作者名，评论者链接（含作者本人的评论）一次都没点。
    assert overlay_links == {
        "old_names": 1, "author_names": 1, "avatar_links": 1, "comment_links": 3, "own_comment_links": 1,
    }
    assert audit["authorNameClicks"] == len(actions(crawler, "click_author")) == 2
    assert audit["commentLinkClicks"] == 0
    filter_targets = [line["target"].get("filter") for line in actions(crawler, "apply_filter")]
    assert filter_targets == ["panel_open", "note_type", "panel_close"]
    first_scroll = actions(crawler, "scroll")[0]["seq"]
    assert actions(crawler, "apply_filter")[-1]["seq"] < first_scroll

    # 行为证据地址是两次编码的 /search_result_ai，target_url_ok 判真。
    behavior_url = crawler.behavior_urls[0]
    assert urlsplit(behavior_url).path == "/search_result_ai"
    assert f"keyword={quote(quote(KEYWORD, safe=''), safe='')}" in behavior_url
    assert _url_contains_keyword(behavior_url, KEYWORD) is True

    # 虚拟列表至多 30 个节点且发生过回收：扫到末尾时第 0 项已不在 DOM 中。
    assert sum(len(items) for items in pages) > 30
    assert audit["maxNoteNodes"] <= 30 and audit["recycledNodes"] > 0 and first_row_rendered == 0

    # 真实 response.body() 取到站点给出的 PNG；每张图只请求一次。
    for note_id, count in images.items():
        assert_staged_png(site, crawler, note_id, count)
        assert all(site.image_requests[image_url(note_id, index)] == 1 for index in range(count))
    refs = {record["note_id"]: record["content_visit_refs"] for record in crawler.records}
    first_image = visit(crawler, refs["p1-02"]["images"][0])
    assert first_image["seq"] < visit(crawler, refs["p1-02"]["note_detail"])["seq"]  # 首图先于 feed 到达
    assert len(actions(crawler, "flip_image")) == 3
    assert refs["p2-05"]["creator_profile"] == refs["p1-02"]["creator_profile"]
    assert refs["p3-09"]["creator_profile"] != refs["p1-02"]["creator_profile"]
    assert [path for path in site.document_requests if path.startswith("/user/profile/")] == [
        "/user/profile/author-a", "/user/profile/author-b",
    ]
    assert len(actions(crawler, "close_tab")) == 2 and open_pages == 1
    # 作者页投影在真实页面中就不带头像；feed 卡片头像只在内存记录中，由写出端的导出清理器去除。
    assert f"{AVATAR_HOST}/avatar/" not in json.dumps(crawler.records, ensure_ascii=False)
    monkeypatch.setenv("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS", "1")
    exported = [sanitize_export_item(record) for record in crawler.records]
    assert "sns-avatar" not in json.dumps(exported, ensure_ascii=False)

    result = run_gate(crawler, state_path, tmp_path)
    assert result["problem_counts"] == {}
    assert result["content_visit_ok"] is True and result["checked_records"] == 3
    assert result["required_stages"] == REQUIRED_STAGES
    assert result["continuity_ok"] is True and result["request_pacing_ok"] is True
    assert site.unexpected == [] and site.escaped() == []
    assert_pointer_events_match_the_sample(pointer_events, crawler)


@pytest.mark.asyncio
async def test_feed_verification_layer_then_redo_reuses_the_cached_first_image(monkeypatch, tmp_path):
    """feed 返回 461：站点显示验证层（captcha iframe），完成后整篇重做；同一文档重开时首图走内存、没有新的
    响应事件，记录引用第一次尝试截获的图片访问，门禁照常通过。"""
    items = [search_item(f"v-{index}") for index in range(8)]
    site = XhsOfflineSite(KEYWORD, [items])
    site.details["v-3"] = detail_card("v-3", images=2, user_id="author-v")
    site.feed_overrides["v-3"] = [461]
    known = [item["id"] for item in items if item["id"] != "v-3"]

    async with replay_browser(site) as (context, page, pointer_events):
        crawler, state_path = prepare_crawler(monkeypatch, tmp_path, context, page, known=known)
        await page.goto(EXPLORE_URL)
        await crawler.search()
        audit = await page.evaluate("() => window.__tpcOffline")
        layers = await page.evaluate("() => document.querySelectorAll('.captcha-layer').length")

    assert site.feed_requests == ["v-3", "v-3"]
    assert audit["captchaShown"] == 1 and layers == 0
    assert site.document_requests.count("/website-login/captcha") == 1
    assert [record["note_id"] for record in crawler.records] == ["v-3"]
    assert_staged_png(site, crawler, "v-3", 2)
    assert site.image_requests[image_url("v-3", 0)] == 1 and site.image_requests[image_url("v-3", 1)] == 1
    clicks = [line for line in actions(crawler, "click_card") if line["target"]["note_id"] == "v-3"]
    assert len(clicks) == 2
    refs = crawler.records[0]["content_visit_refs"]
    first_image = visit(crawler, refs["images"][0])
    assert clicks[0]["seq"] < first_image["seq"] < clicks[1]["seq"]  # 引用第一次尝试截获的首图
    assert visit(crawler, refs["note_detail"])["seq"] > clicks[1]["seq"]
    evidence = json.loads((tmp_path / "behavior_evidence.json").read_text(encoding="utf-8"))
    assert [event["status"] for event in evidence["operator_verification_events"]] == ["completed"]

    result = run_gate(crawler, state_path, tmp_path)
    assert result["problem_counts"] == {}
    assert result["content_visit_ok"] is True and result["checked_records"] == 1
    assert site.unexpected == [] and site.escaped() == []


@pytest.mark.asyncio
async def test_frontend_self_load_when_viewport_is_not_filled_passes_the_gate(monkeypatch, tmp_path):
    """V16 布局（整页滚动）下目标会话第 1 页只有一行、填不满视口：站点不经滚动自行请求第 2 页，门禁计入
    frontend_self_loads 而不失败；筛选入口在 .search-layout 内，其前的零尺寸副本被跳过。"""
    pages = [[search_item(f"s1-{index}") for index in range(4)], [search_item(f"s2-{index}") for index in range(8)]]
    site = XhsOfflineSite(KEYWORD, pages, layout="v16")
    unknown = {"s1-1", "s2-4"}
    known = [item["id"] for items in pages for item in items if item["id"] not in unknown]

    async with replay_browser(site) as (context, page, pointer_events):
        crawler, state_path = prepare_crawler(monkeypatch, tmp_path, context, page, known=known)
        await page.goto(EXPLORE_URL)
        await crawler.search()
        audit = await page.evaluate("() => window.__tpcOffline")
        filter_nodes = await page.evaluate("() => document.querySelectorAll('.search-layout div.filter').length")
        ai_chat = await page.evaluate("() => document.querySelectorAll('.with-ai-chat').length")
        metrics = await page.evaluate(DOM_SCRIPTS["scroll_metrics"])

    assert audit["selfLoads"] >= 1
    assert filter_nodes == 2 and ai_chat == 0  # 零尺寸副本在真实入口之前，入口延迟插入
    assert metrics["scroll_container"] == "document" and metrics["view_rect"]["top"] == 0  # 整页滚动
    assert audit["clickedOptions"] == [["filter_note_type", "图文"]]
    assert [record["note_id"] for record in crawler.records] == ["s1-1", "s2-4"]
    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted" and stopped["source_page"] == 2

    result = run_gate(crawler, state_path, tmp_path)
    assert result["problem_counts"] == {}
    assert result["content_visit_ok"] is True and result["checked_records"] == 2
    assert result["frontend_self_loads"] >= 1
    assert site.unexpected == [] and site.escaped() == []
