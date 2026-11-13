"""#185 知乎详情：假页面、假新标签、假 js-initialData。不访问平台、不启动浏览器。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from .support import END, config, make_crawler, search_page
from trippostcollect.platforms.zhihu.detail_page import blocking_code_for_url
from trippostcollect.platforms.zhihu.models import ZhihuContent
from trippostcollect.platforms.zhihu.page_scripts import STATE_SCRIPTS
from trippostcollect.platforms.zhihu.search_capture import ZhihuPageCapture
from trippostcollect.records.formal import row_for_record, validate_formal_record
from trippostcollect.runtime.page_capture import PageCaptureChannel


def _state(content_id: str, *, kind: str = "answer", content: str = "<p>详情正文</p>",
           voteup=7, comment=1, fav=4, share=0, omit=()) -> str:
    entity = {
        "id": content_id,
        "type": kind,
        "content": content,
        "createdTime": 1_700_000_000,
        "updatedTime": 1_700_000_100,
        "author": {"id": "author-1", "name": "作者", "url_token": "author", "urlToken": "author"},
    }
    if kind == "answer":
        entity["question"] = {"id": "10"}
    if "voteupCount" not in omit:
        entity["voteupCount"] = voteup
    if "commentCount" not in omit:
        entity["commentCount"] = comment
    if "favlistsCount" not in omit:
        entity["favlistsCount"] = fav
    if "shareCount" not in omit:
        entity["reaction"] = {"statistics": {"shareCount": share}}
    bucket = "answers" if kind == "answer" else "articles"
    return json.dumps({"initialState": {"entities": {bucket: {content_id: entity}}}})


def _author_state(*, omit=(), follower=11, following=5, answers=2, articles=3, pins=1, questions=4) -> str:
    user = {"urlToken": "author", "name": "作者"}
    if "followerCount" not in omit:
        user["followerCount"] = follower
    if "followingCount" not in omit:
        user["followingCount"] = following
    if "answerCount" not in omit:
        user["answerCount"] = answers
    if "articlesCount" not in omit:
        user["articlesCount"] = articles
    if "pinsCount" not in omit:
        user["pinsCount"] = pins
    if "questionCount" not in omit:
        user["questionCount"] = questions
    return json.dumps({"initialState": {"entities": {"users": {"author": user}}}})


class QuietMouse:
    async def move(self, *args, **kwargs):
        return None

    async def wheel(self, dx, dy):
        return None


class Locator:
    def __init__(self, page, selector, index=None):
        self.page = page
        self.selector = selector
        self.index = index

    def _matches(self):
        if "/people/" in self.selector:
            return [
                link for link in getattr(self.page, "author_links", [])
                if str(link.get("href") or "").rsplit("/", 1)[-1] in self.selector
            ]
        if self.selector in self.page.markers:
            return [{"marker": self.selector}] if self.page.markers[self.selector] else []
        matched = [
            card for card in self.page.cards
            if card["id"] in self.selector and (
                ("/answer/" in self.selector and card["type"] == "answer")
                or ("/p/" in self.selector and card["type"] == "article")
            )
        ]
        if self.index is None:
            return matched
        return matched[self.index:self.index + 1] if self.index < len(matched) else []

    async def count(self):
        return len(self._matches())

    def nth(self, index):
        return Locator(self.page, self.selector, index)

    @property
    def first(self):
        return self.nth(0)

    async def get_attribute(self, name):
        matched = self._matches()
        if not matched or "href" not in matched[0]:
            return None
        return matched[0]["href"] if name == "href" else None

    async def is_visible(self):
        return bool(self._matches())

    async def click(self, timeout=None):
        matched = self._matches()
        if not matched:
            raise AssertionError(self.selector)
        self.page.clicks.append(matched[0]["id"])
        if self.page.context is None:
            return
        if matched[0].get("kind") == "author":
            self.page.context.spawn_author(matched[0])
        else:
            self.page.context.spawn(matched[0])


class Page:
    def __init__(self, url="https://www.zhihu.com/search?type=content&q=qingdao"):
        self.url = url
        self.cards = []
        self.author_links = []
        self.author_block = ""
        self.author_state = ""
        self.markers = {}
        self.clicks = []
        self.initial_text = ""
        self.closed = False
        self.context = None
        self.mouse = QuietMouse()
        self.listeners = {"response": [], "requestfailed": []}
        self.fronts = 0

    def is_closed(self):
        return self.closed

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def locator(self, selector):
        return Locator(self, selector)

    async def evaluate(self, script, arg=None):
        if "js-initialData" in script:
            return self.initial_text
        if "innerWidth" in script:
            return {"width": 1450, "height": 900}
        if "scrollY" in script or "scrollingElement" in script:
            return {
                "window_y": 0, "scrollable_count": 1, "scroll_top_sum": 0, "max_scroll_top": 0,
            }
        raise AssertionError(script)

    async def wait_for_load_state(self, state, **kwargs):
        return None

    async def wait_for_timeout(self, timeout):
        return None

    async def bring_to_front(self):
        self.fronts += 1

    async def close(self):
        self.closed = True

    async def goto(self, url, **kwargs):
        self.url = url


class ExpectPage:
    def __init__(self, context):
        self.context = context
        self.page = None

    async def __aenter__(self):
        self.context.pending = self
        return self

    async def __aexit__(self, *args):
        self.context.pending = None
        return False

    @property
    def value(self):
        async def get():
            if self.page is None:
                raise TimeoutError("detail tab was not opened")
            return self.page
        return get()


class Context:
    def __init__(self, page):
        self.page = page
        self.pages = [page]
        self.pending = None
        self._on_page = None
        page.context = self

    def on(self, event, callback):
        assert event == "page"
        self._on_page = callback

    def expect_page(self, timeout=None):
        return ExpectPage(self)

    def spawn(self, card):
        if self.page.block == "no-tab":
            return
        detail = Page()
        detail.context = self
        if self.page.block == "login":
            detail.url = "https://www.zhihu.com/signin"
        elif self.page.block == "verify":
            detail.url = "https://www.zhihu.com/question/10/answer/" + card["id"]
            detail.markers = {"text=安全验证": True}
        elif self.page.block == "closed":
            detail.closed = True
        else:
            detail.url = card["href"]
            detail.initial_text = self.page.state_for(card)
        detail.author_links = [] if self.page.author_block == "no-author" else [{
            "kind": "author", "id": "author",
            "href": "https://www.zhihu.com/people/author",
        }]
        self.pages.append(detail)
        if self.pending is not None:
            self.pending.page = detail
        if self._on_page is not None:
            self._on_page(detail)

    def spawn_author(self, link):
        if self.page.author_block == "no-tab":
            return
        author = Page(link["href"])
        author.context = self
        author.initial_text = self.page.author_state
        if self.page.author_block == "login":
            author.url = "https://www.zhihu.com/signin"
        elif self.page.author_block == "verify":
            author.url = "https://www.zhihu.com/account/unhuman"
        elif self.page.author_block == "closed":
            author.closed = True
        self.pages.append(author)
        if self.pending is not None:
            self.pending.page = author

    async def new_page(self):
        detail = Page("about:blank")
        detail.context = self
        self.pages.append(detail)
        return detail


def prepare(monkeypatch, tmp_path, *, block=""):
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "KEYWORDS", "青岛旅游")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(tmp_path / "behavior.json"))
    (tmp_path / "state.json").write_text('{"events": []}', encoding="utf-8")
    page = Page()
    page.block = block
    page.author_state = _author_state()
    page.state_for = lambda card: _state(card["id"], kind=card["type"])
    context = Context(page)
    crawler = make_crawler()
    crawler.context_page = page
    crawler.browser_context = context
    crawler._page_capture = ZhihuPageCapture.open(context)
    crawler._page_capture.install(page)
    crawler._opened_search_keyword = "青岛旅游"
    crawler.zhihu_client = SimpleNamespace(set_search_referer=lambda referer: None)

    async def sleep(_seconds):
        return None

    import trippostcollect.platforms.zhihu.detail_page as detail_page

    monkeypatch.setattr(detail_page.asyncio, "sleep", sleep)
    monkeypatch.setattr(detail_page.random, "uniform", lambda _low, _high: 6.5)
    return crawler, page, tmp_path


def ledger_lines(tmp_path):
    path = tmp_path / "behavior.content_visits.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_login_and_verify_url_constants_are_hypotheses():
    assert blocking_code_for_url("https://www.zhihu.com/signin?next=%2F") == "login_required"
    assert blocking_code_for_url("https://www.zhihu.com/account/unhuman") == "captcha_detected"
    assert blocking_code_for_url("https://www.zhihu.com/search?type=content&q=1") is None
    assert "zhihu.initial_data" in STATE_SCRIPTS
    assert "fetch(" not in STATE_SCRIPTS["zhihu.initial_data"]
    assert "XMLHttpRequest" not in STATE_SCRIPTS["zhihu.initial_data"]


@pytest.mark.asyncio
async def test_click_opens_tab_and_reads_entity_even_when_search_body_is_complete(monkeypatch, tmp_path):
    crawler, page, root = prepare(monkeypatch, tmp_path)
    page.cards = [{
        "id": "101",
        "type": "answer",
        "href": "https://www.zhihu.com/question/10/answer/101",
    }]
    page.state_for = lambda card: _state(
        "101",
        content="<p>详情才算正文</p><img data-original='https://pic1.zhimg.com/v2-detail_r.jpg'>",
        share=0,
    )
    content = ZhihuContent(
        content_id="101",
        question_id="10",
        content_type="answer",
        content_text="搜索里已经完整",
        image_list=["https://pic1.zhimg.com/v2-search_r.jpg"],
        voteup_count=99,
        comment_count=99,
        created_time=1_600_000_000,
        creator_hash="author-1",
        user_nickname="作者",
        followers_observed=True,
        author_followers_source="search_author",
        followers_count=3,
    )

    result = await crawler.enrich_search_content_detail(content)

    assert page.clicks == ["101"]
    assert result.content_text == "详情才算正文"
    assert result.content_detail_source == "answer_detail"
    assert result.voteup_count == 7
    assert result.comment_count == 1
    assert result.collected_count == 4
    assert result.share_count == 0
    assert result.created_time == 1_700_000_000
    assert result.creator_hash == "author-1"
    assert result.creator_url_token == "author"
    assert result.followers_count == 11
    assert result.followers_observed is True
    assert result.author_followers_source == "author_page"
    assert result.following_count == 5
    assert result.author_posts_count == 5
    assert result.author_page_counts == {
        "answerCount": 2, "articlesCount": 3, "pinsCount": 1, "questionCount": 4,
    }
    refs = result.content_visit_refs
    assert refs["note_detail"] and refs["creator_profile"] and refs["images"] == []
    lines = ledger_lines(root)
    actions = [line["action"] for line in lines if line["kind"] == "action"]
    assert actions == ["click_card", "click_author", "close_tab", "close_tab"]
    visits = [line for line in lines if line["kind"] == "visit" and line["stage"] in {"note_detail", "creator_profile"}]
    assert [line["stage"] for line in visits] == ["note_detail", "creator_profile"]
    assert visits[0]["id"] == refs["note_detail"]
    assert visits[1]["id"] == refs["creator_profile"]
    assert visits[1]["url_user_id"] == "author"
    assert visits[1]["subject_id"] == "author"
    pauses = [line["stage"] for line in lines if line["kind"] == "pause"]
    assert pauses == ["note_detail", "creator_profile", "author_tab"]
    assert page.fronts == 1
    dumped = result.model_dump()
    row = row_for_record(
        "zhihu", dumped, artifact_dir="artifact", captured_at="2026-11-13T00:00:00+08:00",
        keyword="青岛旅游", materialized_images=None,
        image_items_for_record=lambda _platform, _record: [],
        ImagePersistenceError=RuntimeError,
    )
    assert row["post_likes_count"] == 7
    assert row["post_comments_count"] == 1
    assert row["post_favorites_count"] == 4
    assert row["post_shares_count"] == 0
    author = json.loads(row["author_json"])
    assert author["following_count"] == 5
    assert author["posts_count"] == 5
    assert author["answerCount"] == 2
    assert author["articlesCount"] == 3
    assert author["pinsCount"] == 1
    assert author["questionCount"] == 4
    assert author["followers_source"] == "author_page"


@pytest.mark.asyncio
async def test_missing_count_keys_are_none_and_fail_validation(monkeypatch, tmp_path):
    crawler, page, _root = prepare(monkeypatch, tmp_path)
    page.cards = [{
        "id": "101", "type": "answer",
        "href": "https://www.zhihu.com/question/10/answer/101",
    }]
    page.state_for = lambda card: _state("101", omit=("voteupCount", "commentCount", "favlistsCount", "shareCount"))
    content = ZhihuContent(
        content_id="101", question_id="10", content_type="answer",
        creator_hash="author-1", user_nickname="作者", followers_observed=True,
        author_followers_source="search_author", followers_count=3,
        image_list=["https://pic1.zhimg.com/v2-search_r.jpg"],
    )

    result = await crawler.enrich_search_content_detail(content)

    assert result.voteup_count is None
    assert result.comment_count is None
    assert result.collected_count is None
    assert result.share_count is None
    record = result.model_dump()
    record["image_list"] = ["https://pic1.zhimg.com/v2-detail_r.jpg"]
    checked = validate_formal_record(
        "zhihu", record, set(),
        content_image_candidates=lambda _platform, item: item.get("image_list") or [],
    )
    assert checked["valid"] is False
    missing = next(reason for reason in checked["reasons"] if reason.startswith("missing_metrics:"))
    assert "voteup_count/liked_count" in missing
    assert "comment_count/comments_count" in missing
    assert "collected_count" in missing
    assert "share_count" in missing


def test_explicit_zero_counts_stay_zero_and_pass_metrics():
    record = {
        "content_id": "101",
        "content_type": "answer",
        "content_text": "正文",
        "content_detail_status": "detail_observed",
        "content_detail_source": "answer_detail",
        "created_time": 1_700_000_000,
        "creator_hash": "author-1",
        "user_nickname": "作者",
        "followers_count": 3,
        "followers_observed": True,
        "author_followers_source": "author_page",
        "image_list": ["https://pic1.zhimg.com/v2-detail_r.jpg"],
        "voteup_count": 0,
        "comment_count": 0,
        "collected_count": 0,
        "share_count": 0,
    }
    checked = validate_formal_record(
        "zhihu", record, set(),
        content_image_candidates=lambda _platform, item: item.get("image_list") or [],
    )
    assert checked["valid"] is True


def test_followers_from_search_results_are_not_a_formal_source():
    """#185 实测回归：知乎粉丝只认作者页；搜索结果的 follower_count 判为不可信来源。"""
    record = {
        "content_id": "101",
        "content_type": "answer",
        "content_text": "正文",
        "content_detail_status": "detail_observed",
        "content_detail_source": "answer_detail",
        "created_time": 1_700_000_000,
        "creator_hash": "author-1",
        "user_nickname": "作者",
        "followers_count": 3,
        "followers_observed": True,
        "author_followers_source": "search_author",
        "image_list": ["https://pic1.zhimg.com/v2-detail_r.jpg"],
        "voteup_count": 1,
        "comment_count": 1,
        "collected_count": 1,
        "share_count": 1,
    }
    checked = validate_formal_record(
        "zhihu", record, set(),
        content_image_candidates=lambda _platform, item: item.get("image_list") or [],
    )
    assert checked["valid"] is False
    assert "untrusted_followers_source" in checked["reasons"]

