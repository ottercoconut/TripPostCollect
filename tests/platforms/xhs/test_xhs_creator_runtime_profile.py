"""#52 作者页取数：运行时白名单投影优先、严格静态解析回退、原因类别与导航诊断。

全部为离线合成数据：不启动浏览器、不访问网络，ID、昵称与 URL 均为虚构值。
"""

import asyncio
import copy
import json
import re
import subprocess
from unittest.mock import AsyncMock, Mock

from playwright._impl._driver import compute_driver_executable
from playwright._impl._errors import TargetClosedError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
import pytest

import trippostcollect.platforms.xhs.author as xhs_author
from trippostcollect.platforms.xhs.client import XiaoHongShuClient
from trippostcollect.platforms.xhs.errors import PlatformRuntimeError, XHSCreatorProfileUnavailable
from trippostcollect.platforms.xhs.parser import (
    XHS_CREATOR_BASIC_FIELDS,
    XHS_CREATOR_INTERACTION_ITEM_FIELDS,
    XHS_CREATOR_INTERACTION_LIST_KEYS,
    XHS_CREATOR_TOP_FIELDS,
    XiaoHongShuExtractor,
    read_creator_runtime_projection,
    update_xhs_note,
    xhs_creator_projection_spec,
)
from trippostcollect.records.sanitization import AUTHOR_AVATAR_KEYS, XHS_SERIALIZED_PROFILE_AVATAR_PATHS

from .support import XiaoHongShuCrawler, client_ports
from .test_xhs_discovery_memory import prepare_crawler


REQUESTED_USER = "synthetic-author-a"
AVATAR_URL = "https://avatar.example.test/synthetic-a.jpg"


def creator_state(*, user_id=None, fans="128", with_fans=True):
    basic = {
        "nickname": "合成作者",
        "desc": "合成简介",
        "gender": 1,
        "ipLocation": "山东",
        "imageb": AVATAR_URL,
        "images": AVATAR_URL,
        "redId": "synthetic-red-id",
    }
    if user_id is not None:
        basic["userId"] = user_id
    interactions = [
        {"type": "follows", "name": "关注", "count": "5"},
        {"type": "interaction", "name": "获赞与收藏", "count": "99"},
    ]
    if with_fans:
        interactions.insert(1, {"type": "fans", "name": "粉丝", "count": fans})
    return {"basicInfo": basic, "interactions": interactions, "tags": [{"icon": AVATAR_URL}]}


def state_html(state_source: str) -> str:
    return (
        "<html><head><title>合成作者 - 小红书</title></head><body>"
        f"<script>window.__INITIAL_STATE__={state_source}</script></body></html>"
    )


def json_state_html(creator) -> str:
    return state_html(json.dumps({"user": {"userPageData": creator}}, ensure_ascii=False))


def legacy_extract_creator_info_from_html(html: str):
    """#52 之前的严格静态解析原文，用于逐项对照返回值。"""
    match = re.search(
        r"<script[^>]*>\s*window\.__INITIAL_STATE__\s*=\s*", html, re.M
    )
    if match is None:
        return None
    state_source = html[match.end() :].replace(":undefined", ":null")
    try:
        info, _ = json.JSONDecoder(strict=False).raw_decode(state_source.lstrip())
    except json.JSONDecodeError:
        return None
    if info is None:
        return None
    user_info = info.get("user")
    if not isinstance(user_info, dict):
        return None
    creator_info = user_info.get("userPageData")
    return creator_info if isinstance(creator_info, dict) else None


