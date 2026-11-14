"""#187 知乎正文图：同一 v2-<32位十六进制> 只保留原图。不访问平台、不启动浏览器。"""

from __future__ import annotations

import json
import sqlite3
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from .support import config, make_crawler as ZhihuCrawler
from trippostcollect.artifacts.formal_images import materialize_formal_record_images
from trippostcollect.db.content import import_valid_records
from trippostcollect.platforms.zhihu.parser import (
    ZhihuExtractor,
    collapse_zhihu_v2_image_urls,
    extract_image_urls_from_html,
)
from trippostcollect.platforms.zhihu import parser as zhihu_store
from trippostcollect.platforms.zhihu.models import ZhihuContent
from trippostcollect.platforms.zhihu.search_capture import ZhihuPageCapture
from trippostcollect.records.formal import formal_record_identity, validate_formal_record
from trippostcollect.artifacts.image_candidates import content_image_candidates


HASH = "ab" * 16
OTHER = "cd" * 16


def _url(host: str, name: str, *, query: str = "") -> str:
    suffix = f"?{query}" if query else ""
    return f"https://{host}/{name}{suffix}"


def _html(urls: list[str]) -> str:
    tags = "".join(f'<img data-original="{url}">' for url in urls)
    return f"<p>青岛正文</p>{tags}"


def _pair() -> tuple[str, str]:
    original = _url("picx.zhimg.com", f"v2-{HASH}_r.jpg", query="source=orig")
    thumb = _url("pic1.zhimg.com", f"50/v2-{HASH}_720w.jpg", query="source=thumb")
    return original, thumb


def test_paired_v2_urls_keep_original_at_first_position() -> None:
    original, thumb = _pair()
    kept = extract_image_urls_from_html(_html([thumb, original]))
    assert kept == [original]


def test_only_720w_is_kept() -> None:
    thumb = _url("pic1.zhimg.com", f"50/v2-{HASH}_720w.jpg")
    assert extract_image_urls_from_html(_html([thumb])) == [thumb]


def test_1440w_beats_720w() -> None:
    wide = _url("pic2.zhimg.com", f"v2-{HASH}_1440w.jpg")
    thumb = _url("pic1.zhimg.com", f"50/v2-{HASH}_720w.jpg")
    assert extract_image_urls_from_html(_html([thumb, wide])) == [wide]


def test_unsuffixed_beats_720w() -> None:
    bare = _url("pic3.zhimg.com", f"v2-{HASH}.jpg")
    thumb = _url("pic1.zhimg.com", f"v2-{HASH}_720w.jpg")
    assert extract_image_urls_from_html(_html([thumb, bare])) == [bare]


def test_b_beats_unknown_suffix() -> None:
    original_b = _url("pic1.zhimg.com", f"v2-{HASH}_b.jpg")
    unknown = _url("pic2.zhimg.com", f"v2-{HASH}_qhd.jpg")
    assert collapse_zhihu_v2_image_urls([unknown, original_b]) == [original_b]


def test_groups_keep_first_seen_order_and_non_v2_urls_stay() -> None:
    original, thumb = _pair()
    other = _url("pic2.zhimg.com", f"v2-{OTHER}_720w.jpg")
    external = "https://cdn.example/v2-" + HASH + "_r.jpg"
    external_thumb = "https://cdn.example/v2-" + HASH + "_720w.jpg"
    legacy = "https://pic1.zhimg.com/80/legacy-photo.jpg"
    urls = [thumb, external, original, other, legacy, external_thumb, legacy]
    assert extract_image_urls_from_html(_html(urls)) == [
        original,
        external,
        other,
        legacy,
        external_thumb,
    ]


