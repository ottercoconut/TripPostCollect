"""辅助入口的调用顺序、文件与判定：登录预热三种入口、补采输入文件、补采判定、页面就绪与抖音 Cookie 清理。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import stat
import sys
from types import SimpleNamespace

import pytest

from trippostcollect.application import inputs, page_evidence, repair, warmup
from trippostcollect.core import paths as core_paths
from trippostcollect.runtime import cookies, page_readiness

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 30, 12, 0, 0, tzinfo=tz)


class FakePlaywright:
    """只记录调用；页面、Cookie、localStorage 与重开状态全部由替身提供。"""

    def __init__(self, scenario):
        self.trace = []
        self.scenario = scenario
        self.launches = 0
        self.chromium = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def launch_persistent_context(self, **kwargs):
        self.trace.append(("launch", kwargs))
        self.launches += 1
        return FakeContext(self, self.launches)


class FakeContext:
    def __init__(self, owner, index):
        self.owner, self.index = owner, index
        self.authenticated = owner.scenario != "manual" or index > 1
        self.desktop_reads = 0
        self.pages = [FakePage(self, 0)]

    async def new_page(self):
        self.owner.trace.append(("new_page", self.index))
        page = FakePage(self, len(self.pages))
        self.pages.append(page)
        return page

    async def cookies(self, urls=None):
        self.owner.trace.append(("cookies", self.index, urls))
        values = {"z_c0": "z", "d_c0": "d", "SUB": "s", "MLOGIN": "1", "SESSDATA": "b", "LOGIN_STATUS": "1"}
        if urls and "https://passport.weibo.com" in urls:
            self.desktop_reads += 1
            values["SSOLoginState"] = "signed" if self.desktop_reads > 1 else ""
        return [{"name": k, "value": v, "domain": ".example.test"} for k, v in values.items()]

    async def close(self):
        self.owner.trace.append(("close", self.index))


class FakePage:
    def __init__(self, context, index):
        self.context, self.index = context, index
        self.url = "about:blank"

    def record(self, *args):
        self.context.owner.trace.append((self.context.index, self.index, *args))

    async def goto(self, url, **kwargs):
        self.url = url
        self.record("goto", url, kwargs)

    async def reload(self, **kwargs):
        self.record("reload", kwargs)
        self.context.authenticated = True

    async def bring_to_front(self):
        self.record("front")

    async def wait_for_timeout(self, milliseconds):
        self.record("wait", milliseconds)
        if milliseconds == 60_000:
            raise KeyboardInterrupt

    async def evaluate(self, script):
        self.record("evaluate", script)
        if script == "() => navigator.userAgent":
            return "离线浏览器 UA"
        if "localStorage" in script:
            return {"HasUserLogin": "1"}
        ok = self.context.authenticated
        if self.context.owner.scenario == "reopen_failed" and self.context.index > 1:
            ok = False
        return {"ok": ok, "login": ok, "uid": "123" if ok else None}


def files_under(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) for p in root.rglob("*") if p.is_file()}


def configure(monkeypatch, module, fake, root):
    ticks = iter(range(10000))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks) / 100))
    monkeypatch.setattr(module, "datetime", FixedDatetime)
    monkeypatch.setattr(module, "profile_dir_for", lambda key: root / "profiles" / key)
    monkeypatch.setattr(module, "cookie_snapshot_path", lambda key: root / "profiles" / key / core_paths.COOKIE_SNAPSHOT_FILENAME)
    monkeypatch.setattr(module, "discover_cdp_browser_path", lambda: None)
    monkeypatch.setattr(module, "browser_runtime_args", lambda: [])
    monkeypatch.setattr(module, "browser_launch_environment", lambda: {})
    monkeypatch.setattr(module, "async_playwright", lambda: fake)
    monkeypatch.setattr(cookies, "datetime", FixedDatetime)


RUN_DIR = "20260930T120000000000+0000"
ALL_TARGETS = ("bilibili", "douyin", "weibo", "zhihu")


def warmup_expected(entry, scenario):
    """(成功, Chrome 启动次数, 写出快照的平台, 平台报告文件)。

    单平台入口：重开验证失败不写快照；--skip-reopen-verify 与 --no-close-on-success 只启动一次。
    统一入口不传这两个开关，四个平台各启动两次；重开验证失败时只有不依赖页面接口判定的抖音写出快照。
    """
    failed = scenario == "reopen_failed"
    if entry == "unified":
        reports = {f"{RUN_DIR}/{key}.json" for key in ALL_TARGETS} | {f"{RUN_DIR}/summary.json", f"{RUN_DIR}/summary.md"}
        return not failed, 5 if failed else 8, {"douyin"} if failed else set(ALL_TARGETS), reports
    reports = {"weibo.json"} if entry == "one" else {f"{RUN_DIR}/weibo.json", f"{RUN_DIR}/summary.json"}
    return not failed, 1 if scenario in {"skip", "keep"} else 2, set() if failed else {"weibo"}, reports


@pytest.mark.parametrize("entry", ["one", "media", "unified"])
@pytest.mark.parametrize("scenario", ["valid", "manual", "reopen_failed", "skip", "keep"])
def test_warmup_trace_and_files(monkeypatch, tmp_path, entry, scenario):
    root = tmp_path / "run"
    root.mkdir()
    fake = FakePlaywright(scenario)
    configure(monkeypatch, warmup, fake, root)
    args = argparse.Namespace(timeout_seconds=1, output_dir=str(root), browser_path=None,
                              no_close_on_success=scenario == "keep", skip_reopen_verify=scenario == "skip")
    if entry == "one":
        result = asyncio.run(warmup.warmup_one(fake, "weibo", root, args))
        ok = result["ok"]
        assert result["platform"] == "weibo"
    elif entry == "media":
        argv = ["mediacrawler_login_warmup.py", "--platforms", "weibo", "--output-dir", str(root), "--timeout-seconds", "1"]
        if scenario in {"keep", "skip"}:
            argv.append("--no-close-on-success" if scenario == "keep" else "--skip-reopen-verify")
        monkeypatch.setattr(sys, "argv", argv)
        ok = {0: True, 1: False}[warmup.media_main()]
    else:
        monkeypatch.setattr(sys, "argv", ["login_warmup.py", "--targets", "all", "--output-dir", str(root), "--timeout-seconds", "1"])
        ok = {0: True, 1: False}[warmup.login_main()]
    files = files_under(root)
    snapshots = {path.split("/")[1] for path in files if path.endswith(core_paths.COOKIE_SNAPSHOT_FILENAME)}
    reports = set(files) - {f"profiles/{key}/{core_paths.COOKIE_SNAPSHOT_FILENAME}" for key in snapshots}
    assert (ok, fake.launches, snapshots, reports) == warmup_expected(entry, scenario)
    assert all(mode == 0o600 for path, (_, mode) in files.items() if path.endswith(core_paths.COOKIE_SNAPSHOT_FILENAME))
    kinds = [event[0] if isinstance(event[0], str) else event[2] for event in fake.trace]
    # 每次启动都在关闭后才进入下一次启动。
    assert kinds.count("launch") == kinds.count("close") == fake.launches
    if scenario == "manual" and entry != "unified":
        # 首次未登录：新开页置前并刷新，等待人工登录后再判定。
        first = kinds[:kinds.index("close")]
        assert first.index("new_page") < first.index("front") < first.index("reload") < first.index("wait")
    if scenario == "keep" and entry != "unified":
        assert kinds[-2:] == ["wait", "close"]


def outcome(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (SystemExit, ValueError, TypeError) as exc:
        return type(exc).__name__, str(exc)


# 读取器 → (合法内容, 解析结果, 错误信息中的文件说明)
REPAIR_INPUTS = {
    "load_zhihu_detail_urls": ('["https://www.zhihu.com/question/1/answer/2?token=x#frag"]',
                               ["https://www.zhihu.com/question/1/answer/2"], "Zhihu detail URL file"),
    "load_xhs_detail_urls": ('["https://www.xiaohongshu.com/explore/a?xsec_token=secret&xsec_source=pc#frag"]',
                             ["https://www.xiaohongshu.com/explore/a?xsec_token=secret&xsec_source=pc"], "XHS detail URL file"),
    "load_xhs_repair_target_ids": ('["a", "a", " b "]', {"a", "b"}, "XHS repair target ID file"),
    "load_post_repair_targets": ('[{"platform_post_id":"123","detail_target":"123","keyword":"青岛"}]',
                                 [{"platform_post_id": "123", "detail_target": "123", "keyword": "青岛"}],
                                 "post repair target file"),
}
EMPTY_MESSAGES = {
    "load_zhihu_detail_urls": "Zhihu detail URL file contains no answer/article URLs",
    "load_xhs_detail_urls": "XHS detail URL file contains no URLs",
    "load_xhs_repair_target_ids": "XHS repair target ID file contains no post IDs",
    "load_post_repair_targets": "post repair target file contains no targets",
}


def _write_input(path, payload, state):
    if state != "missing":
        path.write_text({"valid": payload, "broken": "{", "wrong_type": '"text"', "empty": "[]"}[state])


@pytest.mark.parametrize("name", sorted(REPAIR_INPUTS))
@pytest.mark.parametrize("state", ["valid", "missing", "broken", "wrong_type", "empty"])
def test_repair_file_inputs(tmp_path, name, state):
    payload, parsed, label = REPAIR_INPUTS[name]
    path = tmp_path / "input.json"
    _write_input(path, payload, state)
    reader = getattr(inputs if hasattr(inputs, name) else repair, name)
    result = outcome(reader, *((path, "weibo") if name == "load_post_repair_targets" else (path,)))
    if state == "valid":
        assert result == parsed
    elif state in {"missing", "broken"}:
        # 读不到或不是 JSON：以 SystemExit 报出文件路径与原因。
        assert result[0] == "SystemExit" and result[1].startswith(f"invalid {label}: {path}: ")
    elif state == "wrong_type":
        assert result == ("SystemExit", f"{label} must contain a JSON array")
    else:
        assert result == ("SystemExit", EMPTY_MESSAGES[name])


@pytest.mark.parametrize("state", ["valid", "missing", "broken", "wrong_type", "empty"])
def test_repair_report_is_optional(tmp_path, state):
    """补采报告缺失或不可用时视为空报告，不报错。"""
    path = tmp_path / "input.json"
    _write_input(path, '{"successful_ids":["a"]}', state)
    assert repair.load_xhs_repair_report(path) == ({"successful_ids": ["a"]} if state == "valid" else {})


@pytest.mark.parametrize("blocked,child_ok,valid,images,behavior,allowed", [
    # 子进程整体成功时不需要部分放行。
    (False, True, 0, False, False, True),
    (True, True, 1, True, True, False),
    # 子进程失败时，只有未阻断、有有效记录、图片完整且行为证据合格才放行。
    (False, False, 1, True, True, True),
    (False, False, 0, True, True, False),
    (False, False, 1, False, True, False),
    (False, False, 1, True, False, False),
])
def test_repair_partial_execution(blocked, child_ok, valid, images, behavior, allowed):
    kwargs = dict(repair_mode=True, runtime_blocked=blocked, child_execution_ok=child_ok,
                  validation={"valid_total_count": valid}, image_materialization={"complete": images},
                  behavior_validation={"ok": behavior})
    assert repair.repair_partial_child_execution_allowed(**kwargs) is allowed


PROCESSED = {"available": True, "stopped": True, "stop_reason": "repair_targets_processed",
             "stop_detail": "specified_detail_targets", "candidate_count": 2, "batches": []}


@pytest.mark.parametrize("records,completed,stop_reason", [
    ([], False, ""),
    ([{"platform": "weibo", "run": {"returncode": 0}, "failure_classification": {"failure_type": "success"}}], True, ""),
    ([{"platform": "weibo", "run": {"returncode": 1}, "failure_classification": {"failure_type": "login_required"}}],
     False, "login_required"),
])
def test_repair_completion_and_evidence(records, completed, stop_reason):
    crawl = __import__("mediacrawler_crawl")
    assert repair.repair_candidate_execution_completed(records, ["weibo"]) is completed
    assert repair.repair_runtime_stop_reason(records, ["weibo"], latest_runtime_blocker=crawl.latest_runtime_blocker) == stop_reason
    assert repair.xhs_repair_pagination_evidence(records, target_count=2) == {
        **PROCESSED, "successful_candidate_count": 0, "skipped_candidate_count": 0, "skipped_candidate_failures": [],
    }
    targets = [{"platform_post_id": "1"}, {"platform_post_id": "2"}]
    assert repair.post_repair_pagination_evidence(
        targets, platform="weibo", successful_identities={"weibo:id:1"},
        materialization_failures=[{"identity": "weibo:id:2", "error_code": "failed"}],
    ) == {**PROCESSED, "successful_candidate_count": 1, "skipped_candidate_count": 1, "skipped_candidate_failures": [{
        "platform": "weibo", "identity": "weibo:id:2", "platform_post_id": "2", "failure_scope": "image",
        "detail": "explicit repair target produced no formally valid persisted detail record", "error_code": "failed",
        "retryable": False, "attempts": 1, "source_index": None, "terminal_for_run": True,
        "evidence_source": "image_materialization",
    }]}


def test_repair_fallback_sqlite(tmp_path):
    path = tmp_path / "fallback.sqlite"
    assert repair.load_post_repair_fallbacks(path, "weibo", {"1"}) == {}
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE web_posts(platform_key, platform_post_id, keyword, raw_sample_json)")
        conn.execute("INSERT INTO web_posts VALUES (?, ?, ?, ?)", ("weibo", "1", "青岛", '{"liked_count": 0}'))
    assert repair.load_post_repair_fallbacks(path, "weibo", {"1", "2"}) == {"1": {"keyword": "青岛", "liked_count": 0}}


@pytest.mark.parametrize("navigation_error,warnings", [
    (None, []), ("timeout", ["commit_timeout: navigation"]), ("error", ["commit_error: RuntimeError: navigation"]),
])
@pytest.mark.parametrize("ready", [True, False])
def test_page_readiness_trace(monkeypatch, navigation_error, warnings, ready):
    class Page:
        url = "https://example.test/"

        def __init__(self):
            self.trace = []
            self.reads = 0
            self.clock = 0

        async def goto(self, url, **kwargs):
            self.trace.append(("goto", url, kwargs))
            if navigation_error:
                raise (page_readiness.PlaywrightTimeoutError if navigation_error == "timeout" else RuntimeError)("navigation")

        async def evaluate(self, script):
            self.trace.append(("evaluate",))
            self.reads += 1
            return {"ready": ready and self.reads > 1, "body_text_length": 300 if self.reads > 1 else 0, "url": self.url}

        async def wait_for_timeout(self, value):
            self.trace.append(("wait", value))
            self.clock += value / 1000

    page = Page()
    loop = SimpleNamespace(time=lambda: page.clock)
    monkeypatch.setattr(page_readiness, "asyncio", SimpleNamespace(get_running_loop=lambda: loop))

    async def run():
        nav = await page_readiness.navigate_with_commit_and_readiness(page, page.url, commit_timeout_ms=10, readiness_timeout_ms=1)
        enrichment = await page_readiness.wait_for_content_enrichment(page, timeout_ms=1, poll_ms=1)
        return nav, enrichment

    nav, enrichment = asyncio.run(run())
    # 提交失败只记警告；就绪超时才是致命。
    readiness = {"ready": ready, "body_text_length": 300, "url": page.url, "timed_out": not ready}
    assert nav == {
        "strategy": "commit_then_content_ready", "url": page.url, "commit_timeout_ms": 10, "readiness_timeout_ms": 1,
        "commit_ok": navigation_error is None, "content_ready": ready, "fatal": not ready, "warnings": warnings,
        "readiness": readiness, "final_url": page.url,
    }
    # 正文 300 字已达 250 字下限，富化等待立即满足。
    assert enrichment == {"ready": ready, "body_text_length": 300, "url": page.url, "enriched": True,
                          "enrichment_targets": {"min_body_chars": 250, "min_images": 8}, "timed_out": False}
    assert page.trace[:3] == [("goto", page.url, {"wait_until": "commit", "timeout": 10}), ("evaluate",), ("wait", 250)]


def test_page_evidence_cookie_cleanup(monkeypatch, tmp_path):
    monkeypatch.setattr(page_evidence, "datetime", FixedDatetime)
    db = tmp_path / "Default/Cookies"
    db.parent.mkdir()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE cookies(host_key, name, path)")
        conn.executemany("INSERT INTO cookies VALUES (?, ?, ?)", [(".douyin.com", "a", "/"), (".example.test", "b", "/")])
    at = "2026-09-30T12:00:00+00:00"
    assert page_evidence.clean_douyin_profile_cookies(tmp_path) == {
        "target": "douyin", "strategy": "sqlite_cookie_rotation", "profile_dir": str(tmp_path), "started_at": at,
        "deleted": 1, "cookies": [{"host": ".douyin.com", "name": "a", "path": "/"}], "ok": True,
        "cookie_db": str(db), "finished_at": at,
    }
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM cookies").fetchall() == [(".example.test", "b", "/")]
    trace = []

    class Context:
        async def cookies(self):
            trace.append("cookies")
            return [{"domain": ".douyin.com", "name": "a"}]

        async def clear_cookies(self):
            trace.append("clear")

    assert asyncio.run(page_evidence.clear_douyin_context_cookies(Context())) == {
        "target": "douyin", "strategy": "runtime_context_clear_cookies", "started_at": at, "ok": True,
        "deleted": 1, "cookies": [{"domain": ".douyin.com", "name": "a"}], "finished_at": at,
    }
    assert trace == ["cookies", "clear"]


@pytest.mark.parametrize("scenario", ["valid", "skip", "keep"])
def test_issue127_snapshot_records_login_page_user_agent(monkeypatch, tmp_path, scenario):
    """#127：新实现写出的每份快照都含登录 page 的 navigator.userAgent（离线替身返回固定值）。"""
    root = tmp_path / "run"
    root.mkdir()
    fake = FakePlaywright(scenario)
    configure(monkeypatch, warmup, fake, root)
    args = argparse.Namespace(timeout_seconds=1, output_dir=str(root), browser_path=None,
                              no_close_on_success=scenario == "keep", skip_reopen_verify=scenario == "skip")
    asyncio.run(warmup.warmup_one(fake, "weibo", root, args))
    snapshots = [json.loads(path.read_text()) for path in root.rglob(core_paths.COOKIE_SNAPSHOT_FILENAME)]
    assert snapshots and all(item["user_agent"] == "离线浏览器 UA" for item in snapshots)
