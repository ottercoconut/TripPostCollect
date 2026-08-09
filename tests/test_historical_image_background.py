from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from PIL import Image


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import historical_image_worker as worker  # noqa: E402
import historical_platform_images as batch  # noqa: E402
from trippostcollect.artifacts.image_proxy import (  # noqa: E402
    RemoteImageFetchError,
    RemoteImagePreview,
)
from trippostcollect.artifacts.image_candidates import (  # noqa: E402
    content_image_candidates,
)


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (7, 5), color=(20, 40, 60)).save(output, format="PNG")
    return output.getvalue()


def _generic_post_plan(
    platform_key: str,
    platform_post_id: str,
    record: dict[str, object],
    *,
    excluded_asset_keys: set[str] | None = None,
) -> SimpleNamespace:
    excluded = excluded_asset_keys or set()
    prepared_images = []
    for source_index, candidate in enumerate(
        candidate
        for candidate in content_image_candidates(platform_key, record)
        if candidate.source_asset_key not in excluded
    ):
        prepared_images.append(
            {
                **candidate.as_image_item(),
                "source_index": source_index,
            }
        )
    return SimpleNamespace(
        platform_post_id=platform_post_id,
        authoritative_images=len(prepared_images),
        prepared_images=tuple(prepared_images),
    )


def test_generic_weibo_download_uses_large_proxy_and_ignores_avatar(
    monkeypatch, tmp_path: Path
) -> None:
    seen: list[tuple[str, dict[str, str]]] = []

    def fake_fetch(url, *, headers, **_kwargs):
        seen.append((url, headers))
        return RemoteImagePreview(
            content=_png_bytes(),
            media_type="image/png",
            final_url=url,
        )

    monkeypatch.setattr(batch, "fetch_remote_image_bytes", fake_fetch)
    record = {
        "note_id": "wb-1",
        "note_url": "https://m.weibo.cn/detail/wb-1",
        "image_list_source": "mblog.pics",
        "image_list": [
            {
                "url": "https://wx1.sinaimg.cn/orj360/body.jpg",
                "pid": "body-pid",
            }
        ],
        "avatar_url": "https://tvax1.sinaimg.cn/crop.0.0.100.100/avatar.jpg",
    }
    entries, report = batch._download_generic_post(
        _generic_post_plan("weibo", "wb-1", record),
        record,
        platform_key="weibo",
        staging_root=tmp_path,
        cookie_header="SUB=test",
        deadline=time.monotonic() + 10,
    )

    assert report["downloaded_images"] == 1
    assert report["failed_images"] == 0
    assert len(entries) == 1
    assert entries[0].source_asset_key == "weibo:pid:body-pid"
    assert entries[0].source_url == "https://wx1.sinaimg.cn/orj360/body.jpg"
    assert entries[0].mime_type == "image/png"
    assert seen == [
        (
            "https://i1.wp.com/wx1.sinaimg.cn/large/body.jpg",
            {
                "User-Agent": batch.MOBILE_USER_AGENT,
                "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://m.weibo.cn/detail/wb-1",
                "Cookie": "SUB=test",
            },
        )
    ]


def test_weibo_download_falls_back_to_direct_large_then_original(
    monkeypatch, tmp_path: Path
) -> None:
    seen: list[str] = []

    def fake_fetch(url, **_kwargs):
        seen.append(url)
        if "i1.wp.com" in url:
            raise RemoteImageFetchError("proxy rejected", http_status=400)
        if "/large/" in url:
            return RemoteImagePreview(
                content=b"not-an-image",
                media_type="image/jpeg",
                final_url=url,
            )
        return RemoteImagePreview(
            content=_png_bytes(),
            media_type="image/png",
            final_url=url,
        )

    monkeypatch.setattr(batch, "fetch_remote_image_bytes", fake_fetch)
    record = {
        "note_id": "wb-fallback",
        "note_url": "https://m.weibo.cn/detail/wb-fallback",
        "image_list_source": "mblog.pics",
        "image_list": [
            {
                "url": "https://wx1.sinaimg.cn/orj360/body.jpg",
                "pid": "body-pid",
            }
        ],
    }

    entries, report = batch._download_generic_post(
        _generic_post_plan("weibo", "wb-fallback", record),
        record,
        platform_key="weibo",
        staging_root=tmp_path,
        cookie_header="SUB=test",
        deadline=time.monotonic() + 10,
    )

    assert seen == [
        "https://i1.wp.com/wx1.sinaimg.cn/large/body.jpg",
        "https://wx1.sinaimg.cn/large/body.jpg",
        "https://wx1.sinaimg.cn/orj360/body.jpg",
    ]
    assert entries[0].fetch_status == "downloaded"
    assert entries[0].attempts == 3
    assert report["downloaded_images"] == 1
    assert report["failed_images"] == 0


