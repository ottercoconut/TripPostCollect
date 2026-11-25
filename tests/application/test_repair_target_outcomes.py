"""微博修复细记录只填充差额里的失败项，不改变成功集合。其他平台不读这些记录。"""

from __future__ import annotations

import json

from trippostcollect.application.candidates import AdaptiveAccumulator
from trippostcollect.application.repair import (
    load_weibo_repair_outcomes,
    post_repair_pagination_evidence,
    weibo_posted_repair_outcomes,
)


def _targets(*ids: str) -> list[dict]:
    return [{"platform_post_id": post_id} for post_id in ids]


def _ids(evidence: dict) -> set[str]:
    return {item["platform_post_id"] for item in evidence["skipped_candidate_failures"]}


def test_fine_records_do_not_change_success_or_failure_sets() -> None:
    targets = _targets("ok", "detail", "video", "missing")
    outcomes = [
        {
            "platform_post_id": "detail",
            "error_code": "detail_show_missing",
            "evidence_source": "weibo_detail",
            "attempts": 3,
            "retryable": False,
            "evidence": {
                "navigation_status": 404,
                "final_page_kind": "https://weibo.com/88/detail",
                "show_responses_unmatched": [
                    {"status": 404, "ok": 0, "id_matched": False, "body_error": "JSONDecodeError"},
                ],
                "show_request_failures": 0,
                "waited_seconds": 15,
                "request_url": "https://weibo.com/cookie",
            },
        },
        {
            "platform_post_id": "video",
            "error_code": "repair_target_video",
            "evidence_source": "weibo_video",
            "attempts": 1,
            "retryable": False,
        },
    ]
    without = post_repair_pagination_evidence(
        targets, platform="weibo", successful_identities={"weibo:id:ok"},
    )
    filled = post_repair_pagination_evidence(
        targets, platform="weibo", successful_identities={"weibo:id:ok"},
        target_outcomes=outcomes,
    )
    assert without["successful_candidate_count"] == filled["successful_candidate_count"] == 1
    assert without["skipped_candidate_count"] == filled["skipped_candidate_count"] == 3
    assert _ids(without) == _ids(filled) == {"detail", "video", "missing"}
    by_id = {item["platform_post_id"]: item for item in filled["skipped_candidate_failures"]}
    assert by_id["missing"]["error_code"] == "repair_target_no_valid_output"
    assert by_id["missing"]["evidence_source"] == "repair_target_output_difference"
    assert "evidence" not in by_id["missing"]
    assert by_id["detail"]["error_code"] == "detail_show_missing"
    assert by_id["detail"]["evidence_source"] == "weibo_detail"
    assert by_id["detail"]["attempts"] == 3
    assert by_id["detail"]["retryable"] is False
    assert by_id["detail"]["evidence"]["final_page_kind"] is None
    assert by_id["video"]["error_code"] == "repair_target_video"
    assert by_id["video"]["failure_scope"] == "post"
    blob = json.dumps(filled, ensure_ascii=False)
    assert "http" not in blob
    assert "weibo.com" not in blob
    assert "cookie" not in blob


def test_materialization_failure_stays_ahead_of_a_fine_record() -> None:
    evidence = post_repair_pagination_evidence(
        _targets("bad-image"),
        platform="weibo",
        successful_identities=set(),
        materialization_failures=[{
            "identity": "weibo:id:bad-image",
            "code": "image_too_large",
            "message": "oversized image",
            "attempts": 1,
        }],
        target_outcomes=[{
            "platform_post_id": "bad-image",
            "error_code": "detail_show_missing",
            "evidence_source": "weibo_detail",
            "attempts": 3,
            "retryable": True,
        }],
    )
    failure = evidence["skipped_candidate_failures"][0]
    assert failure["error_code"] == "image_too_large"
    assert failure["evidence_source"] == "image_materialization"
    assert failure["failure_scope"] == "image"
    assert "evidence" not in failure


def test_other_platforms_ignore_weibo_outcome_records() -> None:
    records = [{
        "platform": "zhihu",
        "repair_outcomes": [{
            "platform_post_id": "a",
            "error_code": "detail_show_missing",
            "evidence_source": "weibo_detail",
            "attempts": 1,
            "retryable": False,
        }],
    }]
    assert weibo_posted_repair_outcomes(records, "zhihu") is None
    assert weibo_posted_repair_outcomes(records, "bilibili") is None
    evidence = post_repair_pagination_evidence(
        _targets("a", "b"),
        platform="zhihu",
        successful_identities={"zhihu:id:a"},
    )
    failure = evidence["skipped_candidate_failures"][0]
    assert failure["platform_post_id"] == "b"
    assert failure["error_code"] == "repair_target_no_valid_output"
    assert failure["evidence_source"] == "repair_target_output_difference"
    assert "evidence" not in failure


def test_missing_outcome_file_is_empty(tmp_path) -> None:
    assert load_weibo_repair_outcomes(tmp_path / "missing.json") == []
    path = tmp_path / "broken.json"
    path.write_text("{", encoding="utf-8")
    assert load_weibo_repair_outcomes(path) == []


def test_skip_reason_counts_stay_off_other_platform_summaries() -> None:
    zhihu = AdaptiveAccumulator.for_platform("zhihu", existing_identities=set())
    assert "skipped_reason_counts" not in zhihu.summary()
    weibo = AdaptiveAccumulator.for_platform("weibo", existing_identities=set())
    weibo.note_skip_reason("video")
    weibo.note_skip_reason("video")
    assert weibo.summary()["skipped_reason_counts"] == {"video": 2}
    assert weibo.skipped_candidate_failures == []
