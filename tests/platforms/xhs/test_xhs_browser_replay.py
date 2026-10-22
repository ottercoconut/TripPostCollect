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
import re
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
from trippostcollect.runtime.human_pointer import (
    FRAME_JITTER,
    FRAME_MS,
    GESTURE_MAX_PX,
    GESTURE_MIN_EVENTS,
    GESTURE_PEAK_MAX,
    HOLD_MS,
    pointer_for,
)
from trippostcollect.runtime.visit_ledger import iter_ledger_lines
from trippostcollect.xhs.engagement import engagement_observation
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

            # Playwright 要给回调设属性，内置方法 list.append 不能设属性，必须包一层普通函数（同 8376f79）。
            def record_pointer_event(event: dict) -> None:
                pointer_events.append(event)

            await context.expose_function("__tpcPointerEvent", record_pointer_event)
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


def filter_targets(crawler) -> list[tuple]:
    """筛选动作的 (filter, method) 序列（#81）。"""
    return [(line["target"].get("filter"), line["target"].get("method")) for line in actions(crawler, "apply_filter")]


def visit(crawler, visit_id: str) -> dict:
    return next(line for line in ledger_lines(crawler) if line.get("id") == visit_id)


def engagement_lines(tmp_path) -> dict[str, dict]:
    path = tmp_path / "behavior_evidence.engagement.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return {line["note_id"]: line for line in lines}


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


