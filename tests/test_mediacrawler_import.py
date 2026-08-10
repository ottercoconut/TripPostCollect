from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import io
import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path

from PIL import Image
import pytest

from trippostcollect.artifacts.image_candidates import content_image_candidates
from trippostcollect.artifacts.image_manifest import ImageManifestEntry, write_manifest_atomic
from trippostcollect.artifacts.image_materialization import write_staging_image
from trippostcollect.db.bootstrap import bootstrap_connection


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

mediacrawler_crawl = import_module("mediacrawler_crawl")


def png_bytes(color: tuple[int, int, int] = (3, 4, 5)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (6, 4), color=color).save(output, format="PNG")
    return output.getvalue()


def five_platform_image_records() -> dict[str, dict]:
    return {
        "bilibili": {
            "content_id": "bili-1",
            "content_text": "body",
            "content_images_detail_status": "detail_observed",
            "image_urls": ["https://i0.hdslb.com/bfs/article/bili-asset.png"],
        },
        "weibo": {
            "note_id": "weibo-1",
            "content": "body",
            "image_list_source": "mblog.pics",
            "image_list": ["https://wx1.sinaimg.cn/large/weibo-asset.jpg"],
            "image_assets": [
                {
                    "pid": "weibo-pid-1",
                    "url": "https://wx1.sinaimg.cn/large/weibo-asset.jpg",
                }
            ],
        },
        "xhs": {
            "note_id": "xhs-1",
            "desc": "body",
            "image_list": [
                {"url_default": "https://sns-img.test/notes_pre_post/xhs-asset"}
            ],
        },
        "douyin": {
            "aweme_id": "douyin-1",
            "desc": "body",
            "note_download_url": "https://p3-dy.test/image/douyin-asset?signature=one",
            "image_assets": [
                {
                    "uri": "douyin-uri-1",
                    "url": "https://p3-dy.test/image/douyin-asset?signature=one",
                }
            ],
        },
        "zhihu": {
            "content_id": "zhihu-1",
            "content": "body",
            "image_list": ["https://pic1.zhimg.com/v2-zhihu-asset_r.jpg"],
        },
    }


def staged_selection(
    project_root: Path,
    platform_key: str,
    record: dict,
) -> tuple[dict, Path, Path]:
    data_root = project_root / "temp" / "batch" / platform_key / "data"
    manifest_path = data_root / platform_key / "image_manifest.jsonl"
    entries: list[ImageManifestEntry] = []
    for candidate in content_image_candidates(platform_key, record):
        relative_stem = (
            f"images/{candidate.platform_post_id}/{candidate.source_index:03d}"
            if platform_key == "bilibili"
            else (
                f"{platform_key}/images/{candidate.platform_post_id}/"
                f"{candidate.source_index:03d}"
            )
        )
        staging_root = manifest_path.parent if platform_key == "bilibili" else data_root
        staged = write_staging_image(
            [png_bytes((candidate.source_index + 1, 4, 5))],
            staging_root=staging_root,
            relative_stem=relative_stem,
        )
        entries.append(
            ImageManifestEntry(
                schema_version=1,
                platform_key=candidate.platform_key,
                platform_post_id=candidate.platform_post_id,
                image_role=candidate.image_role,
                source_index=candidate.source_index,
                source_key=candidate.source_key,
                source_asset_key=candidate.source_asset_key,
                source_url=candidate.source_url,
                fetch_status="downloaded",
                attempts=1,
                http_status=200,
                staging_path=staged.path.relative_to(staging_root).as_posix(),
                size_bytes=staged.size_bytes,
                mime_type=staged.mime_type,
                width=staged.width,
                height=staged.height,
                sha256=staged.sha256,
                error_code=None,
            )
        )
    write_manifest_atomic(manifest_path, entries)
    identity = mediacrawler_crawl.formal_record_identity(platform_key, record)
    return (
        {
            "platform": platform_key,
            "record": record,
            "identity": identity,
            "source_path": str(data_root / "jsonl" / "contents.jsonl"),
            "line_number": 1,
            "is_new": True,
            "manifest_paths": [str(manifest_path)],
        },
        manifest_path,
        data_root,
    )


