"""T03 显式输入、故障子集与迁移基线等价性。所有文件均为临时合成数据。"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from trippostcollect.application import candidates, worker_inputs
from trippostcollect.db import discovery_read
from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.runtime import cookies, helpers, http


ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / "tests/golden/t03_baseline.json").read_text())
ENV_KEYS = {
    "db_path": "TRIPPOSTCOLLECT_DB_PATH",
    "xhs_target_key": "TRIPPOSTCOLLECT_XHS_DISCOVERY_TARGET_KEY",
    "xhs_account_id": "TRIPPOSTCOLLECT_XHS_ACCOUNT_ID",
    "xhs_fingerprint": "TRIPPOSTCOLLECT_XHS_DISCOVERY_QUERY_FINGERPRINT",
    "job_id": "TRIPPOSTCOLLECT_DISCOVERY_JOB_ID",
    "fingerprint": "TRIPPOSTCOLLECT_DISCOVERY_QUERY_FINGERPRINT",
    "resume_identities_path": "TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH",
}


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def legacy(tmp_path):
    # 来自登记提交的原源码；副本测试不依赖 .git 或宿主 checkout。
    source = "\n\n".join(BASELINE["sources"].values())
    path = tmp_path / "legacy.py"
    path.write_text(
        "from __future__ import annotations\n"
        "import os, json, sqlite3, random, re, time, urllib.parse\n"
        "from pathlib import Path\n"
        "from urllib.parse import urlparse\n"
        "from typing import Any, Dict, List, Optional, Tuple\n" + source,
    )
    return _module(path, "t03_legacy")


@pytest.fixture
def fork():
    return _module(
        ROOT / "tools/MediaCrawler/tools/trippostcollect_adaptive.py", "t03_fork_adaptive",
    )


@pytest.fixture
def scope(monkeypatch, tmp_path):
    values = dict(
        db_path=str(tmp_path / "known.sqlite"),
        xhs_target_key=" target ", xhs_account_id=" account ", xhs_fingerprint=" fp ",
        job_id=" 24 ", fingerprint=" fp ", resume_identities_path="",
    )
    for key, env in ENV_KEYS.items():
        monkeypatch.setenv(env, values[key])
    return values


def _set_scope(monkeypatch, scope, **changes):
    scope.update(changes)
    for key, value in changes.items():
        monkeypatch.setenv(ENV_KEYS[key], value)


def _database(path):
    bootstrap_database(path, sync_jobs=False)
    # 新连接只构造读取夹具，不触发调度或账号运行流程。
    with sqlite3.connect(path) as conn:
        for platform in ("weibo", "xhs"):
            conn.execute(
                "INSERT INTO web_posts(platform_key,platform_post_id,source_type,"
                "source_url,canonical_url,captured_at) VALUES (?,?,?,?,?,?)",
                (platform, "post", "text", "https://example.invalid/post", "https://example.invalid/url-id", "2026-01-01"),
            )
        for account, identity in (("account", "xhs-seen"), ("other", "other-account")):
            conn.execute(
                "INSERT INTO xhs_discovery_seen_candidates(target_key,account_id,"
                "query_fingerprint,platform_post_id,first_run_id,last_run_id) VALUES (?,?,?,?,?,?)",
                ("target", account, "fp", identity, "run", "run"),
            )
        for job, platform, fingerprint, identity in (
            (24, "weibo", "fp", "seen"), (25, "weibo", "fp", "other-job"),
            (24, "douyin", "fp", "other-platform"), (24, "weibo", "other", "other-fp"),
        ):
            conn.execute(
                "INSERT INTO crawl_discovery_seen_candidates(job_id,platform_key,"
                "query_fingerprint,platform_post_id,first_run_id,last_run_id) VALUES (?,?,?,?,?,?)",
                (job, platform, fingerprint, identity, "run", "run"),
            )
        conn.execute(
            "INSERT INTO crawl_discovery_candidate_exclusions(job_id,platform_key,"
            "query_fingerprint,platform_post_id,reason,authorized_run_id) VALUES (24,'weibo','fp','excluded','test','run')",
        )


def _equal_read(platform, scope, legacy, fork, expected):
    assert legacy.existing_platform_identities(platform) == expected
    assert discovery_read.existing_platform_identities(platform, **scope) == expected
    assert fork.existing_platform_identities(platform) == expected


@pytest.mark.parametrize("platform", ["weibo", "xhs"])
@pytest.mark.parametrize("state", ["unset", "missing", "empty", "directory", "missing-parent", "invalid", "schema-empty"])
def test_database_unavailable(scope, legacy, fork, monkeypatch, platform, state):
    path = Path(scope["db_path"])
    if state == "unset":
        _set_scope(monkeypatch, scope, db_path="")
    elif state == "empty":
        path.touch()
    elif state == "directory":
        path.mkdir()
    elif state == "missing-parent":
        _set_scope(monkeypatch, scope, db_path=str(path / "missing.sqlite"))
    elif state == "invalid":
        path.write_text("不是 SQLite")
    elif state == "schema-empty":
        bootstrap_database(path, sync_jobs=False)
    _equal_read(platform, scope, legacy, fork, set())


@pytest.mark.parametrize("platform", ["weibo", "xhs"])
@pytest.mark.parametrize("missing", [None, "web_posts", "seen", "exclusions"])
def test_database_subset(scope, legacy, fork, platform, missing):
    _database(scope["db_path"])
    expected = {"post", "url-id", "xhs-seen"} if platform == "xhs" else {"post", "url-id", "seen", "excluded"}
    table = {
        "web_posts": "web_posts",
        "seen": "xhs_discovery_seen_candidates" if platform == "xhs" else "crawl_discovery_seen_candidates",
        "exclusions": "crawl_discovery_candidate_exclusions",
    }.get(missing)
    if table:
        with sqlite3.connect(scope["db_path"]) as conn:
            conn.execute(f"DROP TABLE {table}")
        if missing == "web_posts":
            expected = set()
        elif missing == "seen":
            expected.discard("xhs-seen" if platform == "xhs" else "seen")
        elif platform != "xhs":
            expected.discard("excluded")
    _equal_read(platform, scope, legacy, fork, expected)


@pytest.mark.parametrize("job_id", ["", "0", "not-int", "-1"])
def test_job_scope_empty_or_invalid(scope, legacy, fork, monkeypatch, job_id):
    _database(scope["db_path"])
    _set_scope(monkeypatch, scope, job_id=job_id)
    _equal_read("weibo", scope, legacy, fork, {"post", "url-id"})


@pytest.mark.parametrize("field", ["xhs_target_key", "xhs_account_id", "xhs_fingerprint", "fingerprint"])
def test_incomplete_scope(scope, legacy, fork, monkeypatch, field):
    _database(scope["db_path"])
    _set_scope(monkeypatch, scope, **{field: " "})
    _equal_read("xhs" if field.startswith("xhs") else "weibo", scope, legacy, fork, {"post", "url-id"})


@pytest.mark.parametrize("state", ["missing", "broken", "directory", "valid", "object", "null", "number"])
def test_resume_subset(scope, legacy, fork, monkeypatch, tmp_path, state):
    _database(scope["db_path"])
    path = tmp_path / "resume.json"
    _set_scope(monkeypatch, scope, resume_identities_path=str(path))
    if state == "directory":
        path.mkdir()
    elif state != "missing":
        path.write_text({
            "broken": "{", "valid": '[null,"",0,false,"resume","post"]',
            "object": '{"resume":1}', "null": "null", "number": "1",
        }[state])
    expected = {"post", "url-id", "seen", "excluded"}
    if state in {"null", "number"}:
        # 迭代在 except 之外；不能擅自把非法形状吞成空集合。
        for read in (legacy.existing_platform_identities, fork.existing_platform_identities):
            with pytest.raises(TypeError):
                read("weibo")
        with pytest.raises(TypeError):
            discovery_read.existing_platform_identities("weibo", **scope)
    else:
        if state == "valid":
            expected.update({"resume", "0", "False"})
        elif state == "object":
            expected.add("resume")
        _equal_read("weibo", scope, legacy, fork, expected)


@pytest.mark.parametrize("error", [OSError, sqlite3.Error])
def test_outer_database_errors_preserve_resume(scope, legacy, fork, monkeypatch, tmp_path, error):
    resume = tmp_path / "resume.json"
    resume.write_text('["resume"]')
    _set_scope(monkeypatch, scope, resume_identities_path=str(resume))

    def fail(*args):
        raise error("合成连接故障")

    monkeypatch.setattr(sqlite3, "connect", fail)
    _equal_read("weibo", scope, legacy, fork, {"resume"})


@pytest.mark.parametrize("platform,table,error,expected", [
    ("weibo", "seen_candidates", ValueError, {"post", "url-id", "excluded"}),
    ("weibo", "candidate_exclusions", ValueError, {"post", "url-id", "seen"}),
    ("weibo", "seen_candidates", sqlite3.Error, {"post", "url-id", "excluded"}),
    ("weibo", "candidate_exclusions", sqlite3.Error, {"post", "url-id", "seen"}),
    ("xhs", "seen_candidates", sqlite3.Error, {"post", "url-id"}),
    ("weibo", "candidate_exclusions", OSError, set()),
    ("xhs", "seen_candidates", ValueError, None),
    ("weibo", "seen_candidates", TypeError, None),
])
def test_query_error_boundaries(scope, legacy, fork, monkeypatch, platform, table, error, expected):
    _database(scope["db_path"])
    connect = sqlite3.connect

    class FaultConnection:
        def __enter__(self):
            self.conn = connect(scope["db_path"])
            return self

        def __exit__(self, *args):
            self.conn.close()

        def execute(self, sql, args):
            if table in sql:
                raise error("合成查询故障")
            return self.conn.execute(sql, args)

    monkeypatch.setattr(sqlite3, "connect", lambda *_: FaultConnection())
    if expected is not None:
        _equal_read(platform, scope, legacy, fork, expected)
    else:
        for read in (legacy.existing_platform_identities, fork.existing_platform_identities):
            with pytest.raises(error):
                read(platform)
        with pytest.raises(error):
            discovery_read.existing_platform_identities(platform, **scope)


@pytest.mark.parametrize("error", [OSError, TypeError, json.JSONDecodeError])
def test_resume_read_errors_keep_database(scope, legacy, fork, monkeypatch, error):
    _database(scope["db_path"])
    _set_scope(monkeypatch, scope, resume_identities_path="synthetic.json")

    def fail(*args, **kwargs):
        if error is json.JSONDecodeError:
            raise error("合成 JSON 故障", "", 0)
        raise error("合成文件故障")

    monkeypatch.setattr(Path, "read_text", fail)
    _equal_read("weibo", scope, legacy, fork, {"post", "url-id", "seen", "excluded"})


@pytest.mark.parametrize("value,default,expected", [(None, 3, 3), ("bad", 3, 3), ("-2", 3, 0), ("0", 3, 0), ("17", 3, 17), (None, -3, 0)])
def test_env_int_reader(legacy, fork, monkeypatch, value, default, expected):
    name = "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES"
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    read = worker_inputs.env_int_reader(name, default, environ=os.environ)
    assert read() == legacy.env_int(name, default) == fork.env_int(name, default) == expected
    monkeypatch.setenv(name, "29")
    assert read() == 29
    bound = {name: "4"}
    read = worker_inputs.env_int_reader(name, default, environ=bound)
    assert read() == 4
    bound[name] = "6"
    assert read() == 6
    bound[name] = None
    assert read() == max(0, default)


@pytest.mark.parametrize("platform", ["weibo", "douyin", "zhihu", "xhs"])
def test_factory_loads_known_once(fork, monkeypatch, platform):
    known = {"known"}
    calls = []
    monkeypatch.setattr(fork, "existing_platform_identities", lambda value: calls.append(value) or known)
    monkeypatch.setattr(fork, "append_execution_event", lambda *args: None)
    accumulator = fork.AdaptiveAccumulator.from_environment(platform)
    assert isinstance(accumulator, fork.AdaptiveAccumulator)
    assert accumulator.existing_identities is known
    assert accumulator.stagnation_basis == ("candidate_identity" if platform == "weibo" else "valid_new")
    for index in range(5):
        accumulator.begin_batch()
        accumulator.is_known("known")
        accumulator.consider(str(index), valid=False)
        accumulator.finish_batch()
    assert calls == [platform]


def _event_sequence(accumulator):
    accumulator.begin_batch()
    for identity, valid in (("known", True), ("new", True), ("text-only", False)):
        accumulator.consider(identity, valid=valid)
    accumulator.finish_batch(source_page=1, resume_page=2, batch_complete=True)
    accumulator.mark_source_exhausted(
        "empty_page", source_page=2, raw_batch_count=0, source_has_more=False,
    )


def test_root_default_event_sink_does_not_write_files(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    state.write_text('{"events":[]}')
    before = state.read_bytes()
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state))
    accumulator = candidates.AdaptiveAccumulator.for_platform("weibo", existing_identities=set())
    _event_sequence(accumulator)
    accumulator.skip_candidate_failure(
        "failed", failure_scope="image", detail="failed", error_code="test", attempts=1,
    )
    accumulator.mark_runtime_failed("failed")
    accumulator.finish_batch()
    assert state.read_bytes() == before
    assert list(tmp_path.iterdir()) == [state]
    state.unlink()
    _event_sequence(candidates.AdaptiveAccumulator("weibo"))
    assert list(tmp_path.iterdir()) == []


def test_injected_event_sequence_matches_fork_module_patch(fork, monkeypatch):
    root_events = []
    fork_events = []
    root = candidates.AdaptiveAccumulator(
        "weibo", stagnation_basis="candidate_identity", existing_identities={"known"},
        event_sink=lambda event_type, details: root_events.append((event_type, details)),
    )
    adapted = fork.AdaptiveAccumulator(
        "weibo", stagnation_basis="candidate_identity", existing_identities={"known"},
    )
    # 实例创建后才替换模块全局函数，验证出口在事件发生时解析。
    monkeypatch.setattr(
        fork, "append_execution_event",
        lambda event_type, details: fork_events.append((event_type, details)),
    )
    _event_sequence(root)
    _event_sequence(adapted)
    assert root_events == fork_events
    assert [event_type for event_type, _ in root_events] == [
        "adaptive_batch_completed", "adaptive_search_stopped",
    ]
    assert root_events[0][1]["candidate_count"] == 3
    assert root_events[1][1]["stop_reason"] == "source_exhausted"


def test_fork_preserves_explicit_sink_and_excludes_it_from_comparison(fork, monkeypatch):
    events = []

    def sink(event_type, details):
        events.append((event_type, details))

    def reject_module_sink(*args):
        pytest.fail("显式注入的事件出口不能被 fork 默认出口覆盖")

    monkeypatch.setattr(fork, "append_execution_event", reject_module_sink)
    for accumulator_type in (candidates.AdaptiveAccumulator, fork.AdaptiveAccumulator):
        explicit = accumulator_type("weibo", event_sink=sink)
        default = accumulator_type("weibo")
        assert explicit == default
        assert repr(explicit) == repr(default)
        assert "event_sink" not in repr(explicit)
        _event_sequence(explicit)
    assert len(events) == 4


def test_candidates_imports_respect_application_boundary():
    tree = ast.parse((ROOT / "src/trippostcollect/application/candidates.py").read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    for name in imports:
        assert not any(
            name == prefix or name.startswith(prefix + ".")
            for prefix in ("trippostcollect.platforms", "tools", "config")
        ), name


@pytest.mark.parametrize("disabled", [False, True])
@pytest.mark.parametrize("override", [None, False, True])
def test_http_factory(monkeypatch, disabled, override):
    monkeypatch.setattr(http.httpx, "AsyncClient", lambda **kwargs: kwargs)
    kwargs = {} if override is None else {"verify": override}
    result = http.make_async_client(disable_ssl_verify=disabled, **kwargs)
    assert result == {"verify": not disabled if override is None else override}


def test_fork_http_reads_config_each_call(monkeypatch):
    config = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "config", config)
    fork_http = _module(ROOT / "tools/MediaCrawler/tools/httpx_util.py", "t03_fork_http")
    monkeypatch.setattr(http.httpx, "AsyncClient", lambda **kwargs: kwargs)
    assert fork_http.make_async_client() == {"verify": True}
    for disabled in (True, False, True):
        monkeypatch.setattr(config, "DISABLE_SSL_VERIFY", disabled, raising=False)
        assert fork_http.make_async_client() == {"verify": not disabled}
        assert fork_http.make_async_client(verify="custom", timeout=5) == {"verify": "custom", "timeout": 5}


@pytest.mark.parametrize("name", ["get_user_agent", "get_mobile_user_agent"])
def test_user_agents_preserve_candidates_and_randomness(legacy, monkeypatch, name):
    old = getattr(legacy, name)
    new = getattr(helpers, name)
    source = BASELINE["sources"]["tools/crawler_util.py"]
    tree = ast.parse(source)
    definition = next(n for n in tree.body if n.name == name)
    expected = ast.literal_eval(definition.body[0].value)
    captured = []
    monkeypatch.setattr(random, "choice", lambda values: captured.append(values) or values[0])
    assert old() == new() == expected[0]
    assert captured == [expected, expected]
    left, right = random.Random(73), random.Random(73)
    assert [new(left) for _ in range(50)] == [right.choice(expected) for _ in range(50)]


@pytest.mark.parametrize("platform", ["bilibili", "weibo", "douyin", "zhihu", "xhs", "unknown"])
def test_cookie_pure_functions_match_baseline(legacy, platform, monkeypatch):
    monkeypatch.setattr(cookies.time, "time", lambda: 100)
    if platform == "unknown":
        for module in (legacy, cookies):
            with pytest.raises(KeyError):
                module.platform_cookie_url(platform)
    else:
        assert cookies.platform_cookie_url(platform) == legacy.platform_cookie_url(platform)
    assert cookies.required_cookie_names(platform) == legacy.required_cookie_names(platform)
    values = [None, {}, {"name": "empty", "value": ""}, {"name": "zero", "value": 0},
              {"name": " x ", "value": "a=b", "expires": 101}, {"name": "old", "value": "v", "expires": 99},
              {"name": "session", "value": "v", "expires": -1}, {"name": "equal", "value": "v", "expires": 100}]
    assert cookies.cookies_to_header(values) == legacy.cookies_to_header(values)
    for header in ("", "a=1;a=2; =x;bare;b=a=b", " z_c0=x ;d_c0=y"):
        assert cookies.cookie_names_from_header(header) == legacy.cookie_names_from_header(header)
        assert cookies.convert_str_cookie_to_dict(header) == legacy.convert_str_cookie_to_dict(header)
    for values in (None, [], [{"name": "a", "value": "b"}, {"name": "a", "value": "c"}, {}]):
        assert cookies.convert_cookies(values) == legacy.convert_cookies(values)
    export = {"cookie_header": "synthetic", "source": "snapshot", "cookie_names": ["a"]}
    assert cookies.public_cookie_export(export) == legacy.public_cookie_export(export)


@pytest.mark.parametrize("state", ["missing", "broken", "wrong-list", "incomplete", "complete", "null"])
def test_cookie_snapshot_matches_baseline(legacy, monkeypatch, tmp_path, state):
    path = tmp_path / "snapshot.json"
    monkeypatch.setattr(cookies, "cookie_snapshot_path", lambda _: path)
    legacy.cookie_snapshot_path = lambda _: path
    if state != "missing":
        path.write_text({
            "broken": "{", "wrong-list": '{"cookies":{}}',
            "incomplete": '{"cookies":[{"name":"d_c0","value":"x"}]}',
            "complete": '{"saved_at":"now","cookies":[{"name":"d_c0","value":"x"},{"name":"z_c0","value":"y"}]}',
            "null": "null",
        }[state])
    if state == "null":
        for module in (legacy, cookies):
            with pytest.raises(AttributeError):
                module.load_cookie_snapshot("zhihu")
    else:
        assert cookies.load_cookie_snapshot("zhihu") == legacy.load_cookie_snapshot("zhihu")


@pytest.mark.asyncio
@pytest.mark.parametrize("urls", [None, [], ["https://example.invalid/"]])
async def test_context_cookie_call_shape(legacy, urls):
    traces = []

    class Context:
        async def cookies(self, **kwargs):
            traces.append(kwargs)
            return [{"name": "a", "value": "b"}]

    assert await cookies.convert_browser_context_cookies(Context(), urls) == await legacy.convert_browser_context_cookies(Context(), urls)
    assert traces == ([{"urls": urls}] * 2 if urls else [{}, {}])


def test_helper_html_and_url_equivalence(legacy):
    for value in ("", "<script>x</script><style>y</style><b>文本</b>", "<script>\nx\n</script> a &amp; b"):
        assert helpers.extract_text_from_html(value) == legacy.extract_text_from_html(value)
    for value in ("", "https://example.invalid/?a=1&a=2&empty=&b=x%20y", "no-query"):
        assert helpers.extract_url_params_to_dict(value) == legacy.extract_url_params_to_dict(value)


def test_t03_ledger_rows(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/dev"))
    ledger = _module(ROOT / "scripts/dev/adapter_ledger.py", "t03_ledger")
    result = ledger.build_progress(ROOT)
    rows = [row for row in result["rows"] if row["card"] == "T03"]
    assert len(rows) == 28
    assert result["counts"]["missing"] == 0
    pending = {row["qualname"] for row in rows if row["state"] == "pending"}
    # 台账只识别单条 return 到目标模块；继承适配不伪报为原类逐字迁出。
    assert pending == {"AdaptiveAccumulator", "AdaptiveAccumulator.from_environment"}
    assert sum(row["state"] == "moved" for row in rows) == 26


def test_cookie_imports_share_one_implementation(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import mediacrawler_crawl as crawl
    import mediacrawler_login_warmup as warmup
    import repair_bilibili_articles as repair

    assert crawl.required_cookie_names is warmup.required_cookie_names is cookies.required_cookie_names
    assert crawl.load_cookie_snapshot is repair.load_cookie_snapshot is cookies.load_cookie_snapshot
