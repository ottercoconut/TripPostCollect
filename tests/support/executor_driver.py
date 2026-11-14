"""执行器离线驱动：只替换进程、浏览器、网络与时钟边界，观测内容事务、图片晋升与发现提交的顺序与故障。

`drive` 运行 `scripts/mediacrawler_crawl.py` 的 `_run_main` 全链路；`offline_guard` 需由使用模块显式引入
（`offline_guard = executor_driver.offline_guard`）才对该模块生效。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import importlib
import importlib.util
import inspect
import io
import json
from pathlib import Path
import random
import re
import socket
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

from PIL import Image
import pytest

from trippostcollect.artifacts.image_candidates import content_image_candidates
from trippostcollect.artifacts.image_manifest import ImageManifestEntry, write_manifest_atomic
from trippostcollect.artifacts.image_materialization import write_staging_image
from trippostcollect.core.execution_state import FrozenExecutionState
from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.platforms.bilibili import core as bilibili_core
from trippostcollect.platforms.bilibili.models import BilibiliSearchPage
from trippostcollect.runtime import process
from trippostcollect.runtime.visit_ledger import VisitLedger, content_visits_path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
executor = importlib.import_module("mediacrawler_crawl")


def strict_patch(monkeypatch, target, name, value):
    """不存在的属性必须失败，禁止默默增加一个没有被调用的 mock。"""
    assert hasattr(target, name), f"patch 目标不存在：{target}.{name}"
    monkeypatch.setattr(target, name, value, raising=True)


# 执行器端口字段：新实现经 ports 取得，装配在脚本入口调用时读取脚本全局名。
PORT_FIELDS = {"ensure_prerequisites", "run_bilibili_article_search"}
# 执行器各函数的实际定义模块（原 T11 迁移目标）；薄包装不经脚本，直接定位到这里读取全局名。
TARGETS = {
    name: target
    for target, names in (
        ("application/reporting.py", (
            "item_type_from_path", "truncate", "extract_sample", "summarize_jsonl", "summarize_output",
            "summarize_output_with_progress", "write_json_with_progress", "terminal_summary_envelope",
            "collect_behavior_validation", "latest_platform_result_counts", "write_markdown",
        )),
        ("db/content.py", (
            "ensure_web_schema", "find_existing_post", "upsert_web_post", "FormalImportBeforeCommitError",
            "FormalImportBeforeCommitError.__init__", "commit_formal_import", "import_valid_records",
        )),
        ("application/collection.py", (
            "stable_douyin_search_id", "effective_discovery_checkpoint_event", "persist_discovery_checkpoint",
            "load_pagination_evidence", "attach_skipped_candidate_evidence", "collect_formal_records",
            "import_valid_records_with_media_rollback", "_run_platform_without_policy",
            "effective_attempt_exit_code", "run_platform", "apply_formal_completion_gates",
            "formal_import_gate_met", "formal_image_promotion_allowed", "_run_main",
        )),
        ("artifacts/paths.py", (
            "resolve_media_root",
        )),
        ("artifacts/formal_images.py", (
            "_project_relative_evidence_path", "_load_manifest_with_evidence", "_staging_root_for_manifest_entry",
            "rollback_newly_promoted_images", "formal_media_persistence_lock", "_validated_manifest_rows_for_post",
            "materialize_formal_record_images",
        )),
        ("application/failures.py", (
            "runtime_blocker_stop_reason", "runtime_blocker_from_terminal_event",
            "runtime_blocker_from_pagination_evidence", "latest_runtime_blocker", "apply_runtime_blocker",
        )),
    )
    for name in names
}


def owner(function, name=None):
    """找到函数真正读取全局变量的模块；执行器函数按 TARGETS 定位，不经脚本薄包装。"""
    migrated = None
    if function in TARGETS and name not in PORT_FIELDS:
        module_name = "trippostcollect." + TARGETS[function].removesuffix(".py").replace("/", ".")
        if importlib.util.find_spec(module_name) is not None:
            migrated = importlib.import_module(module_name)
    if migrated is not None and hasattr(migrated, function):
        target = migrated
        implementation = inspect.unwrap(getattr(target, function))
    else:
        implementation = inspect.unwrap(getattr(executor, function))
        target = sys.modules[implementation.__module__]
    assert implementation.__globals__ is vars(target)
    return target


def global_reads(code):
    """递归收集代码对象（含嵌套函数、生成器）读取的全局/属性名。"""
    names = set(code.co_names)
    for constant in code.co_consts:
        if inspect.iscode(constant):
            names |= global_reads(constant)
    return names


def binding(monkeypatch, function, name, value):
    target = owner(function, name)
    reader = inspect.unwrap(getattr(executor if target is executor else target, function))
    # 死 patch 防线：被替换的名字必须由实际执行的函数读取，否则 mock 不生效。
    assert name in global_reads(reader.__code__), f"patch 不会生效：{target.__name__}.{function} 不读取 {name}"
    strict_patch(monkeypatch, target, name, value)


def forbid(*args, **kwargs):
    pytest.fail("离线驱动漏拦：禁止启动真实进程、浏览器或网络")


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    strict_patch(monkeypatch, subprocess, "Popen", forbid)
    strict_patch(monkeypatch, process, "run_command", forbid)
    strict_patch(monkeypatch, socket.socket, "connect", forbid)
    strict_patch(monkeypatch, socket.socket, "connect_ex", forbid)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 1, 1, 2, 3, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def record_for(platform, number):
    post_id = str(100 + number)
    url = f"https://images.example.test/{platform}/{post_id}.png"
    record = {
        "title": "青岛游记", "content": "青岛海边完整正文", "content_text": "青岛海边完整正文",
        "desc": "青岛海边完整正文", "user_id": "author-1", "nickname": "作者",
        "published_at": "2026-09-20T12:00:00+08:00", "followers_count": 42,
        "followers_observed": True, "liked_count": 3, "comment_count": 2,
        "comments_count": 2, "shared_count": 1, "share_count": 1,
        "collected_count": 4, "view_count": 6, "voteup_count": 3,
        "source_keyword": "青岛旅游", "content_detail_status": "detail_observed",
        "content_detail_source": {"weibo": "mobile_detail", "douyin": "aweme_detail",
                                  "zhihu": "answer_detail", "xhs": "note_detail",
                                  "bilibili": "article_view_api"}[platform],
        "author_followers_source": {"weibo": "search_author", "zhihu": "author_page",
                                    "bilibili": "relation_stat", "xhs": "creator_profile",
                                    "douyin": "creator_profile"}[platform],
    }
    if platform == "bilibili":
        record.update(content_id=post_id, content_type="article", image_urls=[url],
                      content_images_detail_status="detail_observed")
    elif platform == "weibo":
        record.update(note_id=post_id, image_list=[url], image_list_source="mblog.pics")
    elif platform == "douyin":
        record.update(aweme_id=post_id, aweme_type=68, note_download_url=url,
                      image_assets=[{"url": url, "uri": post_id}])
    elif platform == "zhihu":
        record.update(content_id=post_id, content_type="answer", question_id="80", image_list=[url])
    else:
        record.update(note_id=post_id, type="normal", image_list=[{"url_default": url}])
    return record


def behavior_for(platform):
    return {
        "status": "completed", "profile": "xhs_guarded" if platform == "xhs" else "social_high_risk",
        "url": "https://example.test/search?keyword=青岛旅游",
        "events": [{"event": "pause"}, {"event": "mouse_moves"},
                   {"event": "human_scroll_complete", "effective_passes": 1}],
        "page_readiness": {"ready": True},
        "runtime_fingerprint": {"webdriver": False, "languages": ["zh-CN"], "platform": "MacIntel", "visibility_state": "visible",
                                "user_agent": "离线驱动", "viewport": {"width": 1280, "height": 900}},
        "request_pacing_events": [{"stage": s} for s in ("search_results", "note_detail", "creator_profile")],
        "continuity_events": [{"stage": "search_results", "status": "completed"}],
    }


XHS_LEDGER_ID = "71" * 16
XHS_LEDGER_TIME = "2026-10-01T01:02:03+00:00"


def write_xhs_visit_ledger(behavior_path, records, entries):
    """#71 合成账本：与离线记录、manifest 一致的回车 → 点选“图文” → 逐篇浮层与作者页访问。

    只替代 worker 的取数过程，账本与 `content_visit_refs` 按真实 worker 的写法给出，访问门禁在全链路上照常执行。
    返回目标会话（has_more=false）搜索访问 id，供来源耗尽停止事件引用。
    """
    ledger = VisitLedger.create(
        content_visits_path(behavior_path), platform="xhs", mode="discovery",
        ledger_id=XHS_LEDGER_ID, clock=lambda: XHS_LEDGER_TIME,
    )

    def action(kind, target, from_visit=None):
        return ledger.append("action", seq=ledger.next_seq(), page_role="primary", action=kind, target=target,
                             from_visit=from_visit)

    def visit(stage, rule, source, *, trigger, subject_id=None, **audit):
        return ledger.append("visit", seq=ledger.next_seq(), page_role="primary", recorded_at=XHS_LEDGER_TIME,
                             stage=stage, source=source, rule=rule, endpoint=None,
                             status=200 if source == "page_response" else None, outcome="ok", route="/search_result_ai",
                             query_keys=[], subject_id=subject_id, trigger=trigger, **audit)

    def pause(stage, continuity="n/a"):
        start = ledger.next_seq()
        ledger.append("pause", seq=ledger.next_seq(), page_role="primary", stage=stage, start_seq=start,
                      seconds=1.0, continuity=continuity)

    def search(trigger, *, target, items):
        return visit("search_results", "xhs.search_notes", "page_response", trigger=trigger,
                     search_session="offline-2" if target else "offline-1",
                     filter_signature="target" if target else "none", is_target=target, keyword_matches_target=True,
                     search_page=1, has_more=not target, items=items)

    search(action("press_enter", {}), target=False, items=[])
    target = search(action("apply_filter", {"filter": "note_type"}), target=True,
                    items=[{"id": record["note_id"], "type": "normal"} for record in records])
    pause("search_results", "completed")
    creator = None
    for record in records:
        note_id = record["note_id"]
        click = action("click_card", {"note_id": note_id})
        detail = visit("note_detail", "xhs.feed", "page_response", trigger=click, subject_id=note_id)
        images = [
            visit("note_image", "xhs.note_image", "page_response", trigger=click, subject_id=note_id,
                  asset_key=entry.source_asset_key, sha256=entry.sha256, bytes=entry.size_bytes)
            for entry in entries if entry.platform_post_id == note_id
        ]
        pause("note_detail")
        if creator is None:
            pause("creator_profile")
            author = action("click_author", {"note_id": note_id}, from_visit=detail)
            creator = visit("creator_profile", "creator_page_data", "page_state", trigger=author,
                            subject_id=record["user_id"], url_user_id=record["user_id"])
            action("close_tab", {"note_id": note_id})
        action("close_overlay", {"note_id": note_id})
        record["content_visit_refs"] = {"ledger_id": XHS_LEDGER_ID, "search_results": target, "note_detail": detail,
                                        "creator_profile": creator, "images": images}
    ledger.close()
    return target


ZHIHU_LEDGER_ID = "85" * 16


def write_zhihu_visit_ledger(behavior_path, records, entries):
    """#185 合成账本：搜索截获、点击详情、点击作者、CDN 图片，供知乎 content_visit_ok。"""

    ledger = VisitLedger.create(
        content_visits_path(behavior_path), platform="zhihu", mode="discovery",
        ledger_id=ZHIHU_LEDGER_ID, clock=lambda: XHS_LEDGER_TIME,
    )

    def action(kind, target, from_visit=None, page_role="primary"):
        return ledger.append(
            "action", seq=ledger.next_seq(), page_role=page_role, action=kind,
            target=target, from_visit=from_visit,
        )

    def visit(stage, rule, source, *, trigger, subject_id=None, page_role="primary", **audit):
        return ledger.append(
            "visit", seq=ledger.next_seq(), page_role=page_role, recorded_at=XHS_LEDGER_TIME,
            stage=stage, source=source, rule=rule, endpoint=None,
            status=200 if source != "page_state" else None, outcome="ok",
            subject_id=subject_id, trigger=trigger, **audit,
        )

    def pause(stage, continuity="n/a", page_role="detail"):
        start = ledger.next_seq()
        ledger.append(
            "pause", seq=ledger.next_seq(), page_role=page_role, stage=stage,
            start_seq=start, seconds=1.0, continuity=continuity,
        )

    search = visit(
        "search_results", "zhihu.search_v3", "page_response", trigger=action("navigate", {"route": "/search"}),
        search_session="zhihu-offline", filter_signature="type=content", is_target=True,
        search_page=1, has_more=False,
        items=[{"id": record["content_id"], "type": "answer"} for record in records],
    )
    pause("search_results", "completed", "primary")
    for record in records:
        content_id = record["content_id"]
        token = "author-token"
        record["creator_url_token"] = token
        click = action("click_card", {"content_id": content_id})
        detail = visit(
            "note_detail", "zhihu.initial_data", "page_state", trigger=click,
            subject_id=content_id, page_role="detail",
        )
        pause("note_detail")
        pause("creator_profile")
        author_click = action("click_author", {"content_id": content_id}, from_visit=detail, page_role="detail")
        author = visit(
            "creator_profile", "zhihu.initial_data", "page_state", trigger=author_click,
            subject_id=token, url_user_id=token, page_role="author",
        )
        images = [
            visit(
                "note_image", "zhihu.content_image", "cdn_direct", trigger=click, subject_id=content_id,
                asset_key=entry.source_asset_key, sha256=entry.sha256,
            )
            for entry in entries if entry.platform_post_id == content_id
        ]
        record["content_visit_refs"] = {
            "ledger_id": ZHIHU_LEDGER_ID,
            "search_results": search,
            "note_detail": detail,
            "creator_profile": author,
            "images": images,
        }
    ledger.close()
    return search