CREATOR_JSON = json.dumps(creator_state(), ensure_ascii=False)
NEW_SET_BEFORE_USER = state_html(
    '{"global":{"seen":new Set(["x"])},"user":{"userPageData":' + CREATOR_JSON + "}}"
)
NEW_SET_AFTER_USER = state_html(
    '{"user":{"userPageData":' + CREATOR_JSON + '},"feed":{"ids":new Set([])}}'
)
STATIC_CASES = [
    (json_state_html(creator_state()), "ok"),
    (state_html('{"user":{"userPageData":{"ipLocation":undefined,"interactions":[]}}}'), "ok"),
    (NEW_SET_BEFORE_USER, "state_decode_failed:js_new_expression"),
    (NEW_SET_AFTER_USER, "state_decode_failed:js_new_expression"),
    (state_html('{"user":{"userPageData":{"list":[1,undefined]}}}'), "state_decode_failed:js_identifier"),
    (state_html('{"user":{"userPageData":{"a":-}}}'), "state_decode_failed:invalid_json"),
    ('<script>window.__INITIAL_STATE__={"user":{"userPageData":{"a":1', "state_decode_failed:truncated"),
    ("<html><body>没有状态</body></html>", "state_script_missing"),
    (state_html("null"), "state_null"),
    (state_html('{"note":{}}'), "user_missing"),
    (state_html('{"user":{"userInfo":{}}}'), "user_page_data_missing"),
    (state_html('{"user":{"userPageData":[]}}'), "user_page_data_missing"),
    (state_html('{"user":{"userPageData":{}}}'), "user_page_data_empty"),
]


@pytest.mark.parametrize(("html", "reason"), STATIC_CASES)
def test_strict_static_parse_is_unchanged_and_reports_reason_category(html, reason):
    extractor = XiaoHongShuExtractor()

    creator, observed_reason = extractor.extract_creator_info_with_reason(html)

    assert creator == legacy_extract_creator_info_from_html(html)
    assert extractor.extract_creator_info_from_html(html) == creator
    assert observed_reason == reason
    assert REQUESTED_USER not in observed_reason and "<" not in observed_reason


def test_client_records_static_parse_reason():
    client = XiaoHongShuClient(
        headers={"Cookie": "web_session=test"},
        playwright_page=Mock(),
        cookie_dict={},
        ports=client_ports,
    )
    assert client.last_creator_parse_reason == ""

    assert client.extract_creator_info_from_html(NEW_SET_BEFORE_USER) is None
    assert client.last_creator_parse_reason == "state_decode_failed:js_new_expression"
    assert client.extract_creator_info_from_html(json_state_html(creator_state()))
    assert client.last_creator_parse_reason == "ok"


def test_projection_whitelist_matches_downstream_reads_and_excludes_avatar_and_credentials():
    every_key = (
        set(XHS_CREATOR_TOP_FIELDS)
        | set(XHS_CREATOR_BASIC_FIELDS)
        | set(XHS_CREATOR_INTERACTION_LIST_KEYS)
        | set(XHS_CREATOR_INTERACTION_ITEM_FIELDS)
    )
    lowered = {key.casefold() for key in every_key}
    assert not lowered & AUTHOR_AVATAR_KEYS
    for word in ("avatar", "image", "token", "cookie", "xsec", "session", "secret", "sign"):
        assert not any(word in key for key in lowered), word
    spec = xhs_creator_projection_spec()
    assert spec["basic_keys"] == ["basicInfo", "basic_info", "basic"]
    assert set(spec["basic_fields"]) == set(XHS_CREATOR_BASIC_FIELDS)


def test_projection_spec_takes_avatar_evidence_from_sanitizer():
    spec = xhs_creator_projection_spec()

    assert spec["avatar_keys"] == sorted(AUTHOR_AVATAR_KEYS)
    assert {tuple(path) for path in spec["avatar_paths"]} == set(
        XHS_SERIALIZED_PROFILE_AVATAR_PATHS["creator_profile_json"]
    )
    assert spec["max_depth"] > 0 and spec["max_nodes"] > 0


def test_python_side_refilters_envelope_and_never_accepts_unknown_reasons():
    creator = creator_state(user_id=REQUESTED_USER)
    creator["basicInfo"]["xsec_token"] = "synthetic-token"
    creator["token"] = "synthetic-token"
    creator["desc"] = {"nested": "不是标量"}
    creator["interactions"][0]["icon"] = AVATAR_URL

    projected, reason = read_creator_runtime_projection({"status": "ok", "creator": creator})

    assert reason == "ok"
    serialized = json.dumps(projected, ensure_ascii=False)
    for forbidden in ("imageb", "images", "token", "redId", "tags", "icon", "nested", AVATAR_URL):
        assert forbidden not in serialized
    assert read_creator_runtime_projection({"status": "missing"}) == ({}, "runtime_projection_empty")
    assert read_creator_runtime_projection({"status": "ok", "creator": {"tags": []}}) == (
        {}, "runtime_projection_empty")
    assert read_creator_runtime_projection(
        {"status": "rejected", "reason": "projection_avatar_check_incomplete"}
    ) == ({}, "projection_avatar_check_incomplete")
    assert read_creator_runtime_projection({"status": "rejected", "reason": "<html>"}) == (
        {}, "runtime_projection_error")
    assert read_creator_runtime_projection(None) == ({}, "runtime_projection_error")


