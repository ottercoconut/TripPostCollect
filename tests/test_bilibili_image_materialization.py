from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

from trippostcollect.artifacts.image_manifest import parse_manifest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import mediacrawler_crawl  # noqa: E402


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (6, 5), color=(10, 20, 30)).save(output, format="PNG")
    return output.getvalue()


def search_item(post_id: str = "123") -> dict:
    return {
        "id": post_id,
        "title": "搜索标题",
        "desc": "搜索摘要",
        "arcurl": f"https://www.bilibili.com/read/cv{post_id}/",
        "image_urls": ["https://preview.test/search-preview.jpg"],
        "pubdate": 1_700_000_000,
        "like": 1,
        "reply": 2,
        "view": 3,
        "author": "作者",
        "mid": "456",
    }


def detail_payload() -> dict:
    return {
        "title": "详情标题",
        "content": "第一段完整正文。\n图片\n第二段完整正文。",
        "image_urls": ["https://cover.test/detail-cover.jpg"],
        "opus": {
            "content": {
                "paragraphs": [
                    {
                        "para_type": 2,
                        "pic": {
                            "pics": [
                                {"url": "https://i0.hdslb.com/bfs/article/body-a.png"},
                                {"url": "//i1.hdslb.com/bfs/article/body-b.png"},
                            ]
                        },
                    }
                ]
            }
        },
    }


def hydrated_record(post_id: str = "123") -> dict:
    search = mediacrawler_crawl.normalize_bilibili_article_record(search_item(post_id), "青岛旅游")
    assert search is not None
    return mediacrawler_crawl.hydrate_bilibili_article_record(
        search,
        detail_payload(),
        attempts=1,
    )


def run_args(
    db_path: Path,
    *,
    download_images: bool = True,
    candidate_hard_limit: int = 3,
    target_new_posts: int = 1,
    completion_mode: str = "target-new-posts",
) -> SimpleNamespace:
    return SimpleNamespace(
        keyword="青岛旅游",
        candidate_hard_limit=candidate_hard_limit,
        target_new_posts=target_new_posts,
        source_candidate_hard_limit=candidate_hard_limit,
        source_target_new_posts=target_new_posts,
        completion_mode=completion_mode,
        max_stagnant_batches=3,
        db=str(db_path),
        start_page=1,
        top_refresh_max_pages=0,
        discovery_source_exhausted=False,
        discovery_job_id=None,
        discovery_query_fingerprint="",
        resume_identities_path=None,
        download_images=download_images,
    )


def install_successful_run_mocks(monkeypatch) -> None:
    monkeypatch.setattr(
        mediacrawler_crawl,
        "run_bilibili_behavior_session",
        AsyncMock(return_value=({"cookie_header": "SESSDATA=test"}, {"ok": True})),
    )
    monkeypatch.setattr(mediacrawler_crawl, "behavior_evidence_valid", lambda value: True)
    monkeypatch.setattr(mediacrawler_crawl, "fetch_bilibili_wbi_keys", lambda value: ("a", "b"))
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_article_page",
        lambda keyword, page, **kwargs: [search_item("123")],
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_article_detail_with_retry",
        lambda post_id, cookie_header: (detail_payload(), 1, 0.0),
    )
    monkeypatch.setattr(mediacrawler_crawl, "fetch_bilibili_follower_count", lambda *args: 42)
    monkeypatch.setattr(mediacrawler_crawl.time, "sleep", lambda value: None)


def test_detail_images_download_in_order_with_current_session_headers(tmp_path: Path) -> None:
    record = hydrated_record()
    fetched: list[tuple[str, str, str]] = []

    def fetcher(source_url: str, post_id: str, cookie_header: str):
        fetched.append((source_url, post_id, cookie_header))
        return mediacrawler_crawl.RemoteImagePreview(
            content=png_bytes(),
            media_type="image/png",
            final_url=source_url,
            http_status=200,
        )

    entries = mediacrawler_crawl.download_bilibili_record_images(
        record,
        cookie_header="SESSDATA=test",
        platform_data_root=tmp_path,
        fetcher=fetcher,
        sleep_fn=lambda value: None,
    )

    assert [item[0] for item in fetched] == record["image_urls"]
    assert all(item[1:] == ("123", "SESSDATA=test") for item in fetched)
    assert "https://preview.test/search-preview.jpg" not in [item[0] for item in fetched]
    assert [entry.source_index for entry in entries] == [0, 1]
    assert all(entry.fetch_status == "downloaded" for entry in entries)
    assert all(entry.mime_type == "image/png" for entry in entries)
    assert all((tmp_path / str(entry.staging_path)).is_file() for entry in entries)
    assert list(tmp_path.rglob("*.mp4")) == []

    headers = mediacrawler_crawl.bilibili_image_headers("123", "SESSDATA=test")
    assert headers["Cookie"] == "SESSDATA=test"
    assert headers["Referer"] == "https://www.bilibili.com/read/cv123/"
    assert headers["User-Agent"] == mediacrawler_crawl.BILIBILI_BROWSER_USER_AGENT