def test_weibo_session_after_reloads_snapshot_before_login_check(monkeypatch) -> None:
    checked_headers: list[str] = []

    monkeypatch.setattr(
        batch,
        "load_cookie_snapshot",
        lambda _platform: {"cookie_header": "SUB=fresh"},
    )

    def fake_login(cookie_header: str) -> dict[str, object]:
        checked_headers.append(cookie_header)
        return {"ok": True, "source": "m_weibo_cn_api_config"}

    monkeypatch.setattr(batch, "_weibo_login", fake_login)

    result = batch._session_after(
        "weibo",
        "SUB=stale;mweibo_short_token=expired",
    )

    assert checked_headers == ["SUB=fresh"]
    assert result["ok"] is True
    assert result["cookie_snapshot_reloaded"] is True


def test_weibo_login_bootstraps_mobile_cookies_before_api_check(monkeypatch) -> None:
    requests = []

    class FakeHeaders:
        def __init__(self, values):
            self.values = values

        def get_all(self, _name):
            return self.values

    class FakeResponse:
        def __init__(self, payload: bytes, *, cookies=()):
            self.status = 200
            self.payload = payload
            self.headers = FakeHeaders(list(cookies))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return self.payload

    responses = iter(
        [
            FakeResponse(
                b"<html></html>",
                cookies=("MLOGIN=1; Path=/", "XSRF-TOKEN=fresh; Path=/"),
            ),
            FakeResponse(b'{"ok":1,"data":{"login":true,"uid":"123"}}'),
        ]
    )

    def fake_urlopen(request, **_kwargs):
        requests.append(request)
        return next(responses)

    monkeypatch.setattr(batch, "urlopen", fake_urlopen)

    result = batch._weibo_login("SUB=durable; MLOGIN=stale")

    assert [request.full_url for request in requests] == [
        "https://m.weibo.cn/",
        "https://m.weibo.cn/api/config",
    ]
    assert "SUB=durable" in requests[1].get_header("Cookie")
    assert "MLOGIN=1" in requests[1].get_header("Cookie")
    assert "XSRF-TOKEN=fresh" in requests[1].get_header("Cookie")
    assert result["ok"] is True
    assert result["bootstrap_http_status"] == 200


