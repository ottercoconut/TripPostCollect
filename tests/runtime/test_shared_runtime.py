"""发现读取（已知 ID 的数据库与续跑子集）、候选累计器、Cookie 与 HTTP 辅助函数。所有文件均为临时合成数据。"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import sqlite3

import pytest

from trippostcollect.application import candidates, worker_inputs
from trippostcollect.db import discovery_read
from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.runtime import cookies, helpers, http


ROOT = Path(__file__).resolve().parents[2]
ENV_KEYS = {
    "db_path": "TRIPPOSTCOLLECT_DB_PATH",
    "xhs_target_key": "TRIPPOSTCOLLECT_XHS_DISCOVERY_TARGET_KEY",
    "xhs_account_id": "TRIPPOSTCOLLECT_XHS_ACCOUNT_ID",
    "xhs_fingerprint": "TRIPPOSTCOLLECT_XHS_DISCOVERY_QUERY_FINGERPRINT",
    "job_id": "TRIPPOSTCOLLECT_DISCOVERY_JOB_ID",
    "fingerprint": "TRIPPOSTCOLLECT_DISCOVERY_QUERY_FINGERPRINT",
    "resume_identities_path": "TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH",
}


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


def _read(platform, scope, expected):
    assert discovery_read.existing_platform_identities(platform, **scope) == expected


@pytest.mark.parametrize("platform", ["weibo", "xhs"])
@pytest.mark.parametrize("state", ["unset", "missing", "empty", "directory", "missing-parent", "invalid", "schema-empty"])
def test_database_unavailable(scope, monkeypatch, platform, state):
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
    _read(platform, scope, set())


@pytest.mark.parametrize("platform", ["weibo", "xhs"])
@pytest.mark.parametrize("missing", [None, "web_posts", "seen", "exclusions"])
def test_database_subset(scope, platform, missing):
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
    _read(platform, scope, expected)


@pytest.mark.parametrize("job_id", ["", "0", "not-int", "-1"])
def test_job_scope_empty_or_invalid(scope, monkeypatch, job_id):
    _database(scope["db_path"])
    _set_scope(monkeypatch, scope, job_id=job_id)
    _read("weibo", scope, {"post", "url-id"})


@pytest.mark.parametrize("field", ["xhs_target_key", "xhs_account_id", "xhs_fingerprint", "fingerprint"])
def test_incomplete_scope(scope, monkeypatch, field):
    _database(scope["db_path"])
    _set_scope(monkeypatch, scope, **{field: " "})
    _read("xhs" if field.startswith("xhs") else "weibo", scope, {"post", "url-id"})


@pytest.mark.parametrize("state", ["missing", "broken", "directory", "valid", "object", "null", "number"])
def test_resume_subset(scope, monkeypatch, tmp_path, state):
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
        with pytest.raises(TypeError):
            discovery_read.existing_platform_identities("weibo", **scope)
    else:
        if state == "valid":
            expected.update({"resume", "0", "False"})
        elif state == "object":
            expected.add("resume")
        _read("weibo", scope, expected)


@pytest.mark.parametrize("error", [OSError, sqlite3.Error])
def test_outer_database_errors_preserve_resume(scope, monkeypatch, tmp_path, error):
    resume = tmp_path / "resume.json"
    resume.write_text('["resume"]')
    _set_scope(monkeypatch, scope, resume_identities_path=str(resume))

    def fail(*args):
        raise error("合成连接故障")

    monkeypatch.setattr(sqlite3, "connect", fail)
    _read("weibo", scope, {"resume"})


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
def test_query_error_boundaries(scope, monkeypatch, platform, table, error, expected):
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
        _read(platform, scope, expected)
    else:
        with pytest.raises(error):
            discovery_read.existing_platform_identities(platform, **scope)


@pytest.mark.parametrize("error", [OSError, TypeError, json.JSONDecodeError])
def test_resume_read_errors_keep_database(scope, monkeypatch, error):
    _database(scope["db_path"])
    _set_scope(monkeypatch, scope, resume_identities_path="synthetic.json")

    def fail(*args, **kwargs):
        if error is json.JSONDecodeError:
            raise error("合成 JSON 故障", "", 0)
        raise error("合成文件故障")

    monkeypatch.setattr(Path, "read_text", fail)
    _read("weibo", scope, {"post", "url-id", "seen", "excluded"})


@pytest.mark.parametrize("value,default,expected", [(None, 3, 3), ("bad", 3, 3), ("-2", 3, 0), ("0", 3, 0), ("17", 3, 17), (None, -3, 0)])
def test_env_int_reader(monkeypatch, value, default, expected):
    name = "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES"
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    read = worker_inputs.env_int_reader(name, default, environ=os.environ)
    assert read() == expected
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
def test_root_factory_loads_known_once(platform):
    """env 作用域读取归 entry 装配，构造归 for_platform；已知 ID 只读取一次。"""
    known = {"known"}
    calls = []

    def read(value):
        calls.append(value)
        return known

    accumulator = candidates.AdaptiveAccumulator.for_platform(platform, existing_identities=read(platform))
    assert isinstance(accumulator, candidates.AdaptiveAccumulator)
    assert accumulator.existing_identities is known
    assert accumulator.stagnation_basis == ("candidate_identity" if platform == "weibo" else "valid_new")
    for index in range(5):
        accumulator.begin_batch()
        assert accumulator.is_known("known")
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


def _root_event_sequence():
    root_events = []
    root = candidates.AdaptiveAccumulator(
        "weibo", stagnation_basis="candidate_identity", existing_identities={"known"},
        event_sink=lambda event_type, details: root_events.append((event_type, details)),
    )
    _event_sequence(root)
    return root_events


def _check_event_sequence(root_events):
    assert [event_type for event_type, _ in root_events] == [
        "adaptive_batch_completed", "adaptive_search_stopped",
    ]
    assert root_events[0][1]["candidate_count"] == 3
    assert root_events[1][1]["stop_reason"] == "source_exhausted"


def test_root_injected_event_sequence():
    """注入的事件出口按批次完成、来源耗尽的顺序收到事件；字段的逐项断言见 tests/application/test_discovery.py。"""
    _check_event_sequence(_root_event_sequence())


def test_explicit_sink_is_excluded_from_equality_and_repr():
    events = []
    explicit = candidates.AdaptiveAccumulator("weibo", event_sink=lambda *event: events.append(event))
    default = candidates.AdaptiveAccumulator("weibo")
    assert explicit == default
    assert repr(explicit) == repr(default)
    assert "event_sink" not in repr(explicit)
    _event_sequence(explicit)
    assert len(events) == 2


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


@pytest.mark.parametrize(("platform", "url", "required"), [
    ("bilibili", "https://www.bilibili.com/", ()), ("weibo", "https://m.weibo.cn/", ()),
    ("douyin", "https://www.douyin.com/", ()), ("zhihu", "https://www.zhihu.com/", ("d_c0", "z_c0")),
    ("xhs", "https://www.xiaohongshu.com/", ()),
])
def test_platform_cookie_url_and_required_names(platform, url, required):
    assert cookies.platform_cookie_url(platform) == url
    assert cookies.required_cookie_names(platform) == required


def test_unknown_platform_has_no_cookie_url_or_required_names():
    with pytest.raises(KeyError):
        cookies.platform_cookie_url("unknown")
    assert cookies.required_cookie_names("unknown") == ()


def test_cookie_pure_functions(monkeypatch):
    monkeypatch.setattr(cookies.time, "time", lambda: 100)
    # 跳过空项、空值与已过期（expires < 现在）；会话 cookie（-1）与恰好到期的保留，名字去首尾空格。
    values = [None, {}, {"name": "empty", "value": ""}, {"name": "zero", "value": 0},
              {"name": " x ", "value": "a=b", "expires": 101}, {"name": "old", "value": "v", "expires": 99},
              {"name": "session", "value": "v", "expires": -1}, {"name": "equal", "value": "v", "expires": 100}]
    assert cookies.cookies_to_header(values) == "zero=0;x=a=b;session=v;equal=v"
    assert cookies.cookie_names_from_header("") == []
    assert cookies.cookie_names_from_header("a=1;a=2; =x;bare;b=a=b") == ["a", "b"]
    assert cookies.cookie_names_from_header(" z_c0=x ;d_c0=y") == ["d_c0", "z_c0"]
    # #112：按首个 "=" 切分，值含 "=" 的 cookie 不丢失；同名取后者。
    assert cookies.convert_str_cookie_to_dict("a=1;a=2; =x;bare;b=a=b") == {"a": "2", "": "x", "b": "a=b"}
    assert cookies.convert_str_cookie_to_dict("") == {}
    assert cookies.convert_str_cookie_to_dict(" z_c0=x ;d_c0=y") == {"z_c0": "x", "d_c0": "y"}
    assert cookies.convert_cookies(None) == cookies.convert_cookies([]) == ("", {})
    assert cookies.convert_cookies([{"name": "a", "value": "b"}, {"name": "a", "value": "c"}]) == ("a=b;a=c", {"a": "c"})
    export = {"cookie_header": "synthetic", "source": "snapshot", "cookie_names": ["a"]}
    assert cookies.public_cookie_export(export) == {"source": "snapshot", "cookie_names": ["a"]}


@pytest.mark.parametrize("content", [None, "{", '{"cookies":{}}', '{"cookies":[{"name":"d_c0","value":"x"}]}'])
def test_cookie_snapshot_rejects_missing_broken_or_incomplete(monkeypatch, tmp_path, content):
    path = tmp_path / "snapshot.json"
    monkeypatch.setattr(cookies, "cookie_snapshot_path", lambda _: path)
    if content is not None:
        path.write_text(content)
    assert cookies.load_cookie_snapshot("zhihu") is None


def test_cookie_snapshot_returns_header_names_and_user_agent(monkeypatch, tmp_path):
    path = tmp_path / "snapshot.json"
    monkeypatch.setattr(cookies, "cookie_snapshot_path", lambda _: path)
    path.write_text('{"saved_at":"now","cookies":[{"name":"d_c0","value":"x"},{"name":"z_c0","value":"y"}]}')
    # #127：快照多返回 warmup 保存的 user_agent，旧快照无该字段时为空串。
    assert cookies.load_cookie_snapshot("zhihu") == {
        "cookie_header": "d_c0=x;z_c0=y", "source": "snapshot", "snapshot_path": str(path), "saved_at": "now",
        "cookie_names": ["d_c0", "z_c0"], "required_cookie_names": ["d_c0", "z_c0"], "user_agent": "",
    }
    path.write_text("null")
    with pytest.raises(AttributeError):
        cookies.load_cookie_snapshot("zhihu")


@pytest.mark.asyncio
@pytest.mark.parametrize("urls", [None, [], ["https://example.invalid/"]])
async def test_context_cookie_call_shape(urls):
    traces = []

    class Context:
        async def cookies(self, **kwargs):
            traces.append(kwargs)
            return [{"name": "a", "value": "b"}]

    assert await cookies.convert_browser_context_cookies(Context(), urls) == ("a=b", {"a": "b"})
    assert traces == ([{"urls": urls}] if urls else [{}])


def test_helper_html_text_and_url_params():
    assert helpers.extract_text_from_html("") == ""
    assert helpers.extract_text_from_html("<script>x</script><style>y</style><b>文本</b>") == "文本"
    assert helpers.extract_url_params_to_dict("") == {}
    assert helpers.extract_url_params_to_dict("https://example.invalid/?a=1&a=2&empty=&b=x%20y") == {"a": "2", "b": "x y"}
    assert helpers.extract_url_params_to_dict("no-query") == {}
