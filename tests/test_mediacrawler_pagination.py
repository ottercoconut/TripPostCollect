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
                    "valid_unique_count": 11,
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
        target_valid_posts=50,
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
                    "valid_unique_count": 0,
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
        target_valid_posts=50,
        pagination_evidence=evidence,
    )

    assert evidence["stopped"] is True
    assert validation["candidate_count"] == 84
    assert validation["stop_reason"] == "source_exhausted"
    assert validation["stop_detail"] == "empty_page"
