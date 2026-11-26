"""#209 知乎按回答 ID 直开：最终地址决定问题 ID。不访问平台、不启动浏览器。"""

from __future__ import annotations

import asyncio

import pytest

from trippostcollect.application.visit_gate import validate_content_visits
from trippostcollect.platforms.zhihu.detail_page import admit_landing_shape, classify_admit_landing
from trippostcollect.platforms.zhihu.search_capture import ZhihuPageCapture
from trippostcollect.application.repair import ADMIT_UNAVAILABLE_CODES as REPAIR_UNAVAILABLE_CODES
from trippostcollect.platforms.zhihu.models import (
    ADMIT_ID_MISMATCH,
    ADMIT_NOT_FOUND,
    ADMIT_REDIRECT_ARTICLE,
    ADMIT_REDIRECT_HOME,
    ADMIT_REDIRECT_UNEXPECTED,
    ADMIT_UNAVAILABLE_CODES,
)
from trippostcollect.records.formal import validate_formal_record

from .test_zhihu_detail_page import _author_state, _state, ledger_lines, prepare


ANSWER = "https://www.zhihu.com/answer/101"
FINAL = "https://www.zhihu.com/question/10/answer/101"
AUTHOR = {"kind": "author", "id": "author", "href": "https://www.zhihu.com/people/author"}


def test_unavailable_codes_match_without_the_app_importing_zhihu() -> None:
    assert REPAIR_UNAVAILABLE_CODES == ADMIT_UNAVAILABLE_CODES


def test_classify_admit_landing_names_the_four_outcomes() -> None:
    assert classify_admit_landing(FINAL, "101", 200) == ("10", "")
    assert classify_admit_landing("https://www.zhihu.com/", "101", 200)[1] == ADMIT_REDIRECT_HOME
    assert classify_admit_landing("https://zhuanlan.zhihu.com/p/101", "101", 200)[1] == ADMIT_REDIRECT_ARTICLE
    assert classify_admit_landing("https://www.zhihu.com/p/101", "101", 200)[1] == ADMIT_REDIRECT_ARTICLE
    assert classify_admit_landing(FINAL, "101", 404)[1] == ADMIT_NOT_FOUND
    assert classify_admit_landing(
        "https://www.zhihu.com/question/10/answer/999", "101", 200,
    )[1] == ADMIT_ID_MISMATCH
    assert classify_admit_landing("https://www.zhihu.com/question/10", "101", 200)[1] == (
        ADMIT_REDIRECT_UNEXPECTED
    )


def test_admit_landing_shape_masks_ids_and_drops_query() -> None:
    assert admit_landing_shape("https://www.zhihu.com/question/123/answer/456?utm=1#x") == (
        "https://www.zhihu.com/question/{id}/answer/{id}"
    )
    assert admit_landing_shape("https://www.zhihu.com/answer/456") == "https://www.zhihu.com/answer/{id}"
    assert admit_landing_shape("") == "://"


def _arm(crawler, rule: dict) -> None:
    crawler.browser_context.redirects[ANSWER] = rule


def _opened(crawler):
    return next(page for page in crawler.browser_context.pages if page.requested_urls)