def median(values: list[float]) -> float:
    return sorted(values)[len(values) // 2]


def assert_pointer_events_match_the_sample(events: list[dict], crawler) -> None:
    """合成指针与滚轮对照 Mac 触控板样本（#84）。时长与手势的计划值取自账本的指针记录，严格落在 human_pointer 的
    样本区间内；页面实测值另含 CDP 派发与渲染进程排队，CI 负载高时单次会远超计划（#96），只核对先后与中位数的宽窗口。
    与负载无关的结构逐个核对：pressure 0.5 与 1×1 接触面、整数点击坐标、落点不在几何中心、deltaMode 0、整数 deltaY。"""
    pointers = [
        (line["action"], line["pointer"]) for line in ledger_lines(crawler)
        if line.get("kind") == "action" and line.get("pointer")
    ]
    # 计划：每个移动事件前至少等一帧（带抖动；并入的小位移与段间停顿只会更长），按住时长在样本区间内。
    for _action, pointer in pointers:
        if "moves" in pointer:
            assert pointer["approach_ms"] >= pointer["moves"] * FRAME_MS * (1 - FRAME_JITTER) - 0.5, pointer
    presses = [(action, pointer["hold_ms"]) for action, pointer in pointers if "hold_ms" in pointer]
    assert presses and all(HOLD_MS[0] <= hold <= HOLD_MS[1] for _action, hold in presses), presses
    # 计划：列表滚动的手势各不超过 400px、位移之和等于动作的 delta_y，不小于 8px 的手势至少 8 个事件。
    scan_scrolls = [line for line in actions(crawler, "scroll") if "delta_y" in line["target"]]
    assert scan_scrolls
    for line in scan_scrolls:
        planned = line["pointer"]["gestures"]
        assert sum(gesture["delta_y"] for gesture in planned) == line["target"]["delta_y"]
        for gesture in planned:
            assert abs(gesture["delta_y"]) <= GESTURE_MAX_PX, gesture
            assert abs(gesture["delta_y"]) < GESTURE_MIN_EVENTS or gesture["events"] >= GESTURE_MIN_EVENTS, gesture

    events = sorted(events, key=lambda event: event["t"])
    moves = [event for event in events if event["type"] == "move"]
    gaps = [later["t"] - earlier["t"] for earlier, later in zip(moves, moves[1:]) if later["doc"] == earlier["doc"]]
    # 实测：负载高时 Chromium 把 pointermove 并到帧上、CDP 往返变长，间隔变成整帧或更长。中位数窗口取半帧到 4 帧：
    # 不等待的连发仍低于下限，要过半的移动各晚 3 帧以上才会越过上限。
    assert len(gaps) > 50 and FRAME_MS / 2 <= median(gaps) <= FRAME_MS * 4, median(gaps)
    downs = [event for event in events if event["type"] == "down"]
    ups = [event for event in events if event["type"] == "up"]
    assert [event["type"] for event in events if event["type"] in {"down", "up"}] == ["down", "up"] * len(downs)
    # 每次按下都来自账本里的一个计划；筛选以外的计划都会按下（悬停已打开面板时不按入口，按“图文”前面板已收起时不按）。
    assert sum(action != "apply_filter" for action, _hold in presses) <= len(downs) <= len(presses)
    for down in downs:
        assert down["pressure"] == 0.5 and down["width"] == 1 and down["height"] == 1
        assert float(down["x"]).is_integer() and float(down["y"]).is_integer()
    # 实测：pointerdown 到 pointerup 是按住时长加派发与排队，负载只会拉长、不会缩短。逐个只要求 up 晚于 down；中位数
    # 下限比计划下限低 10ms（计时器提前触发与时间戳粗化的余量），上限取计划上限的两倍，要过半的按压各多出 135ms 以上
    # 才会越界，没有按住（约 0ms）仍低于下限。
    holds = [up["t"] - down["t"] for down, up in zip(downs, ups)]
    assert all(hold > 0 for hold in holds), holds
    assert HOLD_MS[0] - 10 <= median(holds) <= HOLD_MS[1] * 2, holds
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
    assert gestures and {event["mode"] for event in wheels} == {0}
    assert all(float(event["dy"]).is_integer() for event in wheels)
    # 实测：渲染进程忙时连续的滚轮事件会合并（位移相加、事件变少），中途停顿超过 200ms 会把一个手势切成两段，只看
    # 中位数。120px 以上的计划手势约有 26 个以上事件、逐手势 |deltaY| 中位数约在 10 以内；要过半的手势被合并到 8 个
    # 事件以下，或过半的事件超过计划峰值上限 50 才会越界，一次滚完或少数几大步仍会被发现。
    sizes = [len(gesture) for gesture in gestures]
    assert median(sizes) >= GESTURE_MIN_EVENTS, sizes
    assert median([abs(event["dy"]) for event in wheels]) <= GESTURE_PEAK_MAX


def assert_staged_png(site: XhsOfflineSite, crawler, note_id: str, count: int) -> None:
    staged = crawler.staged[note_id]
    assert [item["url"] for item in staged] == [image_url(note_id, index) for index in range(count)]
    for item in staged:
        assert item["content"].startswith(PNG_SIGNATURE)
        assert item["content"] == site.image_body(item["url"])


@pytest.mark.asyncio
async def test_full_scan_in_local_chromium_passes_the_visit_gate(monkeypatch, tmp_path):
    """AI 对话布局下 3 页（每页 12 项、共 36 项）末页 has_more=false。对话栏头部没有关闭按钮（#99）：写一条
    `ai_chat_close` 诊断，不点击，按 AI 对话布局完成全程。等延迟渲染、位于对话栏内的筛选入口（标签行里另有一个隐藏的
    div.filter），面板悬停
    开合：光标移入入口即打开、不按入口，按文字点选“图文”的真标签（不是重叠的 aria-hidden 副本），把光标移出入口与
    面板即收起（面板从 DOM 移除）；列表在页头下方的容器内滚动、
    window 不动，滚轮只作用在结果列表上，对话栏里与结果 ID 重叠的笔记链接不计入、不点击，点卡片都命中封面；
    虚拟列表回收下的全量扫描，第 1 页中间的相关搜索卡（hot_query，没有封面）跳过、不停止；浮层详情与翻图截获 PNG
    （首图先于 feed 到达）；作者链接按作者 ID 定位：旧位置的隐藏作者名与评论区（含作者本人的评论）都不点，点中
    interaction-container 里的作者名打开 target=_blank 作者页（同一作者只点一次），经真实门禁通过。"""
    pages = [[search_item(f"p{number}-{index:02d}") for index in range(12)] for number in (1, 2, 3)]
    pages[0][6] = search_item("p1-06", card_type="video")
    images = {"p1-02": 3, "p2-05": 2, "p3-09": 1}
    site = XhsOfflineSite(KEYWORD, pages, hot_query_at=6, hover_opens_panel=True, ai_chat_close="missing")
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
            visible_entries: Array.from(document.querySelectorAll('div.filter'))
                .filter((node) => node.getBoundingClientRect().width > 0).length,
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
        "ai_chat": 1, "entries": 2, "visible_entries": 1, "layout_entries": 1, "chat_entries": 1,
        "panels": 0, "entry_active": 0, "chat_scroll_top": 0,
    }
    # 没有关闭按钮：不写 dismiss_ai_chat、不点，诊断与日志各一条，按 AI 对话布局继续；“问点点 ai”从未被点。
    assert actions(crawler, "dismiss_ai_chat") == []
    assert audit["aiChatCloseClicks"] == audit["aiChatClosed"] == audit["askAiClicks"] == 0
    [chat_failure] = [item for item in crawler._navigation_diagnostics if item["stage"] == "ai_chat_close"]
    assert chat_failure["outcome"] == "button_not_clickable" and chat_failure["clicks"] == 0
    assert chat_failure["with_ai_chat"] is True and chat_failure["button_count"] == 0
    assert chat_failure["ai_chat_inner"] is not None and chat_failure["button"] is None
    assert sum("AI chat panel not closed; continuing in the AI chat layout" in line for line in log_lines) == 1
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
    # 面板悬停开合（#81）：筛选一次成功，收起由移开光标完成，没有回退到按入口；点选“图文”之后面板只收起一次、不再
    # 打开。接近入口时越过入口可能开 → 关 → 再开（人越过目标时也会这样），不在此断言。
    assert [session["filtered"] for session in audit["sessions"]] == [False, True]
    assert filter_targets(crawler) == [("panel_open", None), ("note_type", None), ("panel_close", "pointer_leave")]
    panel_events = audit["panelEvents"]
    option = panel_events.index("option")
    assert panel_events[option - 1] == "hover_open" and panel_events[option:] == ["option", "leave_close"]
    assert not {"press_open", "press_close"} & set(panel_events), panel_events
    assert "[XiaoHongShuCrawler.scan] Filter panel opened on hover; entry not pressed" in log_lines
    assert not any("stayed open after the pointer left" in line for line in log_lines)
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

    # #94：浮层底部互动栏与评论区按实地结构读数；评论区里评论的点赞（同为 .like-wrapper span.count、在互动栏之前）
    # 不被读到。搜索一侧为登记卡片所用的目标会话条目（分享键 shared_count），页码为笔记所在页。
    lines = engagement_lines(tmp_path)
    assert sorted(lines) == sorted(images)
    for note_id, line in lines.items():
        assert (line["mode"], line["read"], line["bar"], line["left"]) == ("discovery", "ok", True, True)
        assert (line["search"]["source"], line["search"]["page"]) == ("search_results", int(note_id[1]))
        assert line["search"]["search_seq"] < line["search"]["feed_seq"] and line["search"]["lag_seconds"] >= 0
        assert line["counts"] == {
            "liked_count": {
                "feed": "number", "feed_value": 1, "search": "number", "search_value": 1, "zero_by_search": False,
                "page": "number", "page_value": 1,
            },
            "collected_count": {
                "feed": "number", "feed_value": 2, "search": "number", "search_value": 2, "zero_by_search": False,
                "page": "number", "page_value": 2,
            },
            "comment_count": {
                "feed": "number", "feed_value": 3, "search": "number", "search_value": 3, "zero_by_search": False,
                "page": "number", "page_value": 3,
            },
            "share_count": {
                "feed": "number", "feed_value": 4, "search": "number", "search_value": 4, "zero_by_search": False,
                "page": "icon_only",
            },
        }
        assert line["comments"] == {"section": "total", "total": 3, "container": True, "empty_state": False}


