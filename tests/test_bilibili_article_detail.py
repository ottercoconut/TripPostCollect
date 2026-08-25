from __future__ import annotations

import io
import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

mediacrawler_crawl = import_module("mediacrawler_crawl")


def search_item(post_id: str = "123") -> dict:
    return {
        "id": post_id,
        "title": "搜索标题",
        "desc": "搜索摘要只有这一小段",
        "arcurl": f"https://www.bilibili.com/read/cv{post_id}/",
        "image_urls": ["https://example.test/search-preview.jpg"],
        "pubdate": 1_700_000_000,
        "like": 1,
        "reply": 2,
        "view": 3,
        "author": "作者",
        "mid": "456",
    }


def hydrated_record(post_id: str = "123") -> dict:
    record = mediacrawler_crawl.normalize_bilibili_article_record(
        search_item(post_id),
        "青岛西海岸旅游攻略",
    )
    assert record is not None
    record = mediacrawler_crawl.hydrate_bilibili_article_record(
        record,
        {
            "title": "详情标题",
            "content": "第一段完整正文。\n第二段完整正文。",
            "image_urls": ["https://example.test/body.jpg"],
            "opus": {"content": {"paragraphs": []}},
        },
        attempts=1,
    )
    record.update(
        {
            "followers_observed": True,
            "author_followers_source": "relation_stat",
            "followers_count": 100,
            "author_followers_count": 100,
        }
    )
    return record


def test_search_excerpt_alone_is_not_a_formal_bilibili_record() -> None:
    record = mediacrawler_crawl.normalize_bilibili_article_record(
        search_item(),
        "青岛西海岸旅游攻略",
    )

    assert record is not None
    validation = mediacrawler_crawl.validate_formal_record("bilibili", record, set())

    assert record["content_text"] == ""
    assert validation["valid"] is False
    assert "missing_content" in validation["reasons"]
    assert "content_detail_unobserved" in validation["reasons"]
    assert "untrusted_content_detail_source" in validation["reasons"]
    assert "content_images_detail_unobserved" in validation["reasons"]


def test_current_opus_detail_uses_full_body_and_inline_images_not_cover() -> None:
    search_record = mediacrawler_crawl.normalize_bilibili_article_record(
        search_item(),
        "青岛西海岸旅游攻略",
    )
    assert search_record is not None
    body = "第一段完整正文。\n图片\n第二段正文明显长于搜索摘要。\n图片"
    detail = {
        "title": "详情标题",
        "content": body,
        "image_urls": ["https://example.test/detail-cover.jpg"],
        "origin_image_urls": ["https://example.test/detail-cover.jpg"],
        "opus": {
            "content": {
                "paragraphs": [
                    {"para_type": 1, "text": {"nodes": []}},
                    {
                        "para_type": 2,
                        "pic": {"pics": [{"url": "http://example.test/inline-1.jpg"}]},
                    },
                    {
                        "para_type": 2,
                        "pic": {"pics": [{"url": "https://example.test/inline-2.jpg"}]},
                    },
                ]
            }
        },
    }

    record = mediacrawler_crawl.hydrate_bilibili_article_record(
        search_record,
        detail,
        attempts=2,
    )

    assert record["content_text"] == body
    assert record["content_length"] > record["search_excerpt_length"]
    assert record["image_urls"] == [
        "https://example.test/inline-1.jpg",
        "https://example.test/inline-2.jpg",
    ]
    assert record["detail_image_count"] == 2
    assert record["search_preview_count"] == 1
    assert record["content_detail_status"] == "detail_observed"
    assert record["content_images_detail_status"] == "detail_observed"


def test_legacy_html_detail_preserves_paragraphs_and_extracts_body_images() -> None:
    search_record = mediacrawler_crawl.normalize_bilibili_article_record(
        search_item("9987069"),
        "青岛西海岸旅游攻略",
    )
    assert search_record is not None
    detail = {
        "content": (
            '<p>第一段</p><img src="//example.test/legacy-1.png">'
            '<p>第二段</p><img data-src="https://example.test/legacy-2.png">'
        ),
        "image_urls": [
            "https://example.test/legacy-1.png",
            "https://example.test/legacy-2.png",
        ],
        "opus": {"content": {"paragraphs": [{"para_type": 1}]}},
    }

    record = mediacrawler_crawl.hydrate_bilibili_article_record(
        search_record,
        detail,
        attempts=1,
    )

    assert record["content_text"] == "第一段\n第二段"
    assert record["detail_image_count"] == 2
    assert record["detail_image_sources"] == ["content_html_img", "content_html_img"]


def test_short_detail_body_is_accepted_when_it_is_observed() -> None:
    search_record = mediacrawler_crawl.normalize_bilibili_article_record(
        search_item("short"),
        "青岛西海岸旅游攻略",
    )
    assert search_record is not None

    record = mediacrawler_crawl.hydrate_bilibili_article_record(
        search_record,
        {"content": "短文", "image_urls": ["https://example.test/short.jpg"]},
        attempts=1,
    )
    record.update(
        {
            "followers_observed": True,
            "author_followers_source": "relation_stat",
            "followers_count": 1,
        }
    )

    assert mediacrawler_crawl.validate_formal_record("bilibili", record, set())["valid"] is True