def test_asset_projection_downloads_only_the_kept_original() -> None:
    original, thumb = _pair()
    content = ZhihuContent(
        content_id="paired",
        content_type="answer",
        content_detail_status="detail_observed",
        image_list=[thumb, original, "https://pic1.zhimg.com/80/legacy.jpg"],
    )
    assets = zhihu_store.zhihu_content_image_assets(content)
    assert [asset["url"] for asset in assets] == [
        original,
        "https://pic1.zhimg.com/80/legacy.jpg",
    ]
    assert [asset["source_index"] for asset in assets] == [0, 1]


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 5), color="purple").save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_fake_page_answer_downloads_and_imports_only_the_original(
    monkeypatch, tmp_path: Path
) -> None:
    original, thumb = _pair()
    state = {
        "initialState": {
            "entities": {
                "answers": {
                    "501": {
                        "id": "501",
                        "type": "answer",
                        "content": _html([thumb, original]),
                        "createdTime": 1_700_000_000,
                        "voteupCount": 3,
                        "commentCount": 1,
                        "favlistsCount": 2,
                        "question": {"id": "90"},
                        "author": {"id": "author-501", "name": "作者", "urlToken": "author"},
                    }
                }
            }
        }
    }
    parsed, code = ZhihuExtractor().content_from_initial_data(json.dumps(state), "501", "answer")
    assert code == ""
    assert parsed is not None
    assert parsed.image_list == [original]
    assert parsed.image_count == 1
    parsed.content_detail_status = "detail_observed"
    parsed.content_detail_source = "answer_detail"
    parsed.followers_count = 9
    parsed.followers_observed = True
    parsed.author_followers_source = "author_page"

    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE",
        str(tmp_path / "behavior_evidence.json"),
    )
    crawler = ZhihuCrawler()
    crawler._page_capture = ZhihuPageCapture.open(object())
    crawler.zhihu_client = AsyncMock()
    crawler.zhihu_client.get_content_image.return_value = png_bytes()

    await crawler.get_content_images(parsed)

    crawler.zhihu_client.get_content_image.assert_awaited_once_with(
        original,
        referer=parsed.content_url,
    )
    ledger_path = tmp_path / "behavior_evidence.content_visits.jsonl"
    visits = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("kind") == "visit"
    ]
    assert [visit["source"] for visit in visits] == ["cdn_direct"]
    assert visits[0]["stage"] == "note_image"
    assert len(parsed.content_visit_refs["images"]) == 1
    manifest_path = tmp_path / "zhihu" / "image_manifest.jsonl"
    rows = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()]
    assert [row["source_url"] for row in rows] == [original]

    record = zhihu_store.update_zhihu_content(parsed, source_keyword="青岛")
    validation = validate_formal_record(
        "zhihu", record, set(), content_image_candidates=content_image_candidates,
    )
    assert validation["valid"] is True
    assert validation["content_image_count"] == 1
    identity = formal_record_identity("zhihu", record)
    selected = [{
        "platform": "zhihu",
        "identity": identity,
        "record": record,
        "manifest_paths": [str(manifest_path)],
    }]
    media_root = tmp_path / "media"
    report, materialized, complete = materialize_formal_record_images(
        selected,
        project_root=tmp_path,
        media_root=media_root,
        promote=True,
    )
    assert report["complete"] is True
    assert report["expected_images"] == report["validated_images"] == 1
    assert identity in complete
    selected[0]["materialized_images"] = materialized[identity]
    imported = import_valid_records(
        {"captured_at": "2026-11-14T00:00:00+00:00", "keyword": "青岛", "batch_dir": str(tmp_path)},
        selected,
        tmp_path / "formal.sqlite",
        project_root=tmp_path,
        media_root=media_root,
        require_local_images=True,
    )
    assert imported["inserted_rows"] == 1
    with sqlite3.connect(tmp_path / "formal.sqlite") as conn:
        post = conn.execute(
            "SELECT post_images_count FROM web_posts WHERE platform_post_id=?",
            ("501",),
        ).fetchone()
        images = conn.execute(
            "SELECT image_url FROM web_post_images"
        ).fetchall()
    assert post[0] == 1
    assert [row[0] for row in images] == [original]