# #94：操作人 2026-10-18 实地查看的互动栏与评论区结构。评论区在前、互动栏在底部；评论的点赞同样是
# .like-wrapper span.count。numbers 为有数字的笔记（点赞另带 like-active，含“万”），zero 为计数为 0 时的显示，
# missing 缺评论与分享（互动栏外另有一个 .chat-wrapper），hidden 为不可见的根。
ENGAGEMENT_FIELD_HTML = """
<div id="numbers"><div class="interaction-container">
  <div class="comments-el"><div class="comments-container">
    <div class="total">共 12 条评论</div>
    <div class="list-container"><div class="parent-comment"><div class="comment-item"><div class="right">
      <span class="like-wrapper"><span class="count">5</span></span>
    </div></div></div></div>
  </div></div>
  <div class="buttons engage-bar-style">
    <div class="left">
      <span class="like-wrapper like-active"><span class="like-lottie"></span><span class="count">1.2万</span></span>
      <span class="collect-wrapper"><span class="count">36</span></span>
      <span class="chat-wrapper"><span class="count">12</span></span>
    </div>
    <div class="share-wrapper"><svg class="share-icon" width="16" height="16"></svg></div>
  </div>
</div></div>
<div id="zero"><div class="interaction-container">
  <div class="comments-el"><div class="comments-container">
    <div class="no-comments-text">这是一片荒地 <span class="focus-comments">点击评论</span></div>
  </div></div>
  <div class="buttons engage-bar-style">
    <div class="left">
      <span class="like-wrapper"><span class="count"></span></span>
      <span class="collect-wrapper"><span class="count">收藏</span></span>
      <span class="chat-wrapper"><span class="count">评论</span></span>
    </div>
    <div class="share-wrapper"><svg class="share-icon" width="16" height="16"></svg></div>
  </div>
</div></div>
<div id="missing">
  <div class="header"><span class="chat-wrapper"><span class="count">9</span></span></div>
  <div class="buttons engage-bar-style"><div class="left">
    <span class="like-wrapper"><span class="count">7</span></span>
    <span class="collect-wrapper"></span>
  </div></div>
</div>
<div id="hidden" style="display: none"><div class="buttons engage-bar-style"></div></div>
"""