def test_detail_retries_rate_limit_with_backoff(monkeypatch) -> None:
    calls = 0
    sleeps: list[float] = []

    def fetch(post_id, cookie_header):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise mediacrawler_crawl.BilibiliArticleDetailError(
                "rate limited",
                retryable=True,
                code=-509,
            )
        return {"content": "完整正文"}

    monkeypatch.setattr(mediacrawler_crawl, "fetch_bilibili_article_detail", fetch)
    monkeypatch.setattr(mediacrawler_crawl.random, "uniform", lambda low, high: 5.0)
    monkeypatch.setattr(mediacrawler_crawl.time, "sleep", sleeps.append)

    detail, attempts, retry_wait_seconds = (
        mediacrawler_crawl.fetch_bilibili_article_detail_with_retry("123")
    )

    assert detail["content"] == "完整正文"
    assert attempts == 3
    assert retry_wait_seconds == 15.0
    assert sleeps == [5.0, 10.0]


def test_detail_invalid_json_is_retryable(monkeypatch) -> None:
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.close()

    monkeypatch.setattr(
        mediacrawler_crawl,
        "urlopen",
        lambda request, timeout: Response(b"not-json"),
    )

    try:
        mediacrawler_crawl.fetch_bilibili_article_detail("123")
    except mediacrawler_crawl.BilibiliArticleDetailError as exc:
        assert exc.retryable is True
        assert "parse" in str(exc)
    else:
        raise AssertionError("invalid JSON must not be accepted")


def test_relation_stat_rate_limit_is_run_level_failure(monkeypatch) -> None:
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.close()

    monkeypatch.setattr(
        mediacrawler_crawl,
        "urlopen",
        lambda request, timeout: Response(
            json.dumps({"code": -509, "message": "频繁"}).encode()
        ),
    )

    try:
        mediacrawler_crawl.fetch_bilibili_follower_count("456")
    except mediacrawler_crawl.BilibiliFollowerFetchError as exc:
        assert exc.code == -509
        assert exc.retryable is True
        assert exc.runtime_blocking is True
    else:
        raise AssertionError("Bilibili rate limit must not become a missing follower value")