def test_generic_download_retries_recoverable_failure_three_times(
    monkeypatch, tmp_path: Path
) -> None:
    attempts = 0

    def failed_fetch(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise RemoteImageFetchError("temporary", http_status=503, retryable=True)

    monkeypatch.setattr(batch, "fetch_remote_image_bytes", failed_fetch)
    monkeypatch.setattr(batch.time, "sleep", lambda _seconds: None)
    record = {
        "content_id": "zh-1",
        "content_url": "https://www.zhihu.com/question/1/answer/2",
        "image_list": ["https://pic1.zhimg.com/v2-body_r.jpg"],
    }
    entries, report = batch._download_generic_post(
        _generic_post_plan("zhihu", "zh-1", record),
        record,
        platform_key="zhihu",
        staging_root=tmp_path,
        cookie_header="d_c0=test;z_c0=test",
        deadline=time.monotonic() + 10,
    )

    assert attempts == 3
    assert report["downloaded_images"] == 0
    assert report["failure_codes"] == ["image_download_retryable"]
    assert entries[0].attempts == 3
    assert entries[0].http_status == 503


def test_douyin_failure_refreshes_image_detail_once_within_attempt_budget(
    monkeypatch, tmp_path: Path
) -> None:
    fetched_urls: list[str] = []
    refresh_calls: list[str] = []

    def fake_fetch(url, **_kwargs):
        fetched_urls.append(url)
        if "old-sign" in url:
            raise RemoteImageFetchError("expired", http_status=403, retryable=True)
        return RemoteImagePreview(
            content=_png_bytes(),
            media_type="image/png",
            final_url=url,
        )

    def fake_refresh(post_id, _staging_root):
        refresh_calls.append(post_id)
        return {0: "https://p3-sign.douyinpic.com/fresh-sign.jpeg"}

    monkeypatch.setattr(batch, "fetch_remote_image_bytes", fake_fetch)
    monkeypatch.setattr(batch, "_refresh_douyin_urls", fake_refresh)
    record = {
        "aweme_id": "dy-1",
        "aweme_url": "https://www.douyin.com/note/dy-1",
        "note_download_url": ["https://p3-sign.douyinpic.com/old-sign.jpeg"],
    }
    entries, report = batch._download_generic_post(
        _generic_post_plan("douyin", "dy-1", record),
        record,
        platform_key="douyin",
        staging_root=tmp_path,
        cookie_header="sessionid=test",
        deadline=time.monotonic() + 10,
    )

    assert fetched_urls == [
        "https://p3-sign.douyinpic.com/old-sign.jpeg",
        "https://p3-sign.douyinpic.com/fresh-sign.jpeg",
    ]
    assert refresh_calls == ["dy-1"]
    assert entries[0].fetch_status == "downloaded"
    assert entries[0].attempts == 2
    assert entries[0].source_url.endswith("old-sign.jpeg")
    assert report["detail_refresh_attempted"] is True
    assert report["detail_refresh_succeeded"] is True
    assert report["video_requests"] == report["music_requests"] == report["cover_requests"] == 0


def test_generic_download_uses_approved_plan_instead_of_raw_candidates(
    monkeypatch, tmp_path: Path
) -> None:
    seen: list[str] = []

    def fake_fetch(url, **_kwargs):
        seen.append(url)
        return RemoteImagePreview(
            content=_png_bytes(),
            media_type="image/png",
            final_url=url,
        )

    monkeypatch.setattr(batch, "fetch_remote_image_bytes", fake_fetch)
    record = {
        "content_id": "zh-excluded",
        "content_url": "https://zhuanlan.zhihu.com/p/zh-excluded",
        "image_list": [
            "https://pic1.zhimg.com/v2-excluded_r.jpg",
            "https://pic1.zhimg.com/v2-kept_r.jpg",
        ],
    }
    excluded = content_image_candidates("zhihu", record)[0]
    post = _generic_post_plan(
        "zhihu",
        "zh-excluded",
        record,
        excluded_asset_keys={excluded.source_asset_key},
    )

    entries, report = batch._download_generic_post(
        post,
        record,
        platform_key="zhihu",
        staging_root=tmp_path,
        cookie_header="d_c0=test;z_c0=test",
        deadline=time.monotonic() + 10,
    )

    assert seen == ["https://pic1.zhimg.com/v2-kept_r.jpg"]
    assert len(entries) == report["downloaded_images"] == 1
    assert entries[0].source_index == 0
    assert entries[0].source_asset_key != excluded.source_asset_key


def test_background_failure_classification_and_capacity_gate(
    monkeypatch, tmp_path: Path
) -> None:
    database = tmp_path / "database.sqlite"
    database.write_bytes(b"sqlite")
    monkeypatch.setattr(
        worker.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10**15, used=0, free=10**15),
    )
    inventory = {
        platform: {"local_gap": 1}
        for platform in worker.PLATFORM_ORDER
    }

    capacity = worker._capacity_gate(database, inventory)

    assert capacity["ok"] is True
    assert capacity["remaining_estimate_bytes"] == sum(worker.P95_IMAGE_BYTES.values())
    assert worker._classify_failure({"error": "Weibo online login check failed"}) == "auth_required"
    assert worker._classify_failure({"error": "image_decode_failed"}) == "failed"


def test_worker_failure_decision_retries_only_with_remaining_budget() -> None:
    retry = worker._failure_decision(
        {
            "downloaded_posts": [
                {
                    "platform_post_id": "bili-1",
                    "failed_images": 1,
                    "failure_codes": ["image_download_retryable"],
                    "failures": [{"attempts": 1, "error_code": "image_download_retryable"}],
                }
            ]
        }
    )
    exhausted = worker._failure_decision(
        {
            "downloaded_posts": [
                {
                    "platform_post_id": "bili-1",
                    "failed_images": 1,
                    "failure_codes": ["image_download_retryable"],
                    "failures": [{"attempts": 3, "error_code": "image_download_retryable"}],
                }
            ]
        }
    )

    assert retry["action"] == "retry"
    assert retry["attempts_used"] == 1
    assert exhausted["action"] == "defer"
    assert exhausted["reason"] == "image_retry_exhausted"


def test_worker_records_terminal_image_for_review_and_registry(tmp_path: Path) -> None:
    state: dict[str, object] = {"deferred_posts": {}}
    decision = worker._failure_decision(
        {
            "downloaded_posts": [
                {
                    "platform_post_id": "24066523",
                    "failed_images": 1,
                    "failure_codes": ["image_source_unavailable"],
                    "failures": [
                        {
                            "source_index": 3,
                            "attempts": 1,
                            "http_status": 404,
                            "error_code": "image_source_unavailable",
                        }
                    ],
                }
            ]
        }
    )

    worker._record_deferred_posts(
        state,
        platform_key="bilibili",
        report_path=tmp_path / "report.json",
        decision=decision,
    )
    registry_path = tmp_path / "deferred-posts.json"
    worker._sync_deferred_registry(registry_path, state)

    assert decision["action"] == "defer"
    assert state["deferred_posts"]["bilibili"]["24066523"]["status"] == "review_required"
    assert json.loads(registry_path.read_text(encoding="utf-8"))["platform_post_ids"] == {
        "bilibili": ["24066523"]
    }
