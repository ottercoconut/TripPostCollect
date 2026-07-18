from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path


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


def test_existing_valid_record_is_update_not_valid_new_target(tmp_path: Path) -> None:
    db_path = tmp_path / "posts.sqlite"
    existing = bilibili_record("existing")
    new = bilibili_record("new")
    import_summary = {
        "captured_at": "2026-07-13T12:00:00+08:00",
        "keyword": "济南旅游",
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