def test_rate_limit_detail_stops_run_without_marking_candidate_seen(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
    state_path = tmp_path / "state.json"
    mediacrawler_crawl.FrozenExecutionState.create(
        state_path,
        run_id="run-blocked",
        job_key="bili-detail-blocked",
        site_key="bilibili",
        job_kind="mediacrawler_search",
        plan={},
        frozen_inputs=[],
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setattr(
        mediacrawler_crawl,
        "run_bilibili_behavior_session",
        AsyncMock(return_value=({"cookie_header": ""}, {"ok": True})),
    )
    monkeypatch.setattr(mediacrawler_crawl, "behavior_evidence_valid", lambda value: True)
    monkeypatch.setattr(
        mediacrawler_crawl, "fetch_bilibili_wbi_keys", lambda value: ("a", "b")
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_article_page",
        lambda keyword, page, **kwargs: [search_item("blocked")],
    )

    def blocked_detail(post_id, cookie_header):
        raise mediacrawler_crawl.BilibiliArticleDetailError(
            "rate limited",
            retryable=True,
            code=-509,
            attempts=3,
            runtime_blocking=True,
        )

    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_article_detail_with_retry",
        blocked_detail,
    )
    monkeypatch.setattr(mediacrawler_crawl.time, "sleep", lambda value: None)
    args = SimpleNamespace(
        keyword="青岛旅游",
        candidate_hard_limit=3,
        target_new_posts=1,
        source_candidate_hard_limit=3,
        source_target_new_posts=1,
        max_stagnant_batches=3,
        db=str(db_path),
        start_page=1,
        top_refresh_max_pages=0,
        discovery_source_exhausted=False,
        discovery_job_id=None,
        discovery_query_fingerprint="",
        resume_identities_path=None,
    )

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is False
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    assert not [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == "bilibili_article_detail_blocked:-509"
    assert stopped["details"]["candidate_identities"] == []


def test_detail_failure_is_recorded_seen_and_next_post_continues(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
    state_path = tmp_path / "state.json"
    mediacrawler_crawl.FrozenExecutionState.create(
        state_path,
        run_id="run-1",
        job_key="bili-detail-failure",
        site_key="bilibili",
        job_kind="mediacrawler_search",
        plan={},
        frozen_inputs=[],
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setattr(
        mediacrawler_crawl,
        "run_bilibili_behavior_session",
        AsyncMock(return_value=({"cookie_header": ""}, {"ok": True})),
    )
    monkeypatch.setattr(mediacrawler_crawl, "behavior_evidence_valid", lambda value: True)
    monkeypatch.setattr(mediacrawler_crawl, "fetch_bilibili_wbi_keys", lambda value: ("a", "b"))
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_article_page",
        lambda keyword, page, **kwargs: (
            [search_item("failed")] if page == 1 else [search_item("success")]
        ),
    )

    def fail_detail(post_id, cookie_header):
        if post_id == "failed":
            raise mediacrawler_crawl.BilibiliArticleDetailError(
                "detail remained unparsable",
                retryable=True,
                code=0,
                attempts=3,
            )
        return (
            {
                "title": "详情标题",
                    "content": "青岛可用的完整正文",
                "image_urls": ["https://example.test/body.jpg"],
                "opus": {"content": {"paragraphs": []}},
            },
            1,
            0.0,
        )

    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_article_detail_with_retry",
        fail_detail,
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_follower_count",
        lambda creator_id, cookie_header: 100,
    )
    monkeypatch.setattr(mediacrawler_crawl.time, "sleep", lambda value: None)
    args = SimpleNamespace(
        keyword="青岛西海岸旅游攻略",
        candidate_hard_limit=3,
        target_new_posts=1,
        source_candidate_hard_limit=3,
        source_target_new_posts=1,
        max_stagnant_batches=3,
        db=str(db_path),
        start_page=1,
        top_refresh_max_pages=0,
        discovery_source_exhausted=False,
        discovery_job_id=None,
        discovery_query_fingerprint="",
        resume_identities_path=None,
    )

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is True
    assert result["run"]["returncode"] == 0
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert skipped[0]["details"]["identity"] == "failed"
    assert skipped[0]["details"]["failure_scope"] == "post"
    assert skipped[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "target_new_met"
    assert stopped["details"]["candidate_identities"] == ["failed", "success"]


def test_follower_failure_retries_then_records_skip_and_continues(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
    state_path = tmp_path / "state.json"
    mediacrawler_crawl.FrozenExecutionState.create(
        state_path,
        run_id="run-followers",
        job_key="bili-follower-failure",
        site_key="bilibili",
        job_kind="mediacrawler_search",
        plan={},
        frozen_inputs=[],
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setattr(
        mediacrawler_crawl,
        "run_bilibili_behavior_session",
        AsyncMock(return_value=({"cookie_header": ""}, {"ok": True})),
    )
    monkeypatch.setattr(mediacrawler_crawl, "behavior_evidence_valid", lambda value: True)
    monkeypatch.setattr(mediacrawler_crawl, "fetch_bilibili_wbi_keys", lambda value: ("a", "b"))

    failed = search_item("failed-followers")
    failed["mid"] = "creator-failed"
    success = search_item("success-followers")
    success["mid"] = "creator-success"
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_article_page",
        lambda keyword, page, **kwargs: [failed, success],
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_article_detail_with_retry",
        lambda post_id, cookie_header: (
            {
                "title": "详情标题",
                    "content": "青岛可用的完整正文",
                "image_urls": ["https://example.test/body.jpg"],
                "opus": {"content": {"paragraphs": []}},
            },
            1,
            0.0,
        ),
    )
    follower_calls: list[str] = []

    def follower_count(creator_id, cookie_header):
        follower_calls.append(creator_id)
        return None if creator_id == "creator-failed" else 100

    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_follower_count",
        follower_count,
    )
    monkeypatch.setattr(mediacrawler_crawl.time, "sleep", lambda value: None)
    args = SimpleNamespace(
        keyword="青岛西海岸旅游攻略",
        candidate_hard_limit=3,
        target_new_posts=1,
        source_candidate_hard_limit=3,
        source_target_new_posts=1,
        max_stagnant_batches=3,
        db=str(db_path),
        start_page=1,
        top_refresh_max_pages=0,
        discovery_source_exhausted=False,
        discovery_job_id=None,
        discovery_query_fingerprint="",
        resume_identities_path=None,
    )

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is True
    assert follower_calls == [
        "creator-failed",
        "creator-failed",
        "creator-failed",
        "creator-success",
    ]
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    assert skipped[0]["details"]["identity"] == "failed-followers"
    assert skipped[0]["details"]["detail"] == "creator_profile_failed"
    assert skipped[0]["details"]["attempts"] == 3
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "target_new_met"
    assert stopped["details"]["candidate_identities"] == [
        "failed-followers",
        "success-followers",
    ]


def test_reimporting_repaired_record_updates_without_duplication(tmp_path: Path) -> None:
    db_path = tmp_path / "repair.sqlite"
    record = hydrated_record()
    selected = [{"platform": "bilibili", "record": record}]
    summary = {
        "captured_at": "2026-08-02T12:00:00+00:00",
        "keyword": "青岛西海岸旅游攻略",
        "batch_dir": str(tmp_path / "batch"),
    }

    first = mediacrawler_crawl.import_valid_records(summary, selected, db_path)
    second = mediacrawler_crawl.import_valid_records(summary, selected, db_path)

    assert first["inserted_rows"] == 1
    assert second["updated_rows"] == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 1
        row = conn.execute(
            "SELECT content_text, post_images_count FROM web_posts WHERE platform_key='bilibili'"
        ).fetchone()
    assert row == ("第一段完整正文。\n第二段完整正文。", 1)