def write_worker_artifacts(platform, save_path, behavior_path, state, scenario):
    """落盘真实 JSONL、manifest 和 PNG；仅替代 worker，不替代正式门禁。"""
    folder = save_path / platform
    folder.mkdir(parents=True)
    records, entries = [], []
    for number in (1, 2):
        record = record_for(platform, number)
        if scenario == "missing_required":
            record.pop("user_id")
        if scenario == "followers_unobserved":
            record["followers_observed"] = False
        records.append(record)
        for candidate in content_image_candidates(platform, record):
            image = io.BytesIO()
            Image.new("RGB", (6, 4), color=(number, 20, 30)).save(image, format="PNG")
            staged = write_staging_image([image.getvalue()], staging_root=folder,
                                         relative_stem=f"images/{number}/{candidate.source_index}")
            entries.append(ImageManifestEntry(
                schema_version=1, platform_key=platform, platform_post_id=candidate.platform_post_id,
                image_role=candidate.image_role, source_index=candidate.source_index,
                source_key=candidate.source_key, source_asset_key=candidate.source_asset_key,
                source_url=candidate.source_url, fetch_status="downloaded", attempts=1, http_status=200,
                staging_path=staged.path.relative_to(folder).as_posix(), size_bytes=staged.size_bytes,
                mime_type=staged.mime_type, width=staged.width, height=staged.height,
                sha256=staged.sha256, error_code=None,
            ))
            if scenario == "missing_image":
                staged.path.unlink()
            if platform in {"xhs", "zhihu"}:
                # 访问门禁 R5 按 image_assets 逐张核对。
                record.setdefault("image_assets", []).append({
                    "url": candidate.source_url, "source_index": candidate.source_index,
                    "source_asset_key": candidate.source_asset_key,
                })
    if platform == "xhs":
        source_visit_id = write_xhs_visit_ledger(behavior_path, records, entries)
    elif platform == "zhihu":
        source_visit_id = write_zhihu_visit_ledger(behavior_path, records, entries)
    else:
        source_visit_id = None
    (folder / "search_contents_fixed.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    write_manifest_atomic(folder / "image_manifest.jsonl", entries)
    behavior = behavior_for(platform)
    if scenario == "invalid_behavior":
        behavior["events"] = []
    behavior_path.parent.mkdir(parents=True, exist_ok=True)
    behavior_path.write_text(json.dumps(behavior, ensure_ascii=False))
    details = {
        "platform": platform, "batch_no": 1, "candidate_count": 2, "valid_new_count": 2,
        "source_page": 2, "resume_page": 3, "batch_complete": True, "source_has_more": False,
        "candidate_identities": ["101", "102"], "discovery_phase": "frontier",
        "raw_batch_count": 2, "pages_fetched": 2, "stop_reason": "source_exhausted",
        "stop_detail": "has_more_false",
    }
    if source_visit_id is not None:
        details["source_visit_id"] = source_visit_id
    if scenario.startswith("count_"):
        details["candidate_count"] = int(scenario.split("_")[1])
    if scenario == "many_pages":
        details["pages_fetched"] = details["source_page"] = 100000
    if scenario == "stagnant":
        details["stagnant_batches"] = 100000
    if scenario == "empty_page":
        details["raw_batch_count"] = 0
    if scenario == "unverified_empty_first_page":
        details.update(source_page=1, source_offset=0, source_cursor=None, raw_batch_count=0, stop_detail="empty_page")
    no_stop = scenario in {"missing_stop", "many_pages", "stagnant", "empty_page"} or scenario.startswith("count_")
    if scenario == "pagination_block":
        details.update(stop_reason="runtime_failed", stop_detail="blocked_or_forbidden")
    if scenario == "items_unresolved":
        # 小红书全量扫描停止（#92）：运行级失败，来源未耗尽。
        details.update(source_has_more=True, stop_reason="runtime_failed", stop_detail="xhs_search_items_unresolved")
    state.append_event("adaptive_batch_completed", details)
    if not no_stop:
        state.append_event("adaptive_search_stopped", details)
    if scenario == "terminal_block":
        state.append_event("xhs_runtime_terminal", {
            "phase": "search", "failure_type": "platform_security_limit",
            "stop_reason": "runtime_failed", "stop_detail": "安全限制 300011", "retryable": False,
        })
    return behavior


def normalize(value, root):
    """只归一临时绝对路径与运行时钟；主键、内容、图片字节和顺序不变。"""
    if isinstance(value, bytes):
        return value.replace(str(root).encode(), b"<run>")
    if isinstance(value, str):
        value = value.replace(str(root), "<run>")
        return re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?", "<time>", value)
    if isinstance(value, (list, tuple)):
        return [normalize(v, root) for v in value]
    if isinstance(value, dict):
        return {k: normalize(v, root) for k, v in value.items()}
    return value


def database_snapshot(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                  if r[0] in {"web_posts", "web_post_images", "ctf_captures", "ctf_capture_images"}
                  or "discovery" in r[0]]
        snapshot = {}
        for table in tables:
            columns = list(conn.execute(f'PRAGMA table_info("{table}")'))
            primary = [r["name"] for r in sorted(columns, key=lambda r: r["pk"]) if r["pk"]]
            order = ", ".join(f'"{name}"' for name in primary)
            assert order, table
            snapshot[table] = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}')]
    return snapshot