@pytest.mark.asyncio
async def test_note_engagement_script_reads_the_field_structure(tmp_path):
    """真实 Chromium 中执行 note_engagement 脚本：只读互动栏 .left 内的计数，空文字、文字标签、缺失元素与不可见的根
    分别归类；输出只有文字截断、个数与布尔值。"""
    site = XhsOfflineSite(KEYWORD, [[]])
    async with replay_browser(site) as (_context, page, _pointer_events):
        await page.set_content(ENGAGEMENT_FIELD_HTML)
        reads = {
            view: await page.evaluate(DOM_SCRIPTS["note_engagement"], {"roots": [f"#{view}"]})
            for view in ("numbers", "zero", "missing", "hidden")
        }

    assert reads["numbers"] == {
        "view_visible": True, "bar": True, "left": True,
        "like": {"text": "1.2万", "matches": 1, "wrappers": 2},
        "collect": {"text": "36", "matches": 1, "wrappers": 1},
        "chat": {"text": "12", "matches": 1, "wrappers": 1},
        "share": {"button": True, "text": None, "matches": 0, "wrappers": 1},
        "comments": {"container": True, "total_text": "共 12 条评论", "empty_state": False},
    }
    assert reads["hidden"] == {"view_visible": False}
    feed = {"liked_count": "", "collected_count": "", "comment_count": "", "share_count": ""}
    # 这里只验页面读数，没有搜索来源（搜索一侧记 no_source）。
    zero = engagement_observation("zero", mode="discovery", interact=feed, page=reads["zero"])
    assert zero["counts"] == {
        "liked_count": {"feed": "empty_string", "search": "no_source", "zero_by_search": False, "page": "empty"},
        "collected_count": {"feed": "empty_string", "search": "no_source", "zero_by_search": False, "page": "label"},
        "comment_count": {"feed": "empty_string", "search": "no_source", "zero_by_search": False, "page": "label"},
        "share_count": {"feed": "empty_string", "search": "no_source", "zero_by_search": False, "page": "icon_only"},
    }
    assert zero["comments"] == {"section": "empty_state", "container": True, "empty_state": True}
    missing = engagement_observation("missing", mode="discovery", interact=feed, page=reads["missing"])
    assert missing["counts"]["liked_count"] == {
        "feed": "empty_string", "search": "no_source", "zero_by_search": False, "page": "number", "page_value": 7,
    }
    assert missing["counts"]["collected_count"] == {
        "feed": "empty_string", "search": "no_source", "zero_by_search": False, "page": "absent", "matches": 0,
        "wrappers": 1,
    }
    assert missing["counts"]["comment_count"] == {
        "feed": "empty_string", "search": "no_source", "zero_by_search": False, "page": "absent", "matches": 0,
        "wrappers": 1,
    }
    assert missing["counts"]["share_count"] == {
        "feed": "empty_string", "search": "no_source", "zero_by_search": False, "page": "absent", "matches": 0,
        "wrappers": 0,
    }
    assert missing["comments"] == {"section": "not_rendered", "container": False, "empty_state": False}
    hidden = engagement_observation("hidden", mode="discovery", interact=feed, page=reads["hidden"])
    assert hidden["read"] == "view_missing" and hidden["comments"] == {"section": "unread"}