@pytest.mark.asyncio
async def test_card_miss_retries_then_skips_without_stopping_the_run(monkeypatch, tmp_path):
    crawler, page, root = prepare(monkeypatch, tmp_path)
    content = ZhihuContent(
        content_id="404", question_id="10", content_type="answer", content_text="搜索摘要",
    )
    crawler._next_search_page = __import__("unittest").mock.AsyncMock(side_effect=[
        search_page([content]), END,
    ])
    store = __import__("unittest").mock.AsyncMock()
    monkeypatch.setattr(crawler, "_store_content", store)

    await crawler.search()

    store.assert_not_awaited()
    events = json.loads((root / "state.json").read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    assert skipped[0]["details"]["identity"] == "404"
    assert skipped[0]["details"]["error_code"] == "detail_card_missing"
    assert skipped[0]["details"]["attempts"] == 3
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert "404" in stopped["details"]["candidate_identities"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block", "code"),
    [("login", "login_required"), ("verify", "captcha_detected"), ("closed", "browser_target_closed")],
)
async def test_login_verify_or_closed_page_stops_without_seen(monkeypatch, tmp_path, block, code):
    crawler, page, root = prepare(monkeypatch, tmp_path, block=block)
    if block != "closed":
        page.cards = [{
            "id": "101", "type": "answer",
            "href": "https://www.zhihu.com/question/10/answer/101",
        }]
    else:
        page.closed = True
    content = ZhihuContent(content_id="101", question_id="10", content_type="answer")
    crawler._next_search_page = __import__("unittest").mock.AsyncMock(side_effect=[
        search_page([content]), END,
    ])

    await crawler.search()

    events = json.loads((root / "state.json").read_text(encoding="utf-8"))["events"]
    assert not [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == code
    assert "101" not in stopped["details"]["candidate_identities"]


def test_capture_channel_registers_initial_data_script():
    channel = PageCaptureChannel(
        SimpleNamespace(on=lambda *args: None, pages=[]),
        platform="zhihu",
        rules=(),
        ledger=SimpleNamespace(),
        state_scripts=STATE_SCRIPTS,
    )
    assert channel._state_scripts["zhihu.initial_data"].startswith("() =>")
    assert channel._state_scripts["zhihu.user_agent"] == "() => navigator.userAgent"


@pytest.mark.asyncio
async def test_missing_author_link_is_candidate_failure(monkeypatch, tmp_path):
    from trippostcollect.platforms.zhihu.detail_page import AUTHOR_LINK_MISSING
    from trippostcollect.platforms.zhihu.models import ZhihuDetailFetchError

    crawler, page, _root = prepare(monkeypatch, tmp_path)
    page.author_block = "no-author"
    page.cards = [{
        "id": "101", "type": "answer",
        "href": "https://www.zhihu.com/question/10/answer/101",
    }]
    content = ZhihuContent(
        content_id="101", question_id="10", content_type="answer",
        creator_hash="author-1", user_nickname="作者",
    )
    with pytest.raises(ZhihuDetailFetchError) as caught:
        await crawler.enrich_search_content_detail(content)
    assert caught.value.code == AUTHOR_LINK_MISSING
    assert caught.value.runtime_blocking is False
    assert caught.value.attempts == 3


@pytest.mark.asyncio
async def test_author_page_without_user_is_candidate_failure(monkeypatch, tmp_path):
    from trippostcollect.platforms.zhihu.detail_page import AUTHOR_PROFILE_MISSING
    from trippostcollect.platforms.zhihu.models import ZhihuDetailFetchError

    crawler, page, _root = prepare(monkeypatch, tmp_path)
    page.author_state = json.dumps({"initialState": {"entities": {"users": {}}}})
    page.cards = [{
        "id": "101", "type": "answer",
        "href": "https://www.zhihu.com/question/10/answer/101",
    }]
    content = ZhihuContent(
        content_id="101", question_id="10", content_type="answer",
        creator_hash="author-1", user_nickname="作者",
    )
    with pytest.raises(ZhihuDetailFetchError) as caught:
        await crawler.enrich_search_content_detail(content)
    assert caught.value.code == AUTHOR_PROFILE_MISSING
    assert caught.value.runtime_blocking is False


@pytest.mark.asyncio
@pytest.mark.parametrize("block,code", [("login", "login_required"), ("verify", "captcha_detected")])
async def test_author_login_or_verify_stops_the_run(monkeypatch, tmp_path, block, code):
    from trippostcollect.platforms.zhihu.models import ZhihuDetailFetchError

    crawler, page, _root = prepare(monkeypatch, tmp_path)
    page.author_block = block
    page.cards = [{
        "id": "101", "type": "answer",
        "href": "https://www.zhihu.com/question/10/answer/101",
    }]
    content = ZhihuContent(
        content_id="101", question_id="10", content_type="answer",
        creator_hash="author-1", user_nickname="作者",
    )
    with pytest.raises(ZhihuDetailFetchError) as caught:
        await crawler.enrich_search_content_detail(content)
    assert caught.value.runtime_blocking is True
    assert caught.value.code == code
    assert caught.value.attempts == 1


@pytest.mark.asyncio
async def test_missing_work_count_is_none_and_omitted_from_author_json(monkeypatch, tmp_path):
    crawler, page, _root = prepare(monkeypatch, tmp_path)
    page.author_state = _author_state(omit=("articlesCount", "pinsCount"))
    page.cards = [{
        "id": "101", "type": "answer",
        "href": "https://www.zhihu.com/question/10/answer/101",
    }]
    content = ZhihuContent(
        content_id="101", question_id="10", content_type="answer",
        creator_hash="author-1", user_nickname="作者", followers_observed=True,
        followers_count=3, image_list=["https://pic1.zhimg.com/v2-search_r.jpg"],
        content_text="搜索", created_time=1,
    )
    result = await crawler.enrich_search_content_detail(content)
    assert result.author_posts_count is None
    assert "articlesCount" not in result.author_page_counts
    assert "pinsCount" not in result.author_page_counts
    assert result.author_page_counts["answerCount"] == 2
    row = row_for_record(
        "zhihu", result.model_dump(), artifact_dir="artifact",
        captured_at="2026-11-13T00:00:00+08:00", keyword="青岛旅游",
        materialized_images=None, image_items_for_record=lambda _platform, _record: [],
        ImagePersistenceError=RuntimeError,
    )
    author = json.loads(row["author_json"])
    assert "articlesCount" not in author
    assert "pinsCount" not in author
    assert author["answerCount"] == 2
    assert author["posts_count"] is None


def test_client_has_no_detail_http_or_signer():
    from trippostcollect.platforms.zhihu.client import ZhiHuClient
    import execjs

    assert not hasattr(ZhiHuClient, "get_answer_info")
    assert not hasattr(ZhiHuClient, "get_article_info")
    assert not hasattr(ZhiHuClient, "get_video_info")
    assert not hasattr(ZhiHuClient, "_pre_headers")
    assert callable(execjs.compile)
    with pytest.raises(ModuleNotFoundError):
        __import__("trippostcollect.platforms.zhihu.signer")