def drive(root, patch, capsys, platform="weibo", scenario="success", resume_summary=None):
    """替换进程、浏览器、网络与时钟边界并观测 SQL 与故障；不替换收集、晋升、事务或门禁。`resume_summary` 为累计摘要路径。"""
    root.mkdir()
    media = root / "temp/media"
    db = root / "posts.sqlite"
    profile = root / "empty-profile"
    profile.mkdir()
    trace, held_locks = [], []
    state = FrozenExecutionState.create(root / "state.json", run_id="t11", job_key="青岛旅游",
                                        site_key=platform, job_kind="mediacrawler_search", plan={}, frozen_inputs=[])
    patch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state.path))
    argv = ["mediacrawler_crawl.py", "--platforms", platform, "--keyword", "青岛旅游",
            "--db", str(db), "--output-dir", str(root / "batches"), "--media-root", str(media),
            "--download-images", "--xhs-profile-dir", str(profile)]
    if platform == "xhs":
        argv += ["--xhs-account-id", "offline", "--xhs-discovery-target-key", "青岛旅游",
                 "--xhs-discovery-query-fingerprint", "query", "--behavior-profile", "xhs_guarded",
                 "--login-type", "qrcode"]
    else:
        argv += ["--discovery-job-id", "1", "--discovery-query-fingerprint", "query", "--discovery-run-id", "t11"]
    if resume_summary is not None:
        argv += ["--resume-summary", str(resume_summary)]
    strict_patch(patch, sys, "argv", argv)
    args = executor.parse_args()
    assert args.resume_summary == (None if resume_summary is None else str(resume_summary))
    for name in ("zhihu_detail_urls_file", "xhs_detail_urls_file", "xhs_repair_target_ids_file", "repair_targets_file"):
        assert getattr(args, name) is None
    with sqlite3.connect(db) as conn:
        bootstrap_connection(conn, sync_jobs=False)
        conn.execute("INSERT INTO crawl_jobs (id, job_key, site_key, target_url, job_kind, next_run_at) VALUES (1, ?, ?, '', 'mediacrawler_search', '2026-10-01')", ("青岛旅游", platform))
    # 只替换函数体实际读取的全局名；默认参数在定义时已绑定，替换模块全局不会生效，不计为读取。
    for function in ("_run_main", "materialize_formal_record_images", "import_valid_records"):
        for name, replacement in (("PROJECT_ROOT", root), ("datetime", FixedDatetime)):
            if name in global_reads(inspect.unwrap(getattr(owner(function), function)).__code__):
                binding(patch, function, name, replacement)
    for function in ("load_pagination_evidence", "summarize_output"):
        binding(patch, function, "time", SimpleNamespace(monotonic=lambda: 100.0, time=lambda: 1790816523.0, sleep=forbid))
    binding(patch, "_run_main", "ensure_prerequisites", lambda: None)
    resolve = getattr(owner("_run_main"), "resolve_media_root")
    binding(patch, "_run_main", "resolve_media_root",
            lambda value: resolve(value, project_root=root, default_media_root=media))
    binding(patch, "_run_main", "utc_stamp", lambda: "fixed")
    binding(patch, "_run_platform_without_policy", "ROOT", root)
    binding(patch, "_run_platform_without_policy", "discover_cdp_browser_path", lambda: None)
    binding(patch, "_run_platform_without_policy", "export_profile_cookies",
            lambda *a: {"cookie_header": "d_c0=test;z_c0=test", "source": "offline"})
    binding(patch, "_run_platform_without_policy", "cookie_snapshot_path", lambda *a: root / "snapshot.json")
    binding(patch, "run_platform", "clear_site_policy_state", lambda *a: None)
    binding(patch, "run_platform", "record_site_cooldown", forbid)
    strict_patch(patch, random, "uniform", lambda low, high: (low + high) / 2)

    @contextmanager
    def policy(*a, **kw):
        yield {"allowed": True, "disabled": platform == "xhs"}

    binding(patch, "run_platform", "site_request_guard", policy)
    lock = getattr(owner("_run_main"), "formal_media_persistence_lock")

    @contextmanager
    def observed_lock(*, enabled, progress_callback=None):
        acquired = False
        try:
            with lock(enabled=enabled, lock_path=root / "media.lock", progress_callback=progress_callback):
                acquired = enabled
                if enabled:
                    trace.append("media.acquire")
                yield
        finally:
            if acquired:
                trace.append("media.release")

    binding(patch, "_run_main", "formal_media_persistence_lock", observed_lock)
    db_owner = owner("import_valid_records")
    connect = db_owner.connect_db

    def observed_connect(*a, **kw):
        conn = connect(*a, **kw)
        trace.append(("sqlite.busy_timeout", conn.execute("PRAGMA busy_timeout").fetchone()[0]))
        conn.set_trace_callback(lambda sql: trace.append(sql) if sql in {"BEGIN", "BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"} else None)
        return conn

    # 内容事务与发现提交各自读取 connect_db；两处都挂同一观测，保证 ContentCommit/DiscoveryCommit 顺序可比。
    for function in ("import_valid_records", "persist_discovery_checkpoint"):
        binding(patch, function, "connect_db", observed_connect)
    upsert = db_owner.upsert_web_post
    calls = 0
    failure = executor.XhsRuntimeSupervisionError("监督失败") if scenario == "supervision" else RuntimeError("提交探测失败")

    def observed_upsert(*a, **kw):
        nonlocal calls
        calls += 1
        trace.append(f"content.upsert.{calls}")
        result = upsert(*a, **kw)
        if calls == 2 and scenario in {"upsert_failure", "supervision"}:
            raise failure
        return result

    strict_patch(patch, db_owner, "upsert_web_post", observed_upsert)
    commit = db_owner.commit_formal_import

    def observed_commit(conn):
        trace.append("content.commit.enter")
        if scenario != "commit_before":
            commit(conn)
        trace.append(f"content.in_transaction.{conn.in_transaction}")
        if scenario in {"commit_before", "commit_after"}:
            raise failure
        trace.append("content.commit.exit")

    strict_patch(patch, db_owner, "commit_formal_import", observed_commit)
    imported = getattr(owner("import_valid_records_with_media_rollback"), "import_valid_records")

    def observed_import(*a, **kw):
        try:
            return imported(*a, **kw)
        except BaseException as exc:
            trace.append(("content.exception", type(exc).__name__, exc is failure))
            raise

    binding(patch, "import_valid_records_with_media_rollback", "import_valid_records", observed_import)
    if scenario == "sqlite_busy":
        schema = db_owner.ensure_web_schema

        def busy_schema(conn):
            result = schema(conn)
            holder = sqlite3.connect(db)
            holder.execute("BEGIN IMMEDIATE")
            held_locks.append(holder)
            trace.append("other.begin_immediate")
            return result

        strict_patch(patch, db_owner, "ensure_web_schema", busy_schema)
        strict_patch(patch, db_owner, "FORMAL_SQLITE_BUSY_TIMEOUT_MS", 10)
    promotion_owner = owner("materialize_formal_record_images")
    promote = promotion_owner.promote_validated_image
    promotions = 0

    def observed_promote(*a, **kw):
        nonlocal promotions
        promotions += 1
        trace.append(f"media.promote.{promotions}")
        if promotions == 2 and scenario == "promotion_failure":
            raise OSError("晋升失败")
        return promote(*a, **kw)

    strict_patch(patch, promotion_owner, "promote_validated_image", observed_promote)
    persist = getattr(owner("_run_main"), "persist_discovery_checkpoint")

    def observed_persist(*a, **kw):
        trace.append("discovery.enter")
        if scenario == "discovery_failure":
            raise sqlite3.OperationalError("发现提交失败")
        result = persist(*a, **kw)
        trace.append("discovery.exit")
        return result

    binding(patch, "_run_main", "persist_discovery_checkpoint", observed_persist)

    def fake_command(cmd, cwd, timeout, log_dir, **kw):
        assert Path(cwd) == root
        assert cmd[1:4] == ["-P", "-m", "trippostcollect.platforms.entry"]
        save_path = Path(cmd[cmd.index("--save_data_path") + 1])
        assert save_path.is_relative_to(root)
        assert kw["extra_env"]["TRIPPOSTCOLLECT_DB_PATH"] == str(db)
        trace.append("worker.run")
        write_worker_artifacts(platform, save_path, log_dir / "behavior_evidence.json", state, scenario)
        return {"returncode": 0, "timed_out": False, "stdout_tail": "", "stderr_tail": ""}

    binding(patch, "_run_platform_without_policy", "run_command", fake_command)
    if platform == "bilibili":
        # B站没有 worker；保持执行器转发与 article search 本体，只替换会话/请求边界。
        async def fake_behavior(args, evidence_path):
            trace.append("bilibili.behavior")
            evidence = behavior_for("bilibili")
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False))
            return {"cookie_header": "SESSDATA=offline", "user_agent": "Mozilla/5.0 test-browser"}, evidence

        def fake_page(keyword, page, **kwargs):
            trace.append(("bilibili.page", page))
            items = [{"id": str(100 + n), "title": "青岛游记", "desc": "青岛搜索摘要",
                      "pubdate": 1789876800, "like": 3, "reply": 2, "view": 6,
                      "author": "作者", "mid": "author-1"} for n in (1, 2)] if page == 1 else []
            return BilibiliSearchPage(items=items, num_pages=1, num_results=2)

        def fake_detail(post_id, cookie_header, **kwargs):
            trace.append(("bilibili.detail", post_id))
            return {"title": "青岛游记", "content": f'<p>青岛海边完整正文</p><img src="https://i0.hdslb.com/bfs/article/{post_id}.png">'}, 1, 0.0

        def fake_image(url, post_id, **kwargs):
            trace.append(("bilibili.image", post_id))
            buffer = io.BytesIO()
            Image.new("RGB", (6, 4), color=(int(post_id) - 100, 20, 30)).save(buffer, format="PNG")
            return SimpleNamespace(content=buffer.getvalue(), http_status=200, media_type="image/png", final_url=url)

        binding(patch, "run_bilibili_article_search", "run_bilibili_behavior_session", fake_behavior)
        binding(patch, "download_bilibili_record_images", "fetch_bilibili_image_bytes", fake_image)
        for name, replacement in {
            "fetch_bilibili_wbi_keys": lambda *a, **k: ("a", "b"),
            "fetch_bilibili_article_page": fake_page,
            "fetch_bilibili_article_detail_with_retry": fake_detail,
            "fetch_bilibili_follower_count": lambda *a, **k: 42,
            "datetime": FixedDatetime,
            "time": SimpleNamespace(monotonic=lambda: 100.0, sleep=lambda delay: trace.append(("sleep", delay))),
        }.items():
            strict_patch(patch, bilibili_core, name, replacement)
    capsys.readouterr()
    try:
        result = executor._run_main(args, None)
    except (RuntimeError, sqlite3.Error) as exc:
        result = {"exception": type(exc).__name__, "message": str(exc), "original": exc is failure}
    finally:
        for holder in held_locks:
            holder.rollback()
            holder.close()
    stdout, stderr = capsys.readouterr()
    summary_path = root / "batches/fixed/summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else None
    envelope = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    files = {p.relative_to(media).as_posix(): p.read_bytes() for p in media.rglob("*") if p.is_file()}
    dirs = sorted(p.relative_to(media).as_posix() for p in media.rglob("*") if p.is_dir())
    markdown = summary_path.with_suffix(".md")
    snapshot = database_snapshot(db)
    for row in snapshot["web_posts"]:
        assert row["published_at"] == "2026-09-20T12:00:00+08:00"
    for row in snapshot["web_post_images"]:
        assert sha256((root / row["local_path"]).read_bytes()).hexdigest() == row["sha256"]
    return normalize({"result": result, "trace": trace, "database": snapshot,
                      "media": files, "media_dirs": dirs, "summary": summary,
                      "markdown": normalize(markdown.read_text(), root).encode() if markdown.exists() else None,
                      "envelope": envelope, "state": json.loads(state.path.read_text()),
                      "stderr": stderr}, root)