@pytest.mark.asyncio
async def test_feed_verification_layer_then_redo_reuses_the_cached_first_image(monkeypatch, tmp_path):
    """feed 返回 461：站点显示验证层（captcha iframe），完成后整篇重做；同一文档重开时首图走内存、没有新的
    响应事件，记录引用第一次尝试截获的图片访问，门禁照常通过。进入结果页后先关闭对话栏（#99），之后为 V16 整页滚动。
    面板只随点击入口切换（默认）：按入口打开，移开光标收不起，回退为按入口收起。"""
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
    assert len(actions(crawler, "dismiss_ai_chat")) == 1 and audit["aiChatClosed"] == 1 and audit["askAiClicks"] == 0
    assert audit["captchaShown"] == 1 and layers == 0
    assert filter_targets(crawler) == [
        ("panel_open", None), ("note_type", None), ("panel_close", "pointer_leave"), ("panel_close", "entry_press"),
    ]
    assert audit["panelEvents"] == ["press_open", "option", "press_close"]
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


APPROACH_LINE = re.compile(
    r"Filter option approach: hovered=(True|False), entry=\((-?\d+), (-?\d+), (-?\d+), (-?\d+)\), "
    r"entry_container=\((-?\d+), (-?\d+), (-?\d+), (-?\d+)\), panel=\((-?\d+), (-?\d+), (-?\d+), (-?\d+)\), "
    r"corridor=(None|\((-?\d+), (-?\d+), (-?\d+), (-?\d+)\)), container_gap=(-?[\d.]+)"
)


def approach_geometry(log_lines: list[str]) -> dict:
    """点选“图文”时那行日志里的框（left、top、right、bottom）与 div 到面板的间隙。"""
    [line] = [line for line in log_lines if "Filter option approach:" in line]
    match = APPROACH_LINE.search(line)
    assert match, line
    values = match.groups()
    boxes = [tuple(int(value) for value in values[start:start + 4]) for start in (1, 5, 9)]
    corridor = None if values[13] == "None" else tuple(int(value) for value in values[14:18])
    return {
        "hovered": values[0] == "True", "entry": boxes[0], "entry_container": boxes[1], "panel": boxes[2],
        "corridor": corridor, "gap": float(values[18]),
    }


def assert_v16_gap_corridor(geometry: dict) -> None:
    """关闭对话栏后的 V16 几何（同侦察）：div.filter 84×40、“筛选” span 高 20 在其中，面板在 div 下方 10px，走廊为
    span 下沿到面板上沿、span 与面板横向重叠的那段。"""
    span, container, panel = geometry["entry"], geometry["entry_container"], geometry["panel"]
    assert (container[2] - container[0], container[3] - container[1]) == (84, 40), geometry
    assert span[3] - span[1] == 20 and container[1] <= span[1] < span[3] <= container[3], geometry
    assert geometry["gap"] == 10.0 and panel[1] == container[3] + 10, geometry
    assert geometry["corridor"] == (max(span[0], panel[0]), span[3], min(span[2], panel[2]), panel[1]), geometry


