from __future__ import annotations

import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

repair = import_module("repair_xhs_posts")
mediacrawler = import_module("mediacrawler_crawl")


def test_detail_url_preserves_authoritative_xsec_query() -> None:
    row = {
        "platform_post_id": "note-1",
        "canonical_url": (
            "https://www.xiaohongshu.com/explore/note-1?"
            "xsec_token=token-1&xsec_source=pc_search"
        ),
        "raw_sample_json": json.dumps({"xsec_token": "token-1"}),
    }

    url, reason = repair._detail_url(row)

    assert reason == ""
    assert url is not None
    assert "xsec_token=token-1" in url
    assert "xsec_source=pc_search" in url


def test_detail_url_rejects_missing_source_instead_of_guessing() -> None:
    row = {
        "platform_post_id": "note-1",
        "canonical_url": "https://www.xiaohongshu.com/explore/note-1?xsec_token=token-1",
        "raw_sample_json": json.dumps({}),
    }

    assert repair._detail_url(row) == (None, "missing_xsec_token_or_source")


def test_load_xhs_detail_urls_requires_both_tokens(tmp_path: Path) -> None:
    path = tmp_path / "urls.json"
    path.write_text(
        json.dumps(
            [
                "https://www.xiaohongshu.com/explore/note-1?"
                "xsec_token=token-1&xsec_source=pc_search"
            ]
        ),
        encoding="utf-8",
    )

    assert mediacrawler.load_xhs_detail_urls(path)[0].endswith(
        "xsec_token=token-1&xsec_source=pc_search"
    )

    path.write_text(
        json.dumps(["https://www.xiaohongshu.com/explore/note-1?xsec_token=token-1"]),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="xsec_token and xsec_source"):
        mediacrawler.load_xhs_detail_urls(path)


def test_select_targets_excludes_observed_rows_and_limits_batch(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "repair.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE web_posts(
            id INTEGER PRIMARY KEY,
            platform_key TEXT,
            platform_post_id TEXT,
            canonical_url TEXT,
            keyword TEXT,
            raw_sample_json TEXT,
            artifact_dir TEXT
        )
        """
    )
    for index, status in enumerate(("", "detail_observed", ""), start=1):
        post_id = f"note-{index}"
        conn.execute(
            """
            INSERT INTO web_posts(
                id, platform_key, platform_post_id, canonical_url, keyword,
                raw_sample_json, artifact_dir
            ) VALUES (?, 'xhs', ?, ?, '青岛旅游', ?, ?)
            """,
            (
                index,
                post_id,
                f"https://www.xiaohongshu.com/explore/{post_id}?"
                f"xsec_token=token-{index}&xsec_source=pc_search",
                json.dumps({"content_detail_status": status, "xsec_token": f"token-{index}"}),
                f"artifact/{post_id}",
            ),
        )
    conn.commit()

    targets, rejected = repair.select_targets(conn, post_ids=[], max_items=1)

    assert len(targets) == 1
    assert targets[0]["platform_post_id"] == "note-1"
    assert rejected == []
    conn.close()


def test_repair_behavior_gate_does_not_require_search_pacing(monkeypatch) -> None:
    monkeypatch.setattr(mediacrawler, "behavior_evidence_valid", lambda value: True)
    result = mediacrawler.collect_behavior_validation(
        [
            {
                "platform": "xhs",
                "behavior_evidence": {
                    "status": "completed",
                    "profile": "xhs_guarded",
                    "url": "https://www.xiaohongshu.com/search_result?keyword=青岛旅游",
                    "events": [{}],
                    "request_pacing_events": [
                        {"stage": "note_detail"},
                        {"stage": "creator_profile"},
                    ],
                },
                "policy_events": [{"allowed": True, "disabled": True}],
            }
        ],
        ["xhs"],
        "青岛旅游",
        repair_mode=True,
    )

    assert result["ok"] is True
    assert result["platforms"]["xhs"]["request_pacing_ok"] is True
