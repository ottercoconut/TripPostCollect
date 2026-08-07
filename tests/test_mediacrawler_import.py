from __future__ import annotations

from hashlib import sha256
import io
import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path

from PIL import Image

from trippostcollect.db.bootstrap import bootstrap_connection


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

mediacrawler_crawl = import_module("mediacrawler_crawl")


def test_content_import_bootstrap_preserves_parent_runner_jobs() -> None:
    one_off_config = {
        "jobs": [
            {
                "job_key": "once_first",
                "site_key": "weibo",
                "target_url": "https://example.invalid/first",
                "job_kind": "mediacrawler_search",
                "enabled": True,
            },
            {
                "job_key": "once_second",
                "site_key": "douyin",
                "target_url": "https://example.invalid/second",
                "job_kind": "mediacrawler_search",
                "enabled": True,
            },
        ]
    }
    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, config=one_off_config)

        result = mediacrawler_crawl.ensure_web_schema(conn)

        jobs = conn.execute(
            "SELECT job_key, enabled FROM crawl_jobs ORDER BY job_key"
        ).fetchall()
    assert result["synced_jobs"] == 0
    assert jobs == [("once_first", 1), ("once_second", 1)]


def test_upsert_preserves_verified_local_image_metadata(tmp_path: Path) -> None:
    original_image_url = "https://example.test/old/notes_pre_post/image.jpg"
    refreshed_image_url = "https://example.test/new/notes_pre_post/image.jpg"
    post = {
        "platform_key": "xhs",
        "platform_post_id": "note-1",
        "source_type": "mediacrawler_search",
        "source_url": "https://www.xiaohongshu.com/explore/note-1",
        "canonical_url": "https://www.xiaohongshu.com/explore/note-1",
        "captured_at": "2026-08-07T00:00:00+00:00",
        "raw_sample_json": "{}",
        "_image_items": [
            {"url": original_image_url, "role": "content", "source_key": "image_list"}
        ],
    }
    local_path = Path("outputs/images/note-1/0.jpg")
    absolute_path = tmp_path / local_path
    absolute_path.parent.mkdir(parents=True)
    payload = io.BytesIO()
    Image.new("RGB", (4, 3), color=(1, 2, 3)).save(payload, format="WEBP")
    image_bytes = payload.getvalue()
    absolute_path.write_bytes(image_bytes)
    image_sha256 = sha256(image_bytes).hexdigest()

    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, sync_jobs=False)
        post_id, inserted = mediacrawler_crawl.upsert_web_post(
            conn,
            dict(post),
            project_root=tmp_path,
            media_root=tmp_path / "data/media",
        )
        assert inserted is True
        conn.execute(
            """
            UPDATE web_post_images
            SET local_path=?, width=4, height=3,
                mime_type='image/webp', sha256=?,
                raw_image_json=?
            WHERE web_post_id=?
            """,
            (
                local_path.as_posix(),
                image_sha256,
                json.dumps({"local_file": {"source": "test"}}),
                post_id,
            ),
        )

        refreshed_post = dict(post)
        refreshed_post["_image_items"] = [
            {"url": refreshed_image_url, "role": "content", "source_key": "image_list"}
        ]
        updated_post_id, inserted = mediacrawler_crawl.upsert_web_post(
            conn,
            refreshed_post,
            project_root=tmp_path,
            media_root=tmp_path / "data/media",
        )
        image = conn.execute(
            """
            SELECT image_url, local_path, width, height, mime_type, sha256, raw_image_json
            FROM web_post_images
            WHERE web_post_id=?
            """,
            (post_id,),
        ).fetchone()

    assert updated_post_id == post_id
    assert inserted is False
    assert image[:6] == (
        refreshed_image_url,
        local_path.as_posix(),
        4,
        3,
        "image/webp",
        image_sha256,
    )
    assert '"local_file"' in image[6]