@pytest.mark.asyncio
async def test_tall_hover_panel_is_left_sideways_when_there_is_no_room_below(monkeypatch, tmp_path):
    """#81 第 7 轮：进入结果页后关闭对话栏（#99），按 V16 布局筛选。面板很高（至少 560px，下沿约在 726px），列表区域
    （纵向约 273–743px）在面板下方放不下落点，改取面板左侧；移开光标即收起，不按入口，点选“图文”之后面板只收起
    一次、不再打开。V16 的入口 div.filter 与面板之间有 10px 间隙，“筛选” span 到面板 20px：按 div 判定加间隙走廊，
    从入口到“图文”（大致在入口正下方）的接近途中面板一直开着。"""
    items = [search_item(f"t-{index}") for index in range(8)]
    site = XhsOfflineSite(KEYWORD, [items], hover_opens_panel=True, panel_min_height=560)
    known = [item["id"] for item in items]

    async with replay_browser(site) as (context, page, pointer_events):
        crawler, state_path = prepare_crawler(monkeypatch, tmp_path, context, page, known=known)
        await page.goto(EXPLORE_URL)
        with crawler_log_lines() as log_lines:
            await crawler.search()
        audit = await page.evaluate("() => window.__tpcOffline")
        ai_chat = await page.evaluate("() => document.querySelectorAll('.with-ai-chat, .ai-chat-section').length")

    assert len(actions(crawler, "dismiss_ai_chat")) == 1 and audit["aiChatClosed"] == 1 and ai_chat == 0
    assert [session["filtered"] for session in audit["sessions"]] == [False, True]
    assert filter_targets(crawler) == [("panel_open", None), ("note_type", None), ("panel_close", "pointer_leave")]
    panel_events = audit["panelEvents"]
    option = panel_events.index("option")
    assert panel_events[option - 1] == "hover_open" and panel_events[option:] == ["option", "leave_close"]
    assert not {"press_open", "press_close"} & set(panel_events), panel_events
    geometry = approach_geometry(log_lines)
    assert geometry["hovered"] is True
    assert_v16_gap_corridor(geometry)
    assert not any("leave area unavailable" in line for line in log_lines)
    assert not any("stayed open after the pointer left" in line for line in log_lines)
    assert audit["askAiClicks"] == 0 and audit["strayClicks"] == []
    assert last_stop(state_path)["stop_reason"] == "source_exhausted"
    assert site.unexpected == [] and site.escaped() == []


@pytest.mark.asyncio
async def test_ai_chat_is_closed_then_the_scan_scrolls_the_whole_page_and_passes_the_gate(monkeypatch, tmp_path):
    """#99：AI 对话布局进入结果页，点关闭按钮（写 dismiss_ai_chat、带 pointer），对话栏与分隔条移除，列表改为整页滚动；
    前端以回车会话的同一 search_id 续页第 2、3 页，记为非目标访问、trigger 为关闭动作；之后按 V16 布局点选“图文”，
    两页目标会话照常扫完，“问点点 ai”从未被点，门禁通过。"""
    pages = [[search_item(f"c{number}-{index:02d}") for index in range(12)] for number in (1, 2)]
    unknown = ["c1-05", "c2-07"]
    site = XhsOfflineSite(KEYWORD, pages)
    known = [item["id"] for items in pages for item in items if item["id"] not in unknown]

    async with replay_browser(site) as (context, page, pointer_events):
        crawler, state_path = prepare_crawler(monkeypatch, tmp_path, context, page, known=known)
        await page.goto(EXPLORE_URL)
        with crawler_log_lines() as log_lines:
            await crawler.search()
        audit = await page.evaluate("() => window.__tpcOffline")
        layout = await page.evaluate("""() => ({
            ai_chat: document.querySelectorAll('.ai-feeds-page.with-ai-chat').length,
            chat_sections: document.querySelectorAll('.ai-chat-section, .ai-chat-section-divider').length,
            close_buttons: document.querySelectorAll('button.xhs-ai-chat-header__close').length,
            top_entries: document.querySelectorAll('.search-layout > .search-layout__top > div.filter').length,
            ask_ai_tabs: document.querySelectorAll('.search-layout__top .ai-tab').length,
        })""")
        metrics = await page.evaluate(DOM_SCRIPTS["scroll_metrics"])

    stopped = last_stop(state_path)
    assert stopped["stop_reason"] == "source_exhausted" and stopped["source_page"] == 2
    assert [record["note_id"] for record in crawler.records] == unknown
    [dismiss] = actions(crawler, "dismiss_ai_chat")
    assert dismiss["target"] == {"control": "ai_chat_close"}
    assert {"x_ratio", "y_ratio", "approach_ms", "moves", "settle_ms", "hold_ms"} <= set(dismiss["pointer"])
    assert audit["aiChatCloseClicks"] == audit["aiChatClosed"] == 1 and audit["aiChatReopened"] == 0
    assert audit["askAiClicks"] == 0 and audit["strayClicks"] == []
    assert layout == {"ai_chat": 0, "chat_sections": 0, "close_buttons": 0, "top_entries": 1, "ask_ai_tabs": 1}
    # 关闭后整页滚动：window 动过，容器没有；扫描结束时的读数为文档滚动。
    assert audit["maxWindowScrollY"] > 0 and audit["maxListScrollTop"] == 0
    assert metrics["scroll_container"] == "document" and metrics["view_rect"]["top"] == 0
    assert metrics["with_ai_chat"] is False and metrics["top_row_rect"] is None  # 扫完时标签行已滚出可见区
    # 续页：回车会话（不带 filters）的同一 search_id 第 2、3 页，非目标访问，trigger 为关闭动作；目标会话由点选“图文”触发。
    general = [body for body in site.search_requests if "filters" not in body]
    assert [body["page"] for body in general] == [1, 2, 3] and len({body["search_id"] for body in general}) == 1
    searches = [line for line in ledger_lines(crawler) if line.get("kind") == "visit" and line["stage"] == "search_results"]
    assert [(line["is_target"], line["search_page"]) for line in searches] == [
        (False, 1), (False, 2), (False, 3), (True, 1), (True, 2),
    ]
    assert [line["trigger"] for line in searches[1:3]] == [dismiss["id"], dismiss["id"]]
    assert visit(crawler, searches[3]["trigger"])["target"]["filter"] == "note_type"
    assert any(
        line.startswith("[XiaoHongShuCrawler.scan] AI chat panel close: clicks=1, confirmed=True, scroll_container=document")
        for line in log_lines
    )
    assert not any("AI chat panel not closed" in line or "reappeared" in line for line in log_lines)
    # V16 下按入口打开面板（默认只随点击切换），移开光标收不起再按入口；入口与面板之间的走廊照常算出。
    assert filter_targets(crawler) == [
        ("panel_open", None), ("note_type", None), ("panel_close", "pointer_leave"), ("panel_close", "entry_press"),
    ]
    assert audit["panelEvents"] == ["press_open", "option", "press_close"]
    geometry = approach_geometry(log_lines)
    assert geometry["hovered"] is False
    assert_v16_gap_corridor(geometry)
    assert audit["coverClicks"] == len(actions(crawler, "click_card")) == audit["overlaysOpened"] == 2

    result = run_gate(crawler, state_path, tmp_path)
    assert result["problem_counts"] == {}
    assert result["content_visit_ok"] is True and result["checked_records"] == 2
    assert site.unexpected == [] and site.escaped() == []