# ---------------------------------------------------------------------------
# 投影脚本本身：用 Playwright 自带的 node driver 在 vm 上下文里执行 page.evaluate 使用的同一份常量。
# 只注入合成的 window.__INITIAL_STATE__，不联网、不启动浏览器。

NODE_RUNNER = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const context = vm.createContext({});
vm.runInContext(
    "globalThis.window = {}; globalThis.document = {}; globalThis.__probes = {getter: 0, toJSON: 0};"
    + "globalThis.location = {href: 'https://www.xiaohongshu.com/user/profile/synthetic'};",
    context,
);
vm.runInContext(input.setup, context);
const projector = vm.runInContext('(' + input.script + ')', context);
const spec = vm.runInContext('JSON.parse(' + JSON.stringify(JSON.stringify(input.spec)) + ')', context);
const result = projector(spec);
const probes = vm.runInContext('JSON.parse(JSON.stringify(globalThis.__probes))', context);
process.stdout.write(JSON.stringify({ result, probes }));
"""


def run_projection_script(setup: str, *, spec=None) -> dict:
    node, _cli = compute_driver_executable()
    completed = subprocess.run(
        [str(node), "-e", NODE_RUNNER],
        input=json.dumps({
            "setup": setup,
            "script": xhs_author.XHS_CREATOR_RUNTIME_PROJECTION_SCRIPT,
            "spec": spec or xhs_creator_projection_spec(),
        }),
        capture_output=True,
        text=True,
        timeout=60,
        env={},
        check=True,
    )
    return json.loads(completed.stdout)


SYNTHETIC_AVATAR = "https://avatar.example.test/synthetic-a.jpg"


def page_state(user_page_data: str, *, prelude: str = "") -> str:
    return (
        f"{prelude}\nwindow.__INITIAL_STATE__ = {{"
        f"feed: {{seen: new Set(['x'])}}, user: {{userPageData: {user_page_data}}}}};"
    )


def test_script_drops_desc_equal_to_basic_info_imageb_and_keeps_zero_fans():
    output = run_projection_script(page_state(f"""{{
        basicInfo: {{nickname: '合成作者', desc: '{SYNTHETIC_AVATAR}', gender: 0, ipLocation: '山东',
                     imageb: ' {SYNTHETIC_AVATAR} ', images: ['https://avatar.example.test/b.jpg']}},
        interactions: [{{type: 'fans', name: '粉丝', count: 0}}, {{type: 'follows', count: '5'}}],
    }}"""))

    assert output["result"] == {"status": "ok", "creator": {
        "basicInfo": {"nickname": "合成作者", "gender": 0, "ipLocation": "山东"},
        "interactions": [{"type": "fans", "name": "粉丝", "count": 0}, {"type": "follows", "count": "5"}],
    }}
    assert SYNTHETIC_AVATAR not in json.dumps(output["result"])


def test_script_drops_fields_equal_to_nested_avatar_key_evidence():
    output = run_projection_script(page_state(f"""{{
        extraInfo: {{blocks: [{{Avatar_Url: {{small: '{SYNTHETIC_AVATAR}'}}}}]}},
        nickname: '{SYNTHETIC_AVATAR}',
        basicInfo: {{nickname: '合成作者', desc: '普通简介 https://example.test/page'}},
        interactions: [{{type: 'fans', count: '{SYNTHETIC_AVATAR}'}}, {{type: 'fans', name: '粉丝', count: '1.2万'}}],
    }}"""))

    creator = output["result"]["creator"]
    assert "nickname" not in creator
    assert creator["basicInfo"] == {"nickname": "合成作者", "desc": "普通简介 https://example.test/page"}
    assert creator["interactions"] == [{"type": "fans"}, {"type": "fans", "name": "粉丝", "count": "1.2万"}]
    assert SYNTHETIC_AVATAR not in json.dumps(output)


def test_script_drops_non_scalar_fields_and_rebuilds_interactions_with_fixed_keys():
    output = run_projection_script(page_state("""{
        desc: {text: '对象简介'},
        nickname: ['数组昵称'],
        fans: Infinity,
        basicInfo: {nickname: '合成作者', desc: {text: '对象简介'}, redId: 'synthetic-red', xsec_token: 't'},
        interactions: [
            {type: 'fans', count: '9', icon: 'https://example.test/icon.png', extra: {deep: 1}},
            'not-a-record',
            {type: 'follows', count: {value: 1}},
        ],
        token: 'synthetic-token',
    }"""))

    assert output["result"] == {"status": "ok", "creator": {
        "basicInfo": {"nickname": "合成作者"},
        "interactions": [{"type": "fans", "count": "9"}, {"type": "follows"}],
    }}


def test_script_unwraps_vue_refs():
    output = run_projection_script(page_state(
        """ref({basicInfo: ref({nickname: '合成作者', imageb: 'https://avatar.example.test/r.jpg'}),
               interactions: ref([ref({type: 'fans', count: '7'})])})""",
        prelude="""
        const ref = (value) => ({__v_isRef: true, dep: {}, _rawValue: value, _value: value});
        """,
    ))

    assert output["result"] == {"status": "ok", "creator": {
        "basicInfo": {"nickname": "合成作者"},
        "interactions": [{"type": "fans", "count": "7"}],
    }}


@pytest.mark.parametrize(("setup", "spec_override"), [
    (page_state("loop", prelude="const loop = {basicInfo: {nickname: '合成作者'}}; loop.self = loop;"), {}),
    (page_state("{basicInfo: {nickname: '合成作者', a: {b: {c: {d: 1}}}}}"), {"max_depth": 3}),
    (page_state("{basicInfo: {nickname: '合成作者'}, list: [1, 2, 3, 4, 5, 6]}"), {"max_nodes": 5}),
    (page_state("{basicInfo: {nickname: '合成作者'}, handler: () => 1}"), {}),
    (page_state("{basicInfo: {nickname: '合成作者'}, when: new (class Custom {})()}"), {}),
])
def test_script_rejects_whole_projection_when_avatar_check_is_incomplete(setup, spec_override):
    spec = {**xhs_creator_projection_spec(), **spec_override}

    output = run_projection_script(setup, spec=spec)

    assert output["result"] == {"status": "rejected", "reason": "projection_avatar_check_incomplete"}


def test_script_never_invokes_getters_or_tojson():
    output = run_projection_script(page_state(
        "data",
        prelude="""
        // 不可枚举的 toJSON 不出现在 Object.keys 中，但 JSON.stringify 会调用它；脚本不得触发。
        const trap = () => { globalThis.__probes.toJSON += 1; throw new Error('toJSON'); };
        const data = {basicInfo: {nickname: '合成作者'}, interactions: [{type: 'fans', count: '3'}]};
        Object.defineProperty(data, 'toJSON', {enumerable: false, value: trap});
        Object.defineProperty(data.basicInfo, 'toJSON', {enumerable: false, value: trap});
        """,
    ))
    assert output["result"] == {"status": "ok", "creator": {
        "basicInfo": {"nickname": "合成作者"}, "interactions": [{"type": "fans", "count": "3"}]}}
    assert output["probes"] == {"getter": 0, "toJSON": 0}

    guarded = run_projection_script(page_state(
        "data",
        prelude="""
        const data = {interactions: [{type: 'fans', count: '3'}]};
        Object.defineProperty(data, 'basicInfo', {enumerable: true, get() {
            globalThis.__probes.getter += 1; throw new Error('getter');
        }});
        """,
    ))
    assert guarded["result"] == {"status": "rejected", "reason": "projection_avatar_check_incomplete"}
    assert guarded["probes"] == {"getter": 0, "toJSON": 0}


def test_script_reports_missing_state_without_returning_evidence():
    assert run_projection_script("window.__INITIAL_STATE__ = {user: {}};")["result"] == {"status": "missing"}
    assert run_projection_script("")["result"] == {"status": "missing"}
    output = run_projection_script(page_state(f"{{avatar: '{SYNTHETIC_AVATAR}', basicInfo: {{nickname: 'n'}}}}"))
    assert set(output["result"]) == {"status", "creator"}
    assert SYNTHETIC_AVATAR not in json.dumps(output)


class FakeMouse:
    async def move(self, *args, **kwargs):
        return None

    async def wheel(self, *args, **kwargs):
        return None


class CreatorPage:
    viewport_size = {"width": 1280, "height": 800}

    def __init__(self, projections, *, html="", load_state_error=None):
        self.url = f"https://www.xiaohongshu.com/user/profile/{REQUESTED_USER}"
        self.mouse = FakeMouse()
        self.projections = list(projections)
        self.html = html
        self.load_state_error = load_state_error
        self.load_states = []
        self.projection_calls = 0
        self.content_calls = 0
        self.closed = False

    async def wait_for_timeout(self, milliseconds):
        return None

    async def wait_for_load_state(self, state, timeout=None):
        self.load_states.append((state, timeout))
        if self.load_state_error is not None:
            raise self.load_state_error

    async def evaluate(self, script, arg=None):
        if "ready_state" in script:
            return {"ready_state": "complete", "title": "合成作者 - 小红书", "dom_length": 290_000,
                    "body_text_length": 800, "body_child_element_count": 3, "visibility_state": "visible"}
        assert script == xhs_author.XHS_CREATOR_RUNTIME_PROJECTION_SCRIPT
        assert arg == xhs_creator_projection_spec()
        self.projection_calls += 1
        value = self.projections.pop(0) if len(self.projections) > 1 else self.projections[0]
        if isinstance(value, BaseException):
            raise value
        # 合成的页面返回信封：None 表示页面没有作者状态，普通 dict 表示投影成功。
        if value is None:
            return {"status": "missing"}
        if "status" not in value:
            return {"status": "ok", "creator": copy.deepcopy(value)}
        return copy.deepcopy(value)

    async def content(self):
        self.content_calls += 1
        return self.html

    def is_closed(self):
        return self.closed


class StaticClient:
    """只暴露浏览器回退会用到的静态解析；与真实客户端一样记录原因类别。"""

    def __init__(self, api_creator=None, api_html=""):
        self._extractor = XiaoHongShuExtractor()
        self.last_creator_parse_reason = ""
        self.api_creator = api_creator
        self.api_html = api_html
        self.api_calls = []
        self.static_calls = 0

    async def get_creator_info(self, **kwargs):
        self.api_calls.append(kwargs)
        if self.api_html:
            return self.extract_creator_info_from_html(self.api_html)
        return self.api_creator

    def extract_creator_info_from_html(self, html):
        self.static_calls += 1
        creator, self.last_creator_parse_reason = self._extractor.extract_creator_info_with_reason(html)
        return creator


@pytest.fixture
def crawler(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_ENRICH_CREATORS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(tmp_path / "behavior_evidence.json"))
    instance = XiaoHongShuCrawler()
    instance._guarded_pause = AsyncMock(return_value=0.0)
    instance.context_page = Mock()
    instance.context_page.is_closed.return_value = False
    instance._popup_checkpoint_state = AsyncMock(return_value={})
    instance._goto_with_deadline = AsyncMock()
    instance._close_page_with_deadline = AsyncMock()
    instance.xhs_client = StaticClient()
    clock = {"now": 100.0}
    instance.sleeps = []

    async def sleep(seconds):
        instance.sleeps.append(seconds)
        clock["now"] += seconds

    instance._popup_monotonic = lambda: clock["now"]
    instance._popup_sleep = sleep
    instance.visible_markers = []

    async def inspect_state(page):
        markers = instance.visible_markers.pop(0) if instance.visible_markers else {}
        return "", markers

    monkeypatch.setattr(xhs_author, "inspect_visible_page_state", inspect_state)
    instance.navigation_path = tmp_path / "behavior_evidence.navigation.json"
    return instance


def parse_diagnostics(instance):
    return [event for event in instance._navigation_diagnostics if event["stage"] == "creator_profile_parse"]


def assert_diagnostic_is_safe(instance):
    text = instance.navigation_path.read_text(encoding="utf-8")
    for forbidden in ("avatar", "imageb", AVATAR_URL, "<html", "__INITIAL_STATE__", "合成简介"):
        assert forbidden not in text


async def open_with(crawler, page):
    crawler._new_guarded_page = AsyncMock(return_value=page)
    return await crawler._get_creator_info_from_browser(REQUESTED_USER)


@pytest.mark.asyncio
async def test_new_set_ssr_fails_static_parse_but_runtime_projection_reads_creator(crawler):
    assert XiaoHongShuExtractor().extract_creator_info_from_html(NEW_SET_BEFORE_USER) is None
    page = CreatorPage([creator_state()], html=NEW_SET_BEFORE_USER)

    creator = await open_with(crawler, page)

    assert creator["interactions"][1] == {"type": "fans", "name": "粉丝", "count": "128"}
    assert "imageb" not in creator["basicInfo"] and "images" not in creator["basicInfo"]
    assert page.content_calls == 0 and crawler.xhs_client.static_calls == 0
    assert page.load_states == [("domcontentloaded", 10_000)]
    assert [event["outcome"] for event in parse_diagnostics(crawler)] == ["ok"]
    assert crawler._creator_browser_reason == ""
    assert_diagnostic_is_safe(crawler)
    crawler._close_page_with_deadline.assert_awaited_once_with(page, reason="creator_profile_cleanup")


@pytest.mark.asyncio
async def test_empty_projection_falls_back_to_strict_static_parse(crawler):
    page = CreatorPage([None], html=json_state_html(creator_state()))

    creator = await open_with(crawler, page)

    assert creator == creator_state()
    assert page.content_calls == 1 and crawler.xhs_client.static_calls == 1
    assert sum(crawler.sleeps) == pytest.approx(10.0)
    assert [event["outcome"] for event in parse_diagnostics(crawler)] == ["ok_static_state"]


@pytest.mark.asyncio
async def test_readiness_retries_within_budget_until_fans_are_observed(crawler):
    page = CreatorPage([
        None,
        creator_state(with_fans=False),
        creator_state(with_fans=False),
        creator_state(fans="1.2万"),
    ])

    creator = await open_with(crawler, page)

    assert page.projection_calls == 4
    assert crawler.sleeps == [0.5, 0.5, 0.5]
    assert creator["interactions"][1]["count"] == "1.2万"
    assert page.content_calls == 0


@pytest.mark.asyncio
async def test_budget_exhaustion_without_fans_reports_followers_unobserved(crawler):
    page = CreatorPage([creator_state(with_fans=False)], html=NEW_SET_AFTER_USER)

    creator = await open_with(crawler, page)

    assert creator is None
    assert sum(crawler.sleeps) == pytest.approx(10.0)
    assert crawler._creator_browser_reason == (
        "followers_unobserved,static_state_decode_failed:js_new_expression"
    )
    assert [event["outcome"] for event in parse_diagnostics(crawler)] == [crawler._creator_browser_reason]
    assert_diagnostic_is_safe(crawler)


@pytest.mark.asyncio
async def test_page_not_ready_is_recorded_and_projection_still_runs(crawler):
    page = CreatorPage([None], html="<html><body></body></html>",
                       load_state_error=PlaywrightTimeoutError("domcontentloaded 超时"))

    creator = await open_with(crawler, page)

    assert creator is None
    assert page.projection_calls > 1
    assert crawler._creator_browser_reason == (
        "page_not_ready,runtime_projection_empty,static_state_script_missing"
    )


@pytest.mark.asyncio
async def test_mismatched_page_author_is_unavailable_without_static_fallback(crawler):
    page = CreatorPage([creator_state(user_id="synthetic-author-other")],
                       html=json_state_html(creator_state(user_id="synthetic-author-other")))

    creator = await open_with(crawler, page)

    assert creator is None
    assert page.projection_calls == 1 and page.content_calls == 0
    assert crawler._creator_browser_reason == "creator_mismatch"
    assert [event["outcome"] for event in parse_diagnostics(crawler)] == ["creator_mismatch"]


@pytest.mark.asyncio
async def test_rejected_projection_falls_back_to_static_parse_without_waiting(crawler):
    page = CreatorPage([{"status": "rejected", "reason": "projection_avatar_check_incomplete"}],
                       html=NEW_SET_BEFORE_USER)

    creator = await open_with(crawler, page)

    assert creator is None
    assert page.projection_calls == 1 and crawler.sleeps == [] and page.content_calls == 1
    assert crawler._creator_browser_reason == (
        "projection_avatar_check_incomplete,static_state_decode_failed:js_new_expression"
    )


@pytest.mark.asyncio
async def test_missing_page_user_id_uses_note_author_id(crawler):
    page = CreatorPage([creator_state()])
    crawler._new_guarded_page = AsyncMock(return_value=page)
    note = {"note_id": "synthetic-note", "user": {"user_id": REQUESTED_USER, "nickname": "合成作者"},
            "xsec_token": "synthetic-note-token", "interact_info": {}}

    await crawler.enrich_note_creator(note)

    assert "userId" not in note["creator_profile"]["basicInfo"]
    record = update_xhs_note(note, source_keyword="青岛", current_timestamp=lambda: 0,
                             save_data_option="jsonl")
    assert record["user_id"] == REQUESTED_USER
    assert record["fans_count"] == "128"
    assert record["followers_observed"] is True
    assert record["author_followers_source"] == "creator_profile"
    assert AVATAR_URL not in record["creator_profile_json"]


@pytest.mark.asyncio
async def test_unit_fans_count_follows_downstream_rules_without_zero_fill(crawler):
    page = CreatorPage([creator_state(fans="1.2万")])
    crawler._new_guarded_page = AsyncMock(return_value=page)
    note = {"note_id": "synthetic-note", "user": {"user_id": REQUESTED_USER}, "interact_info": {}}

    await crawler.enrich_note_creator(note)
    record = update_xhs_note(note, source_keyword="青岛", current_timestamp=lambda: 0,
                             save_data_option="jsonl")

    assert record["fans_count"] == "1.2万" and record["followers_count"] == "1.2万"
    assert record["followers_observed"] is True


@pytest.mark.asyncio
async def test_login_during_readiness_uses_existing_primary_page_recovery(crawler):
    page = CreatorPage([None])
    crawler.visible_markers = [{}, {}, {"login_required": True}]
    crawler._recover_creator_login_on_primary_page = AsyncMock(return_value={"fans": "1"})

    assert await open_with(crawler, page) == {"fans": "1"}
    crawler._recover_creator_login_on_primary_page.assert_awaited_once_with(REQUESTED_USER)
    assert page.content_calls == 0
    crawler._close_page_with_deadline.assert_awaited_once_with(page, reason="creator_profile_cleanup")


@pytest.mark.asyncio
async def test_verification_during_readiness_keeps_existing_wait(crawler):
    page = CreatorPage([None])
    crawler.visible_markers = [{}, {}, {"captcha_or_verify": True}]
    crawler._wait_for_creator_profile_verification = AsyncMock(return_value={"fans": "2"})

    assert await open_with(crawler, page) == {"fans": "2"}
    crawler._wait_for_creator_profile_verification.assert_awaited_once_with(page, REQUESTED_USER)
    assert page.projection_calls == 1


@pytest.mark.asyncio
async def test_security_limit_during_readiness_stops_the_run(crawler, monkeypatch):
    page = CreatorPage([None])
    markers = {"platform_security_limit": True, "captcha_or_verify": True, "login_required": False,
               "rate_limited": False, "blocked": False}
    record_limit = AsyncMock(return_value={"observed_error_code": "300011"})
    monkeypatch.setattr(crawler.ports, "record_platform_security_limit", record_limit)

    async def inspect_state(current_page):
        return "安全限制 300011", markers

    calls = {"count": 0}

    async def staged_inspect(current_page):
        calls["count"] += 1
        if calls["count"] <= 2:
            return "", {}
        return await inspect_state(current_page)

    monkeypatch.setattr(xhs_author, "inspect_visible_page_state", staged_inspect)

    with pytest.raises(PlatformRuntimeError) as exc_info:
        await open_with(crawler, page)

    assert exc_info.value.code == "platform_security_limit_300011"
    assert record_limit.await_args.kwargs["stage"] == f"creator_profile:{REQUESTED_USER}:runtime_wait"
    crawler._close_page_with_deadline.assert_awaited_once_with(page, reason="creator_profile_cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    asyncio.CancelledError(),
    TargetClosedError("Target page, context or browser has been closed"),
])
async def test_cancellation_and_closed_target_propagate(crawler, failure):
    page = CreatorPage([failure])

    with pytest.raises(type(failure)):
        await open_with(crawler, page)

    assert page.content_calls == 0
    crawler._close_page_with_deadline.assert_awaited_once_with(page, reason="creator_profile_cleanup")


@pytest.mark.asyncio
async def test_closed_page_error_propagates_but_transient_evaluate_error_retries(crawler):
    closed_page = CreatorPage([PlaywrightError("Execution context was destroyed")])
    closed_page.closed = True
    with pytest.raises(PlaywrightError):
        await open_with(crawler, closed_page)

    transient = CreatorPage([PlaywrightError("Execution context was destroyed"), creator_state()])
    assert (await open_with(crawler, transient))["interactions"][1]["count"] == "128"
    assert transient.projection_calls == 2


@pytest.mark.asyncio
async def test_both_paths_failing_raise_with_combined_reason_categories(crawler):
    crawler.xhs_client = StaticClient(api_html=NEW_SET_BEFORE_USER)
    page = CreatorPage([None], html=NEW_SET_BEFORE_USER)
    crawler._new_guarded_page = AsyncMock(return_value=page)

    with pytest.raises(XHSCreatorProfileUnavailable) as exc_info:
        await crawler.enrich_note_creator({"user": {"user_id": REQUESTED_USER}})

    assert exc_info.value.attempts == 2
    assert exc_info.value.reason == (
        "api=state_decode_failed:js_new_expression;"
        "browser=runtime_projection_empty,static_state_decode_failed:js_new_expression"
    )
    assert REQUESTED_USER not in exc_info.value.reason
    assert [event["outcome"] for event in parse_diagnostics(crawler)] == [
        "runtime_projection_empty,static_state_decode_failed:js_new_expression"
    ]
    assert_diagnostic_is_safe(crawler)


@pytest.mark.asyncio
async def test_api_request_failure_reason_uses_exception_type(crawler):
    crawler.xhs_client = StaticClient()
    crawler.xhs_client.get_creator_info = AsyncMock(side_effect=ValueError("合成异常 synthetic-author-a"))
    page = CreatorPage([None], html="<html></html>")
    crawler._new_guarded_page = AsyncMock(return_value=page)

    with pytest.raises(XHSCreatorProfileUnavailable) as exc_info:
        await crawler.enrich_note_creator({"user": {"user_id": REQUESTED_USER}})

    assert exc_info.value.reason == (
        "api=api_request_failed:ValueError;browser=runtime_projection_empty,static_state_script_missing"
    )


@pytest.mark.asyncio
async def test_candidate_skip_detail_carries_reason_without_html_or_avatar(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, _, state_path = prepare_crawler(
        monkeypatch,
        tmp_path,
        items=[{"id": "retry-creator"}, {"id": "success-creator"}],
    )
    reason = "api=state_decode_failed:js_new_expression;browser=followers_unobserved,static_state_script_missing"
    crawler.enrich_note_creator = AsyncMock(
        side_effect=[XHSCreatorProfileUnavailable("author-retry", attempts=2, reason=reason), None]
    )

    await crawler.search()

    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event["details"] for event in events if event["type"] == "candidate_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["detail"] == f"creator_profile_failed:{reason}"
    assert skipped[0]["error_code"] == "creator_profile_unavailable"
    assert skipped[0]["attempts"] == 2 and skipped[0]["retryable"] is True
    serialized = json.dumps(skipped[0], ensure_ascii=False)
    assert "avatar" not in serialized and "<html" not in serialized and "author-retry" not in serialized