def test_content_import_bootstrap_preserves_parent_runner_jobs() -> None:
    one_off_config = {
        "jobs": [
            {
                "job_key": "once_first",
                "site_key": "weibo",
                "target_url": "https://example.invalid/first",
                "job_kind": "mediacrawler_search",
                "enabled": True,
                "params": {"keyword": "青岛旅游"},
            },
            {
                "job_key": "once_second",
                "site_key": "douyin",
                "target_url": "https://example.invalid/second",
                "job_kind": "mediacrawler_search",
                "enabled": True,
                "params": {"keyword": "崂山攻略"},
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


def test_five_platform_manifests_share_root_verification_promotion_and_import(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    media_root = project_root / "temp" / "formal-media"
    selected = [
        staged_selection(project_root, platform_key, record)[0]
        for platform_key, record in five_platform_image_records().items()
    ]

    report, materialized, complete_identities = (
        mediacrawler_crawl.materialize_formal_record_images(
            selected,
            project_root=project_root,
            media_root=media_root,
            promote=True,
        )
    )

    assert report["complete"] is True
    assert report["candidate_posts"] == report["complete_posts"] == 5
    assert report["expected_images"] == report["validated_images"] == 5
    assert report["promoted_images"] == 5
    assert report["reused_images"] == 0
    assert complete_identities == set(materialized)
    for item in selected:
        item["materialized_images"] = materialized[item["identity"]]
        local = item["materialized_images"][0]
        assert local.local_path.startswith(f"temp/formal-media/{item['platform']}/")
        assert (project_root / local.local_path).is_file()

    db_path = project_root / "temp" / "formal.sqlite"
    summary = {
        "captured_at": "2026-08-07T00:00:00+00:00",
        "keyword": "青岛旅游",
        "batch_dir": str(project_root / "temp" / "batch"),
    }
    first = mediacrawler_crawl.import_valid_records(
        summary,
        selected,
        db_path,
        project_root=project_root,
        media_root=media_root,
        require_local_images=True,
    )
    second = mediacrawler_crawl.import_valid_records(
        summary,
        selected,
        db_path,
        project_root=project_root,
        media_root=media_root,
        require_local_images=True,
    )
    second_report, _, _ = mediacrawler_crawl.materialize_formal_record_images(
        selected,
        project_root=project_root,
        media_root=media_root,
        promote=True,
    )

    with sqlite3.connect(db_path) as conn:
        counts = conn.execute(
            "SELECT (SELECT COUNT(*) FROM web_posts), "
            "(SELECT COUNT(*) FROM web_post_images WHERE image_role='content'), "
            "(SELECT COUNT(*) FROM web_post_images WHERE local_path IS NOT NULL)"
        ).fetchone()
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()

    assert first["inserted_rows"] == 5
    assert second["inserted_rows"] == 0
    assert second["updated_rows"] == 5
    assert second_report["promoted_images"] == 0
    assert second_report["reused_images"] == 5
    assert counts == (5, 5, 5)
    assert quick_check == "ok"
    assert foreign_keys == []
    assert len(list(media_root.rglob("*.png"))) == 5


def test_no_import_image_verification_never_promotes_or_creates_sqlite(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    item, _, _ = staged_selection(
        project_root,
        "xhs",
        five_platform_image_records()["xhs"],
    )
    media_root = project_root / "temp" / "diagnostic-media"
    db_path = project_root / "temp" / "diagnostic.sqlite"

    report, materialized, complete_identities = (
        mediacrawler_crawl.materialize_formal_record_images(
            [item],
            project_root=project_root,
            media_root=media_root,
            promote=False,
        )
    )

    assert report["complete"] is True
    assert report["promotion_required"] is False
    assert report["validated_images"] == 1
    assert report["promoted_images"] == report["reused_images"] == 0
    assert materialized == {}
    assert complete_identities == {item["identity"]}
    assert not media_root.exists()
    assert not db_path.exists()


@pytest.mark.parametrize(
    "failure_case,expected_code",
    [
        ("missing_manifest", "missing_image_manifest"),
        ("missing_file", "image_file_missing"),
        ("count_mismatch", "image_manifest_count_mismatch"),
        ("identity_mismatch", "image_manifest_identity_mismatch"),
        ("sha_mismatch", "image_hash_mismatch"),
    ],
)
def test_manifest_or_staging_failure_blocks_whole_post(
    tmp_path: Path,
    failure_case: str,
    expected_code: str,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    item, manifest_path, data_root = staged_selection(
        project_root,
        "xhs",
        five_platform_image_records()["xhs"],
    )
    entries = list(
        mediacrawler_crawl.parse_manifest(manifest_path.read_bytes())
    )
    staged_path = data_root / str(entries[0].staging_path)
    if failure_case == "missing_manifest":
        manifest_path.unlink()
    elif failure_case == "missing_file":
        staged_path.unlink()
    elif failure_case == "count_mismatch":
        write_manifest_atomic(manifest_path, [])
    elif failure_case == "identity_mismatch":
        write_manifest_atomic(
            manifest_path,
            [replace(entries[0], source_url="https://sns-img.test/notes_pre_post/other")],
        )
    elif failure_case == "sha_mismatch":
        staged_path.write_bytes(png_bytes((90, 80, 70)))

    media_root = project_root / "temp" / "failure-media"
    report, materialized, complete_identities = (
        mediacrawler_crawl.materialize_formal_record_images(
            [item],
            project_root=project_root,
            media_root=media_root,
            promote=True,
        )
    )

    assert report["complete"] is False
    assert report["failures"][0]["code"] == expected_code
    assert materialized == {}
    assert complete_identities == set()
    assert not media_root.exists()


def test_media_root_override_is_limited_to_project_temp(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    formal_root = project_root / "data" / "media"
    diagnostic_root = project_root / "temp" / "diagnostic-media"

    assert mediacrawler_crawl.resolve_media_root(
        formal_root,
        project_root=project_root,
        default_media_root=formal_root,
    ) == formal_root
    assert mediacrawler_crawl.resolve_media_root(
        diagnostic_root,
        project_root=project_root,
        default_media_root=formal_root,
    ) == diagnostic_root
    with pytest.raises(
        mediacrawler_crawl.ImagePersistenceError,
        match="below project temp",
    ):
        mediacrawler_crawl.resolve_media_root(
            project_root / "outputs" / "unsafe-override",
            project_root=project_root,
            default_media_root=formal_root,
        )


def test_formal_cli_requires_project_image_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mediacrawler_crawl.py",
            "--platforms",
            "bilibili",
            "--candidate-hard-limit",
            "1",
            "--target-new-posts",
            "1",
        ],
    )

    with pytest.raises(SystemExit, match="--download-images"):
        mediacrawler_crawl.main()