@pytest.mark.asyncio
async def test_frontend_self_load_when_viewport_is_not_filled_passes_the_gate(monkeypatch, tmp_path):
    """V16 布局（整页滚动）下目标会话第 1 页只有一行、填不满视口：站点不经滚动自行请求第 2 页，门禁计入
    frontend_self_loads 而不失败；筛选入口在 .search-layout 内，其前的零尺寸副本被跳过。进入时就没有对话栏（#99）：
    不点、不写动作，只记一行日志。"""
    pages = [[search_item(f"s1-{index}") for index in range(4)], [search_item(f"s2-{index}") for index in range(8)]]
    site = XhsOfflineSite(KEYWORD, pages, layout="v16")
    unknown = {"s1-1", "s2-4"}
    known = [item["id"] for items in pages for item in items if item["id"] not in unknown]

    async with replay_browser(site) as (context, page, pointer_events):
        crawler, state_path = prepare_crawler(monkeypatch, tmp_path, context, page, known=known)
        await page.goto(EXPLORE_URL)
        with crawler_log_lines() as log_lines:
            await crawler.search()
        audit = await page.evaluate("() => window.__tpcOffline")
        filter_nodes = await page.evaluate("() => document.querySelectorAll('.search-layout div.filter').length")
        ai_chat = await page.evaluate("() => document.querySelectorAll('.with-ai-chat').length")
        metrics = await page.evaluate(DOM_SCRIPTS["scroll_metrics"])

    assert audit["selfLoads"] >= 1
    assert filter_nodes == 2 and ai_chat == 0  # 零尺寸副本在真实入口之前，入口延迟插入
    assert actions(crawler, "dismiss_ai_chat") == [] and audit["aiChatCloseClicks"] == 0
    assert [line for line in log_lines if "AI chat panel" in line] == [
        "[XiaoHongShuCrawler.scan] AI chat panel absent; already V16 layout"
    ]
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