@pytest.mark.asyncio
async def test_bare_answer_redirect_uses_final_question_id_and_existing_visits(monkeypatch, tmp_path):
    crawler, page, root = prepare(monkeypatch, tmp_path)
    ledger_path = root / "behavior.content_visits.jsonl"
    ledger_path.unlink()
    crawler._page_capture = ZhihuPageCapture.open(crawler.browser_context, mode="repair")
    crawler._page_capture.install(page)
    page.author_state = _author_state()
    _arm(crawler, {
        "url": FINAL,
        "status": 200,
        "state": _state("101", content="<p>青岛纯文字正文</p>"),
        "author_links": [AUTHOR],
    })
    # 页面状态里的问题 ID 与最终地址不同。入库问题 ID 必须取最终地址。
    state = _state("101", content="<p>青岛纯文字正文</p>")
    state = state.replace('"id": "10"', '"id": "999"', 1)
    crawler.browser_context.redirects[ANSWER]["state"] = state

    result = await crawler.get_note_detail(ANSWER, asyncio.Semaphore(1))

    opened = _opened(crawler)
    assert opened.requested_urls == [ANSWER]
    assert result is not None
    assert result.content_id == "101"
    assert result.question_id == "10"
    assert result.content_url == FINAL
    assert result.content_detail_status == "detail_observed"
    assert result.content_detail_source == "answer_detail"
    assert result.content_text == "青岛纯文字正文"
    assert crawler._repair_outcomes == []
    refs = result.content_visit_refs
    assert refs["note_detail"] and refs["creator_profile"] and refs["images"] == []
    actions = [line["action"] for line in ledger_lines(root) if line["kind"] == "action"]
    assert actions == ["navigate", "click_author", "close_tab", "close_tab"]
    record = result.model_dump()
    validation = validate_formal_record(
        "zhihu",
        record,
        set(),
        content_image_candidates=lambda _platform, _item: [],
    )
    assert validation["valid"] is True, validation
    visit = validate_content_visits(
        [record],
        [root / "behavior.content_visits.jsonl"],
        platform="zhihu",
        mode="repair",
        manifests=[],
    )
    assert visit["problem_counts"] == {}
    assert visit["content_visit_ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rule", "code"),
    [
        ({"url": "https://www.zhihu.com/", "status": 200}, ADMIT_REDIRECT_HOME),
        ({"url": "https://zhuanlan.zhihu.com/p/101", "status": 200}, ADMIT_REDIRECT_ARTICLE),
        ({"url": "https://www.zhihu.com/404", "status": 404}, ADMIT_NOT_FOUND),
        ({"url": "https://www.zhihu.com/question/10/answer/999", "status": 200}, ADMIT_ID_MISMATCH),
    ],
)
async def test_bad_landing_is_unavailable_and_does_not_open_another_url(
    monkeypatch, tmp_path, rule, code,
):
    crawler, _page, root = prepare(monkeypatch, tmp_path)
    _arm(crawler, rule)

    result = await crawler.get_note_detail(ANSWER, asyncio.Semaphore(1))

    opened = _opened(crawler)
    assert result is None
    assert opened.requested_urls == [ANSWER]
    assert crawler._repair_outcomes[-1]["error_code"] == code
    assert crawler._repair_outcomes[-1]["retryable"] is False
    actions = [line["action"] for line in ledger_lines(root) if line["kind"] == "action"]
    assert actions[0] == "navigate"
    assert "click_author" not in actions


@pytest.mark.asyncio
async def test_unexpected_landing_is_a_plain_failure_not_unavailable(monkeypatch, tmp_path, caplog):
    caplog.set_level("WARNING", logger="TripPostCollect")
    crawler, _page, root = prepare(monkeypatch, tmp_path)
    _arm(crawler, {"url": "https://www.zhihu.com/question/10", "status": 200})

    result = await crawler.get_note_detail(ANSWER, asyncio.Semaphore(1))

    opened = _opened(crawler)
    assert result is not None
    assert result.content_detail_status == "request_failed"
    assert not result.content_text
    assert ADMIT_REDIRECT_UNEXPECTED not in ADMIT_UNAVAILABLE_CODES
    assert opened.requested_urls == [ANSWER]
    assert crawler._repair_outcomes == []
    assert "shape=https://www.zhihu.com/question/{id} status=200" in caplog.text
    assert "/question/10" not in caplog.text
    actions = [line["action"] for line in ledger_lines(root) if line["kind"] == "action"]
    assert actions.count("navigate") == 1
    assert "click_author" not in actions


@pytest.mark.asyncio
async def test_canonical_repair_url_keeps_entity_question_id(monkeypatch, tmp_path):
    crawler, _page, _root = prepare(monkeypatch, tmp_path)
    canonical = "https://www.zhihu.com/question/10/answer/101"
    crawler.browser_context.redirects[canonical] = {
        "url": canonical,
        "status": 200,
        "state": _state("101").replace('"id": "10"', '"id": "999"', 1),
        "author_links": [AUTHOR],
    }

    result = await crawler.get_note_detail(canonical, asyncio.Semaphore(1))

    assert result is not None
    assert result.question_id == "999"
    assert crawler._repair_outcomes == []
