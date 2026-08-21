from __future__ import annotations

import json
import sqlite3
import sys
import types
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

repair = import_module("repair_xhs_posts")
mediacrawler = import_module("mediacrawler_crawl")
entrypoint = import_module("mediacrawler_export_entrypoint")


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


def test_load_xhs_repair_fallbacks_includes_existing_metrics(tmp_path: Path) -> None:
    db_path = tmp_path / "repair.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE web_posts(
            platform_key TEXT,
            platform_post_id TEXT,
            keyword TEXT,
            raw_sample_json TEXT,
            post_likes_count INTEGER,
            post_favorites_count INTEGER,
            post_comments_count INTEGER,
            post_shares_count INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO web_posts VALUES(
            'xhs', 'note-1', '青岛旅游', '{}', 11, 12, 0, 13
        )
        """
    )
    conn.commit()
    conn.close()

    fallbacks = mediacrawler.load_post_repair_fallbacks(
        db_path,
        "xhs",
        {"note-1"},
    )

    assert fallbacks["note-1"] == {
        "keyword": "青岛旅游",
        "liked_count": 11,
        "collected_count": 12,
        "comment_count": 0,
        "share_count": 13,
    }


def test_xhs_repair_preserves_existing_metric_without_overwriting_fresh_zero() -> None:
    detail = {
        "note_id": "note-1",
        "desc": "完整正文",
        "content_detail_status": "detail_observed",
        "content_detail_source": "note_detail",
        "time": 1_786_000_000,
        "image_list": [{"url_default": "https://example.test/body.jpg"}],
        "user_id": "author-1",
        "nickname": "作者",
        "author_followers_count": 10,
        "followers_observed": True,
        "author_followers_source": "creator_profile",
        "liked_count": 0,
        "collected_count": 2,
        "comment_count": "",
        "share_count": 3,
    }

    merged = mediacrawler.merge_repair_fallback_metadata(
        "xhs",
        detail,
        {"liked_count": 99, "comment_count": 1},
    )

    validation = mediacrawler.validate_formal_record("xhs", merged, set())
    assert validation["valid"] is True
    assert merged["liked_count"] == 0
    assert merged["comment_count"] == 1
    assert merged["repair_fallback_evidence"] == {
        "metrics": {
            "comment_count": {
                "source": "existing_web_posts_metric",
                "value": 1,
            }
        }
    }


def test_xhs_invalid_candidate_does_not_block_valid_repair_subset(tmp_path: Path) -> None:
    contents_path = tmp_path / "xhs" / "detail_contents_test.jsonl"
    contents_path.parent.mkdir(parents=True)
    base = {
        "content_detail_status": "detail_observed",
        "content_detail_source": "note_detail",
        "time": 1_786_000_000,
        "image_list": [{"url_default": "https://example.test/body.jpg"}],
        "user_id": "author-1",
        "nickname": "作者",
        "author_followers_count": 10,
        "followers_observed": True,
        "author_followers_source": "creator_profile",
        "liked_count": 1,
        "collected_count": 2,
        "comment_count": "",
        "share_count": 3,
    }
    contents_path.write_text(
        "\n".join(
            json.dumps(value, ensure_ascii=False)
            for value in (
                {**base, "note_id": "missing-body", "desc": ""},
                {**base, "note_id": "valid-note", "desc": "完整正文"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    summary = {
        "records": [
            {
                "platform": "xhs",
                "output": {
                    "jsonl_files": [str(contents_path)],
                    "image_manifest_paths": [],
                },
            }
        ]
    }

    validation, records = mediacrawler.collect_formal_records(
        summary,
        candidate_hard_limit=2,
        target_new_posts=0,
        db_path=None,
        allowed_identities={"xhs:id:missing-body", "xhs:id:valid-note"},
        repair_metadata_by_identity={
            "xhs:id:missing-body": {"comment_count": 1},
            "xhs:id:valid-note": {"comment_count": 1},
        },
        repair_mode=True,
    )

    assert validation["valid_total_count"] == 1
    assert validation["invalid_reason_counts"] == {"missing_content": 1}
    assert [item["identity"] for item in records] == ["xhs:id:valid-note"]


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


def test_partial_xhs_repair_can_commit_valid_records() -> None:
    assert mediacrawler.repair_partial_child_execution_allowed(
        repair_mode=True,
        child_execution_ok=False,
        validation={"valid_total_count": 2},
        image_materialization={"complete": True},
        behavior_validation={"ok": True},
    ) is True

    assert mediacrawler.repair_partial_child_execution_allowed(
        repair_mode=True,
        child_execution_ok=False,
        validation={"valid_total_count": 0},
        image_materialization={"complete": True},
        behavior_validation={"ok": True},
    ) is False


def test_xhs_repair_pagination_exposes_candidate_failures() -> None:
    evidence = mediacrawler.xhs_repair_pagination_evidence(
        [
            {
                "platform": "xhs",
                "repair_report": {
                    "batches": [{"batch": 1, "target_count": 2}],
                    "successful_ids": ["note-2"],
                    "candidate_failures": [
                        {
                            "platform_post_id": "note-1",
                            "failure_scope": "detail",
                            "error_code": "api_and_html_empty",
                            "attempts": 3,
                            "retryable": True,
                        }
                    ],
                },
            }
        ],
        target_count=2,
    )

    assert evidence["candidate_count"] == 2
    assert evidence["successful_candidate_count"] == 1
    assert evidence["skipped_candidate_count"] == 1
    assert evidence["skipped_candidate_failures"][0]["attempts"] == 3


def test_xhs_repair_report_preserves_run_level_blocker() -> None:
    reason = mediacrawler.repair_runtime_stop_reason(
        [
            {
                "platform": "xhs",
                "failure_classification": {"failure_type": "runtime_failed"},
                "repair_report": {
                    "runtime_blocker": {"error_code": "login_required"}
                },
            }
        ],
        ["xhs"],
    )

    assert reason == "login_required"


@pytest.mark.asyncio
async def test_xhs_repair_continues_same_and_later_batches_after_candidate_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: list[str] = []
    requested: list[str] = []

    class FakeCrawler:
        async def get_note_detail_async_task(self, *, note_id, **_kwargs):
            requested.append(note_id)
            if note_id == "note-1":
                error = RuntimeError("detail exhausted")
                error.code = "api_and_html_empty"
                error.attempts = 3
                raise error
            return {
                "note_id": note_id,
                "xsec_token": f"token-{note_id}",
                "type": "normal",
            }

        async def enrich_note_creator(self, _note_detail):
            return None

        async def get_notice_media(self, _note_detail):
            return None

        async def batch_get_note_comments(self, _note_ids, _tokens):
            return None

        @staticmethod
        def is_video_note(_note_detail):
            return False

    async def update_xhs_note(note_detail):
        stored.append(note_detail["note_id"])

    fake_core = types.ModuleType("media_platform.xhs.core")
    fake_core.XiaoHongShuCrawler = FakeCrawler
    fake_core.config = SimpleNamespace(
        MAX_CONCURRENCY_NUM=1,
        XHS_SPECIFIED_NOTE_URL_LIST=["note-1", "note-2", "note-3"],
    )
    fake_core.parse_note_info_from_note_url = lambda value: SimpleNamespace(
        note_id=value,
        xsec_source="pc_search",
        xsec_token=f"token-{value}",
    )
    fake_core.utils = SimpleNamespace(
        logger=SimpleNamespace(info=lambda *_args: None, warning=lambda *_args: None)
    )
    fake_core.xhs_store = SimpleNamespace(update_xhs_note=update_xhs_note)
    fake_xhs = types.ModuleType("media_platform.xhs")
    fake_xhs.core = fake_core
    fake_platform = types.ModuleType("media_platform")
    fake_platform.xhs = fake_xhs
    monkeypatch.setitem(sys.modules, "media_platform", fake_platform)
    monkeypatch.setitem(sys.modules, "media_platform.xhs", fake_xhs)
    monkeypatch.setitem(sys.modules, "media_platform.xhs.core", fake_core)
    report_path = tmp_path / "repair_report.json"
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_REPAIR", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_REPAIR_BATCH_SIZE", "2")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_REPAIR_REPORT_PATH", str(report_path))

    entrypoint.install_xhs_repair_resilience()
    await FakeCrawler().get_specified_notes()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert requested == ["note-1", "note-2", "note-3"]
    assert stored == ["note-2", "note-3"]
    assert [batch["batch"] for batch in report["batches"]] == [1, 2]
    assert report["successful_ids"] == ["note-2", "note-3"]
    assert report["candidate_failures"] == [
        {
            "platform": "xhs",
            "identity": "xhs:id:note-1",
            "platform_post_id": "note-1",
            "batch": 1,
            "failure_scope": "detail",
            "detail": "RuntimeError",
            "error_type": "RuntimeError",
            "error_code": "api_and_html_empty",
            "attempts": 3,
            "retryable": True,
        }
    ]
