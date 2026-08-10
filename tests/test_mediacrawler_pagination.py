from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

mediacrawler_crawl = import_module("mediacrawler_crawl")


def write_state(path: Path, events: list[dict]) -> None:
    path.write_text(json.dumps({"events": events}), encoding="utf-8")


def bilibili_record(post_id: str) -> dict:
    return {
        "content_id": post_id,
        "content_type": "article",
        "title": f"title-{post_id}",
        "desc": "body",
        "content_text": "detail body",
        "content_detail_status": "detail_observed",
        "content_detail_source": "article_view_api",
        "content_images_detail_status": "detail_observed",
        "pub_time": "2026-07-13 12:00:00",
        "user_id": "author-1",
        "nickname": "author",
        "image_urls": [f"https://example.test/{post_id}-1.jpg"],
        "followers_count": 100,
        "followers_observed": True,
        "author_followers_source": "relation_stat",
        "liked_count": 1,
        "comment_count": 2,
        "view_count": 3,
    }


def test_unfinished_pagination_is_not_reported_as_source_exhausted(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    write_state(
        state_path,
        [
            {
                "type": "adaptive_batch_completed",
                "details": {
                    "platform": "xhs",
                    "batch_no": 1,
                    "candidate_count": 20,
                    "valid_new_count": 11,
                    "source_page": 1,
                    "source_has_more": True,
                    "raw_batch_count": 20,
                    "stop_reason": "continue",
                },
            }
        ],
    )

    evidence = mediacrawler_crawl.load_pagination_evidence(state_path)
    validation, _ = mediacrawler_crawl.collect_formal_records(
        {"records": []},
        candidate_hard_limit=300,
        target_new_posts=50,
        db_path=tmp_path / "missing.sqlite",
        pagination_evidence=evidence,
    )

    assert evidence["batch_count"] == 1
    assert evidence["stopped"] is False
    assert validation["candidate_count"] == 20
    assert validation["stop_reason"] == "runtime_failed"


def test_deferred_image_candidate_blocks_source_exhaustion_completion(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    write_state(
        state_path,
        [
            {
                "type": "adaptive_search_stopped",
                "details": {
                    "platform": "xhs",
                    "candidate_count": 1,
                    "valid_new_count": 0,
                    "stop_reason": "deferred_retry_pending",
                    "stop_detail": "image_candidate_failures",
                    "resume_page": 2,
                    "resume_cursor": "search-id",
                    "source_has_more": True,
                    "batch_complete": False,
                    "deferred_image_count": 1,
                    "deferred_image_failures": [
                        {
                            "identity": "note-1",
                            "error_code": "image_download_retryable",
                            "attempts": 3,
                        }
                    ],
                    "candidate_identities": [],
                },
            }
        ],
    )

    evidence = mediacrawler_crawl.load_pagination_evidence(state_path)
    validation, _ = mediacrawler_crawl.collect_formal_records(
        {"records": []},
        candidate_hard_limit=10,
        target_new_posts=0,
        db_path=tmp_path / "missing.sqlite",
        pagination_evidence=evidence,
        completion_mode="source-exhausted",
    )

    assert validation["source_exhausted_met"] is False
    assert validation["completion_met"] is False
    assert validation["stop_reason"] == "deferred_retry_pending"
    assert evidence["stop_event"]["deferred_image_count"] == 1


def test_inconsistent_exhaustion_event_is_rejected_when_deferred_count_remains(
    tmp_path: Path,
) -> None:
    pagination = {
        "stopped": True,
        "stop_reason": "source_exhausted",
        "stop_detail": "empty_page",
        "candidate_count": 1,
        "stop_event": {
            "platform": "bilibili",
            "stop_reason": "source_exhausted",
            "stop_detail": "empty_page",
            "source_page": 2,
            "raw_batch_count": 0,
            "deferred_image_count": 1,
            "deferred_image_failures": [
                {
                    "identity": "article-1",
                    "error_code": "image_download_retryable",
                    "attempts": 3,
                }
            ],
        },
    }

    validation, _ = mediacrawler_crawl.collect_formal_records(
        {"records": []},
        candidate_hard_limit=10,
        target_new_posts=0,
        db_path=tmp_path / "missing.sqlite",
        pagination_evidence=pagination,
        completion_mode="source-exhausted",
    )

    assert validation["deferred_image_count"] == 1
    assert validation["source_exhausted_met"] is False
    assert validation["completion_met"] is False
    assert validation["stop_reason"] == "deferred_retry_pending"


def test_deferred_image_failures_are_reflected_in_materialization_summary() -> None:
    materialization = {
        "retryable_failures": 0,
        "terminal_failures": 0,
        "complete": True,
    }
    pagination = {
        "stop_event": {
            "deferred_image_count": 2,
            "deferred_image_failures": [
                {
                    "identity": "retryable",
                    "error_code": "image_download_retryable",
                    "retryable": True,
                    "attempts": 3,
                },
                {
                    "identity": "terminal",
                    "error_code": "image_source_unavailable",
                    "retryable": False,
                    "attempts": 1,
                },
            ],
        }
    }

    summary = mediacrawler_crawl.attach_deferred_image_evidence(
        materialization,
        pagination,
    )

    assert summary["complete"] is True
    assert summary["deferred_image_count"] == 2
    assert summary["retryable_failures"] == 1
    assert summary["terminal_failures"] == 1
    assert len(summary["deferred_image_failures"]) == 2


def test_incomplete_run_rewinds_checkpoint_to_earliest_deferred_image(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    write_state(
        state_path,
        [
            {
                "type": "candidate_deferred",
                "details": {
                    "platform": "weibo",
                    "identity": "failed-post",
                    "detail": "image_download_failed",
                    "error_code": "image_source_unavailable",
                    "retryable": False,
                    "attempts": 1,
                    "source_index": 1,
                    "source_page": 10,
                    "discovery_phase": "frontier",
                },
            },
            {
                "type": "adaptive_batch_completed",
                "details": {
                    "platform": "weibo",
                    "candidate_count": 10,
                    "valid_new_count": 5,
                    "source_page": 10,
                    "resume_page": 10,
                    "batch_complete": False,
                    "stop_reason": "continue",
                    "candidate_identities": ["success-10"],
                },
            },
            {
                "type": "adaptive_batch_completed",
                "details": {
                    "platform": "weibo",
                    "candidate_count": 20,
                    "valid_new_count": 9,
                    "source_page": 11,
                    "resume_page": 12,
                    "batch_complete": True,
                    "stop_reason": "continue",
                    "candidate_identities": ["success-10", "success-11"],
                },
            },
        ],
    )

    evidence = mediacrawler_crawl.load_pagination_evidence(state_path)
    checkpoint_event = mediacrawler_crawl.effective_discovery_checkpoint_event(
        evidence
    )
    validation, _ = mediacrawler_crawl.collect_formal_records(
        {"records": []},
        candidate_hard_limit=300,
        target_new_posts=0,
        db_path=tmp_path / "missing.sqlite",
        pagination_evidence=evidence,
        completion_mode="source-exhausted",
    )

    assert evidence["stopped"] is False
    assert evidence["deferred_image_count"] == 1
    assert evidence["deferred_image_failures"][0]["identity"] == "failed-post"
    assert checkpoint_event is not None
    assert checkpoint_event["resume_page"] == 10
    assert checkpoint_event["batch_complete"] is False
    assert checkpoint_event["source_has_more"] is True
    assert checkpoint_event["stop_reason"] == "runtime_failed"
    assert checkpoint_event["stop_detail"] == (
        "deferred_image_pending_after_incomplete_run"
    )
    assert validation["pagination_incomplete"] is True
    assert validation["deferred_image_count"] == 1
    assert validation["stop_reason"] == "runtime_failed"
    assert validation["completion_met"] is False


def test_source_exhaustion_without_stop_event_never_falls_back_to_target_met(
    tmp_path: Path,
) -> None:
    jsonl_path = tmp_path / "bili" / "jsonl" / "search_contents_2026-07-13.jsonl"
    jsonl_path.parent.mkdir(parents=True)
    jsonl_path.write_text(
        json.dumps(bilibili_record("new")) + "\n",
        encoding="utf-8",
    )
    pagination = {
        "available": True,
        "stopped": False,
        "candidate_count": 267,
        "batches": [
            {
                "candidate_count": 267,
                "valid_new_count": 208,
                "stop_reason": "continue",
                "source_page": 17,
                "source_has_more": True,
            }
        ],
    }

    validation, _ = mediacrawler_crawl.collect_formal_records(
        {"records": [{"output": {"jsonl_files": [str(jsonl_path)]}}]},
        candidate_hard_limit=100,
        target_new_posts=1,
        db_path=tmp_path / "missing.sqlite",
        pagination_evidence=pagination,
        completion_mode="source-exhausted",
    )

    assert validation["new_target_met"] is True
    assert validation["source_exhausted_met"] is False
    assert validation["completion_met"] is False
    assert validation["stop_reason"] == "runtime_failed"


def test_runtime_failed_pagination_overrides_reached_target(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "bili" / "jsonl" / "search_contents_test.jsonl"
    jsonl_path.parent.mkdir(parents=True)
    jsonl_path.write_text(
        json.dumps(bilibili_record("new")) + "\n",
        encoding="utf-8",
    )
    pagination = {
        "available": True,
        "stopped": True,
        "stop_reason": "runtime_failed",
        "candidate_count": 1,
        "stop_event": {
            "platform": "bilibili",
            "stop_reason": "runtime_failed",
            "batch_complete": False,
            "candidate_count": 1,
            "valid_new_count": 1,
        },
    }

    validation, selected = mediacrawler_crawl.collect_formal_records(
        {"records": [{"output": {"jsonl_files": [str(jsonl_path)]}}]},
        candidate_hard_limit=10,
        target_new_posts=1,
        db_path=tmp_path / "missing.sqlite",
        pagination_evidence=pagination,
        completion_mode="target-new-posts",
    )

    assert len(selected) == 1
    assert validation["valid_new_count"] == 1
    assert validation["pagination_runtime_blocked"] is True
    assert validation["completion_met"] is False
    assert validation["stop_reason"] == "runtime_failed"


def test_explicit_empty_page_proves_source_exhaustion(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    write_state(
        state_path,
        [
            {
                "type": "adaptive_search_stopped",
                "details": {
                    "platform": "douyin",
                    "candidate_count": 84,
                    "valid_new_count": 0,
                    "pages_fetched": 6,
                    "source_page": 7,
                    "source_has_more": False,
                    "raw_batch_count": 0,
                    "stop_reason": "source_exhausted",
                    "stop_detail": "empty_page",
                },
            }
        ],
    )

    evidence = mediacrawler_crawl.load_pagination_evidence(state_path)
    validation, _ = mediacrawler_crawl.collect_formal_records(
        {"records": []},
        candidate_hard_limit=1000,
        target_new_posts=50,
        db_path=tmp_path / "missing.sqlite",
        pagination_evidence=evidence,
    )

    assert evidence["stopped"] is True
    assert validation["candidate_count"] == 84
    assert validation["stop_reason"] == "source_exhausted"
    assert validation["stop_detail"] == "empty_page"


def test_unverified_douyin_first_page_empty_is_runtime_failure(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    write_state(
        state_path,
        [
            {
                "type": "adaptive_search_stopped",
                "details": {
                    "platform": "douyin",
                    "candidate_count": 0,
                    "valid_new_count": 0,
                    "pages_fetched": 1,
                    "source_page": 1,
                    "source_offset": 0,
                    "source_cursor": "",
                    "source_has_more": False,
                    "raw_batch_count": 0,
                    "stop_reason": "source_exhausted",
                    "stop_detail": "empty_page",
                },
            }
        ],
    )

    evidence = mediacrawler_crawl.load_pagination_evidence(state_path)
    validation, _ = mediacrawler_crawl.collect_formal_records(
        {"records": []},
        candidate_hard_limit=1000,
        target_new_posts=50,
        db_path=tmp_path / "missing.sqlite",
        pagination_evidence=evidence,
        completion_mode="source-exhausted",
    )

    assert validation["source_exhausted_met"] is False
    assert validation["completion_met"] is False
    assert validation["stop_reason"] == "runtime_failed"
    assert validation["stop_detail"] == "unverified_empty_first_page"


def test_verified_douyin_first_page_empty_can_prove_exhaustion(tmp_path: Path) -> None:
    pagination = {
        "stopped": True,
        "stop_reason": "source_exhausted",
        "stop_detail": "verified_empty_first_page",
        "candidate_count": 0,
        "stop_event": {
            "platform": "douyin",
            "source_page": 1,
            "source_offset": 0,
            "source_cursor": "",
            "raw_batch_count": 0,
            "stop_reason": "source_exhausted",
            "stop_detail": "verified_empty_first_page",
        },
    }

    validation, _ = mediacrawler_crawl.collect_formal_records(
        {"records": []},
        candidate_hard_limit=1000,
        target_new_posts=50,
        db_path=tmp_path / "missing.sqlite",
        pagination_evidence=pagination,
        completion_mode="source-exhausted",
    )

    assert validation["source_exhausted_met"] is True
    assert validation["completion_met"] is True
    assert validation["stop_reason"] == "source_exhausted"
    assert validation["stop_detail"] == "verified_empty_first_page"


def test_source_exhausted_completion_ignores_configured_quantity_limits(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "bili" / "jsonl" / "search_contents_2026-07-13.jsonl"
    jsonl_path.parent.mkdir(parents=True)
    jsonl_path.write_text(
        "\n".join(
            json.dumps(record)
            for record in (bilibili_record("first"), bilibili_record("second"))
        )
        + "\n",
        encoding="utf-8",
    )
    pagination = {
        "stopped": True,
        "stop_reason": "source_exhausted",
        "stop_detail": "empty_page",
        "candidate_count": 2,
    }

    validation, selected = mediacrawler_crawl.collect_formal_records(
        {"records": [{"output": {"jsonl_files": [str(jsonl_path)]}}]},
        candidate_hard_limit=1,
        target_new_posts=1,
        db_path=tmp_path / "missing.sqlite",
        pagination_evidence=pagination,
        completion_mode="source-exhausted",
    )

    assert len(selected) == 2
    assert validation["candidate_count"] == 2
    assert validation["new_target_met"] is True
    assert validation["source_exhausted_met"] is True
    assert validation["completion_met"] is True
    assert validation["quantity_limits_enforced"] is False


def test_pagination_evidence_keeps_douyin_frontier_reseed_event(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    write_state(
        state_path,
        [
            {
                "type": "discovery_frontier_reseeded",
                "details": {
                    "platform": "douyin",
                    "reason": "new_candidates_after_saved_exhaustion",
                    "saved_resume_page": 22,
                    "saved_resume_offset": 315,
                    "saved_resume_cursor": "old-search-id",
                    "resume_page": 4,
                    "resume_offset": 45,
                    "resume_cursor": "new-search-id",
                    "refresh_new_candidate_count": 34,
                },
            }
        ],
    )

    evidence = mediacrawler_crawl.load_pagination_evidence(state_path)

    assert evidence["frontier_reseeds"] == [
        {
            "platform": "douyin",
            "reason": "new_candidates_after_saved_exhaustion",
            "saved_resume_page": 22,
            "saved_resume_offset": 315,
            "saved_resume_cursor": "old-search-id",
            "resume_page": 4,
            "resume_offset": 45,
            "resume_cursor": "new-search-id",
            "refresh_new_candidate_count": 34,
        }
    ]


def test_existing_valid_record_is_update_not_valid_new_target(tmp_path: Path) -> None:
    db_path = tmp_path / "posts.sqlite"
    existing = bilibili_record("existing")
    new = bilibili_record("new")
    import_summary = {
        "captured_at": "2026-07-13T12:00:00+08:00",
        "keyword": "青岛旅游",
        "batch_dir": str(tmp_path),
    }
    first_import = mediacrawler_crawl.import_valid_records(
        import_summary,
        [{"platform": "bilibili", "record": existing}],
        db_path,
    )
    assert first_import["inserted_rows"] == 1

    jsonl_path = tmp_path / "bili" / "jsonl" / "search_contents_2026-07-13.jsonl"
    jsonl_path.parent.mkdir(parents=True)
    jsonl_path.write_text(
        "\n".join(json.dumps(item) for item in (existing, new)) + "\n",
        encoding="utf-8",
    )
    summary = {"records": [{"output": {"jsonl_files": [str(jsonl_path)]}}]}

    validation, selected = mediacrawler_crawl.collect_formal_records(
        summary,
        candidate_hard_limit=10,
        target_new_posts=1,
        db_path=db_path,
    )
    imported = mediacrawler_crawl.import_valid_records(import_summary, selected, db_path)

    assert validation["valid_new_count"] == 1
    assert validation["valid_existing_count"] == 1
    assert validation["valid_total_count"] == 2
    assert validation["new_target_met"] is True
    assert imported["inserted_rows"] == 1
    assert imported["updated_rows"] == 1


def test_zhihu_missing_image_distinguishes_unobserved_detail() -> None:
    base = {
        "content_id": "answer-1",
        "content_type": "answer",
        "content_text": "正文",
        "content_url": "https://www.zhihu.com/question/1/answer/answer-1",
        "created_time": 1_700_000_000,
        "creator_hash": "author-1",
        "user_nickname": "author",
        "followers_count": 10,
        "followers_observed": True,
        "author_followers_source": "search_author",
        "voteup_count": 1,
        "comment_count": 2,
        "image_list": [],
    }

    request_failed = mediacrawler_crawl.validate_formal_record(
        "zhihu",
        {**base, "content_detail_status": "request_failed"},
        set(),
    )
    observed_without_image = mediacrawler_crawl.validate_formal_record(
        "zhihu",
        {**base, "content_detail_status": "detail_observed"},
        set(),
    )

    assert "content_detail_unobserved" in request_failed["reasons"]
    assert "missing_content_image" not in request_failed["reasons"]
    assert "missing_content_image" in observed_without_image["reasons"]
    assert "content_detail_unobserved" not in observed_without_image["reasons"]


def test_load_zhihu_detail_urls_accepts_only_answer_and_article(tmp_path: Path) -> None:
    path = tmp_path / "zhihu-urls.json"
    path.write_text(
        json.dumps(
            [
                "https://www.zhihu.com/question/123/answer/456?utm_source=test",
                "https://zhuanlan.zhihu.com/p/789",
                "https://zhuanlan.zhihu.com/p/789",
            ]
        ),
        encoding="utf-8",
    )

    assert mediacrawler_crawl.load_zhihu_detail_urls(path) == [
        "https://www.zhihu.com/question/123/answer/456",
        "https://zhuanlan.zhihu.com/p/789",
    ]

    path.write_text(
        json.dumps(["https://www.zhihu.com/zvideo/123"]),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="unsupported Zhihu detail URL"):
        mediacrawler_crawl.load_zhihu_detail_urls(path)


def test_campaign_records_may_exceed_each_run_candidate_budget(tmp_path: Path) -> None:
    first_path = tmp_path / "first" / "bili" / "jsonl" / "search_contents_1.jsonl"
    second_path = tmp_path / "second" / "bili" / "jsonl" / "search_contents_2.jsonl"
    first_path.parent.mkdir(parents=True)
    second_path.parent.mkdir(parents=True)
    first_path.write_text(
        "\n".join(json.dumps(bilibili_record(f"old-{index}")) for index in range(3)) + "\n",
        encoding="utf-8",
    )
    second_path.write_text(
        "\n".join(json.dumps(bilibili_record(f"new-{index}")) for index in range(3)) + "\n",
        encoding="utf-8",
    )

    validation, selected = mediacrawler_crawl.collect_formal_records(
        {
            "records": [
                {"output": {"jsonl_files": [str(first_path)]}},
                {"output": {"jsonl_files": [str(second_path)]}},
            ]
        },
        candidate_hard_limit=3,
        target_new_posts=6,
        db_path=tmp_path / "missing.sqlite",
        pagination_evidence={"candidate_count": 3, "stopped": True},
        enforce_candidate_limit=False,
    )

    assert validation["candidate_count"] == 6
    assert validation["run_candidate_count"] == 3
    assert validation["new_target_met"] is True
    assert len(selected) == 6


def test_local_image_completeness_is_part_of_formal_record_selection(
    tmp_path: Path,
) -> None:
    jsonl_path = tmp_path / "bili" / "jsonl" / "search_contents_1.jsonl"
    jsonl_path.parent.mkdir(parents=True)
    record = bilibili_record("requires-local-image")
    jsonl_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    summary = {
        "records": [
            {
                "output": {
                    "jsonl_files": [str(jsonl_path)],
                    "image_manifest_paths": [],
                }
            }
        ]
    }

    validation, selected = mediacrawler_crawl.collect_formal_records(
        summary,
        candidate_hard_limit=1,
        target_new_posts=1,
        db_path=tmp_path / "missing.sqlite",
        require_local_images=True,
        localized_identities=set(),
    )

    assert selected == []
    assert validation["local_images_required"] is True
    assert validation["local_images_complete"] is False
    assert validation["completion_met"] is False
    assert validation["invalid_reason_counts"] == {"local_images_incomplete": 1}