def test_search_only_record_never_creates_image_request(tmp_path: Path) -> None:
    record = mediacrawler_crawl.normalize_bilibili_article_record(search_item(), "青岛旅游")
    assert record is not None
    calls = 0

    def fetcher(*args):
        nonlocal calls
        calls += 1
        raise AssertionError("search preview must not be downloaded")

    entries = mediacrawler_crawl.download_bilibili_record_images(
        record,
        cookie_header="",
        platform_data_root=tmp_path,
        fetcher=fetcher,
    )

    assert entries == []
    assert calls == 0


def test_retryable_image_failure_writes_only_failed_manifest_status(tmp_path: Path) -> None:
    record = hydrated_record()
    calls = 0
    sleeps: list[float] = []
    logs: list[str] = []

    def fetcher(*args):
        nonlocal calls
        calls += 1
        raise mediacrawler_crawl.RemoteImageFetchError(
            "HTTP 503",
            http_status=503,
            retryable=True,
        )

    entries = mediacrawler_crawl.download_bilibili_record_images(
        record,
        cookie_header="",
        platform_data_root=tmp_path,
        fetcher=fetcher,
        sleep_fn=sleeps.append,
        log_fn=logs.append,
    )

    assert calls == 3
    assert len(sleeps) == 2
    assert len(entries) == 1
    assert entries[0].fetch_status == "failed"
    assert entries[0].attempts == 3
    assert entries[0].http_status == 503
    assert entries[0].error_code == "image_download_retryable"
    assert entries[0].staging_path is None
    assert list(tmp_path.rglob("*.png")) == []
    assert sum("[image_download_retry]" in line for line in logs) == 2
    assert sum("[image_download_retry_exhausted]" in line for line in logs) == 1


def test_terminal_http_failure_is_not_mislabeled_retryable(tmp_path: Path) -> None:
    record = hydrated_record()
    calls = 0

    def fetcher(*args):
        nonlocal calls
        calls += 1
        raise mediacrawler_crawl.RemoteImageFetchError(
            "HTTP 404",
            http_status=404,
            retryable=False,
        )

    entries = mediacrawler_crawl.download_bilibili_record_images(
        record,
        cookie_header="",
        platform_data_root=tmp_path,
        fetcher=fetcher,
        sleep_fn=lambda _value: None,
    )

    assert calls == 1
    assert entries[0].attempts == 1
    assert entries[0].http_status == 404
    assert entries[0].error_code == "image_source_unavailable"


def test_empty_image_response_retries_and_recovers(tmp_path: Path) -> None:
    record = hydrated_record()
    responses = [b"", b"", png_bytes(), png_bytes()]

    def fetcher(source_url: str, post_id: str, cookie_header: str):
        return mediacrawler_crawl.RemoteImagePreview(
            content=responses.pop(0),
            media_type="image/png",
            final_url=source_url,
            http_status=200,
        )

    entries = mediacrawler_crawl.download_bilibili_record_images(
        record,
        cookie_header="",
        platform_data_root=tmp_path,
        fetcher=fetcher,
        sleep_fn=lambda _value: None,
    )

    assert [entry.attempts for entry in entries] == [3, 1]
    assert all(entry.fetch_status == "downloaded" for entry in entries)


def test_remote_size_failure_uses_unified_error_code() -> None:
    error = mediacrawler_crawl.RemoteImageFetchError(
        "remote image exceeds 20 bytes",
        code="image_too_large",
    )

    assert mediacrawler_crawl.remote_image_failure_code(error) == "image_too_large"


def test_bilibili_run_writes_manifest_and_never_downloads_preview(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
    install_successful_run_mocks(monkeypatch)
    fetched: list[str] = []

    def fetch_image(source_url: str, post_id: str, cookie_header: str):
        fetched.append(source_url)
        return mediacrawler_crawl.RemoteImagePreview(
            content=png_bytes(),
            media_type="image/png",
            final_url=source_url,
            http_status=200,
        )

    monkeypatch.setattr(mediacrawler_crawl, "fetch_bilibili_image_bytes", fetch_image)
    result = mediacrawler_crawl.run_bilibili_article_search(run_args(db_path), tmp_path / "batch")

    assert result["ok"] is True
    assert result["media_enabled"] is True
    assert result["video_enabled"] is False
    assert result["image_materialization"]["expected_images"] == 2
    assert result["image_materialization"]["downloaded_images"] == 2
    assert result["image_materialization"]["complete"] is True
    assert fetched == [
        "https://i0.hdslb.com/bfs/article/body-a.png",
        "https://i1.hdslb.com/bfs/article/body-b.png",
    ]
    assert all("preview" not in url for url in fetched)
    manifest_path = Path(result["image_materialization"]["manifest_paths"][0])
    manifest_bytes = manifest_path.read_bytes()
    entries = parse_manifest(manifest_bytes)
    assert len(entries) == 2
    assert b"SESSDATA" not in manifest_bytes
    assert b"Cookie" not in manifest_bytes
    assert result["image_materialization"]["manifest_sha256"] == (
        mediacrawler_crawl.manifest_sha256(entries)
    )
    assert list((tmp_path / "batch").rglob("*.mp4")) == []


def test_image_failure_keeps_page_and_candidate_unseen(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
    state_path = tmp_path / "state.json"
    mediacrawler_crawl.FrozenExecutionState.create(
        state_path,
        run_id="run-1",
        job_key="bili-image-failure",
        site_key="bilibili",
        job_kind="mediacrawler_search",
        plan={},
        frozen_inputs=[],
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    install_successful_run_mocks(monkeypatch)

    def fail_image(source_url: str, post_id: str, cookie_header: str):
        raise mediacrawler_crawl.RemoteImageFetchError(
            "HTTP 503",
            http_status=503,
            retryable=True,
        )

    monkeypatch.setattr(mediacrawler_crawl, "fetch_bilibili_image_bytes", fail_image)
    result = mediacrawler_crawl.run_bilibili_article_search(run_args(db_path), tmp_path / "batch")

    assert result["ok"] is False
    assert result["run"]["returncode"] == 0
    assert result["image_materialization"]["downloaded_images"] == 0
    assert result["image_materialization"]["retryable_failures"] == 1
    manifest_path = Path(result["image_materialization"]["manifest_paths"][0])
    manifest = parse_manifest(manifest_path.read_bytes())
    assert len(manifest) == 1 and manifest[0].fetch_status == "failed"
    content_jsonl = next((tmp_path / "batch").rglob("search_contents_*.jsonl"))
    assert content_jsonl.read_text(encoding="utf-8") == ""

    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    deferred = [event for event in events if event["type"] == "candidate_deferred"]
    assert deferred[0]["details"]["identity"] == "123"
    assert deferred[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "deferred_retry_pending"
    assert stopped["details"]["stop_detail"] == "retryable_candidate_failures"
    assert stopped["details"]["resume_page"] == 1
    assert stopped["details"]["batch_complete"] is False
    assert stopped["details"]["candidate_identities"] == []
    assert stopped["details"]["deferred_retryable_count"] == 1


def test_deferred_candidate_wins_over_empty_page_exhaustion(
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
        run_id="run-empty-after-deferred",
        job_key="bili-empty-after-deferred",
        site_key="bilibili",
        job_kind="mediacrawler_search",
        plan={},
        frozen_inputs=[],
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    install_successful_run_mocks(monkeypatch)
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_article_page",
        lambda keyword, page, **kwargs: [search_item("123")] if page == 1 else [],
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_image_bytes",
        lambda *args: (_ for _ in ()).throw(
            mediacrawler_crawl.RemoteImageFetchError(
                "HTTP 503", http_status=503, retryable=True
            )
        ),
    )

    result = mediacrawler_crawl.run_bilibili_article_search(
        run_args(db_path, target_new_posts=2), tmp_path / "batch"
    )

    assert result["ok"] is False
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "deferred_retry_pending"
    assert stopped["details"]["stop_detail"] == "retryable_candidate_failures"
    assert stopped["details"]["resume_page"] == 1
    assert stopped["details"]["source_has_more"] is True
    assert stopped["details"]["batch_complete"] is False


def test_terminal_image_failure_stops_without_deferring_candidate(
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
        run_id="run-terminal-image",
        job_key="bili-terminal-image",
        site_key="bilibili",
        job_kind="mediacrawler_search",
        plan={},
        frozen_inputs=[],
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    install_successful_run_mocks(monkeypatch)
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_image_bytes",
        lambda *args: (_ for _ in ()).throw(
            mediacrawler_crawl.RemoteImageFetchError(
                "HTTP 404", http_status=404, retryable=False
            )
        ),
    )

    result = mediacrawler_crawl.run_bilibili_article_search(
        run_args(db_path), tmp_path / "batch"
    )

    assert result["ok"] is False
    assert result["run"]["returncode"] == 1
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    assert not [event for event in events if event["type"] == "candidate_deferred"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["image_error_code"] == "image_source_unavailable"
    assert stopped["details"]["candidate_identities"] == []


def test_deferred_candidate_wins_when_it_exactly_hits_hard_limit(
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
        run_id="run-deferred-limit",
        job_key="bili-deferred-limit",
        site_key="bilibili",
        job_kind="mediacrawler_search",
        plan={},
        frozen_inputs=[],
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    install_successful_run_mocks(monkeypatch)
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_image_bytes",
        lambda *args: (_ for _ in ()).throw(
            mediacrawler_crawl.RemoteImageFetchError(
                "HTTP 503", http_status=503, retryable=True
            )
        ),
    )

    mediacrawler_crawl.run_bilibili_article_search(
        run_args(db_path, candidate_hard_limit=1), tmp_path / "batch"
    )

    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "deferred_retry_pending"


def test_image_failure_does_not_block_later_bilibili_candidate(
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
        run_id="run-continue",
        job_key="bili-image-continue",
        site_key="bilibili",
        job_kind="mediacrawler_search",
        plan={},
        frozen_inputs=[],
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    install_successful_run_mocks(monkeypatch)
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_article_page",
        lambda keyword, page, **kwargs: (
            [search_item("123"), search_item("124")] if page == 1 else []
        ),
    )

    def selective_image(source_url: str, post_id: str, cookie_header: str):
        if post_id == "123":
            raise mediacrawler_crawl.RemoteImageFetchError(
                "HTTP 503",
                http_status=503,
                retryable=True,
            )
        return mediacrawler_crawl.RemoteImagePreview(
            content=png_bytes(),
            media_type="image/png",
            final_url=source_url,
            http_status=200,
        )

    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_image_bytes",
        selective_image,
    )
    result = mediacrawler_crawl.run_bilibili_article_search(
        run_args(db_path), tmp_path / "batch"
    )

    assert result["ok"] is True
    assert result["image_materialization"]["complete"] is True
    assert result["image_materialization"]["deferred_retryable_count"] == 1
    content_jsonl = next((tmp_path / "batch").rglob("search_contents_*.jsonl"))
    rows = [json.loads(line) for line in content_jsonl.read_text().splitlines()]
    assert [row["content_id"] for row in rows] == ["124"]
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "target_new_met"
    assert stopped["details"]["resume_page"] == 1
    assert stopped["details"]["candidate_identities"] == ["124"]


def test_known_post_id_is_skipped_before_detail_or_image_requests(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        conn.execute(
            "INSERT INTO web_posts VALUES ('bilibili', '123', 'https://www.bilibili.com/read/cv123/')"
        )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "run_bilibili_behavior_session",
        AsyncMock(return_value=({"cookie_header": ""}, {"ok": True})),
    )
    monkeypatch.setattr(mediacrawler_crawl, "behavior_evidence_valid", lambda value: True)
    monkeypatch.setattr(mediacrawler_crawl, "fetch_bilibili_wbi_keys", lambda value: ("a", "b"))
    requested_pages: list[int] = []

    def fetch_page(keyword, page, **kwargs):
        requested_pages.append(page)
        return [search_item("123")] if page == 1 else []

    monkeypatch.setattr(mediacrawler_crawl, "fetch_bilibili_article_page", fetch_page)
    monkeypatch.setattr(
        mediacrawler_crawl,
        "fetch_bilibili_article_detail_with_retry",
        lambda *args: (_ for _ in ()).throw(AssertionError("known post detail requested")),
    )
    monkeypatch.setattr(
        mediacrawler_crawl,
        "download_bilibili_record_images",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("known post image requested")),
    )

    result = mediacrawler_crawl.run_bilibili_article_search(run_args(db_path), tmp_path / "batch")

    assert result["ok"] is False
    assert requested_pages == [1, 2]
    assert result["image_materialization"]["candidate_posts"] == 0
    assert result["image_materialization"]["expected_images"] == 0
