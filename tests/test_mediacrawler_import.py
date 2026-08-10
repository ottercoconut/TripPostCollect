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
from trippostcollect.artifacts.image_completion import verify_image_artifacts
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
    *,
    image_payloads: list[bytes] | None = None,
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
        payload = (
            image_payloads[candidate.source_index]
            if image_payloads is not None
            else png_bytes((candidate.source_index + 1, 4, 5))
        )
        staged = write_staging_image(
            [payload],
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
    artifact_evidence = verify_image_artifacts(
        {"image_materialization": report},
        project_root=project_root,
        expect_promotion=True,
    )

    assert report["complete"] is True
    assert report["candidate_posts"] == report["complete_posts"] == 5
    assert report["expected_images"] == report["validated_images"] == 5
    assert artifact_evidence["ok"] is True
    assert artifact_evidence["unique_images"] == 5
    assert artifact_evidence["sha256_duplicate_images"] == 0
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


def test_same_post_images_are_deduplicated_by_verified_sha256_before_import(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    media_root = project_root / "temp" / "formal-media"
    record = {
        "content_id": "zhihu-sha-dedupe",
        "content": "body",
        "image_list": [
            "https://pic1.zhimg.com/v2-asset-a_r.jpg",
            "https://pic1.zhimg.com/v2-asset-b_r.jpg",
            "https://pic1.zhimg.com/v2-asset-c_r.jpg",
        ],
    }
    duplicate_payload = png_bytes((10, 20, 30))
    item, _, _ = staged_selection(
        project_root,
        "zhihu",
        record,
        image_payloads=[duplicate_payload, duplicate_payload, png_bytes((40, 50, 60))],
    )

    report, materialized, complete_identities = (
        mediacrawler_crawl.materialize_formal_record_images(
            [item],
            project_root=project_root,
            media_root=media_root,
            promote=True,
        )
    )
    artifact_evidence = verify_image_artifacts(
        {"image_materialization": report},
        project_root=project_root,
        expect_promotion=True,
    )
    retained = materialized[item["identity"]]
    item["materialized_images"] = retained
    summary = {
        "captured_at": "2026-08-10T00:00:00+00:00",
        "keyword": "青岛旅游",
        "batch_dir": str(project_root / "temp" / "batch"),
    }
    db_path = project_root / "temp" / "formal.sqlite"
    result = mediacrawler_crawl.import_valid_records(
        summary,
        [item],
        db_path,
        project_root=project_root,
        media_root=media_root,
        require_local_images=True,
    )

    with sqlite3.connect(db_path) as conn:
        post_images_count = conn.execute(
            "SELECT post_images_count FROM web_posts WHERE platform_post_id=?",
            ("zhihu-sha-dedupe",),
        ).fetchone()[0]
        images = conn.execute(
            """
            SELECT image_index, image_url, sha256, raw_image_json
            FROM web_post_images
            WHERE image_role='content'
            ORDER BY image_index
            """
        ).fetchall()

    assert report["complete"] is True
    assert report["expected_images"] == report["validated_images"] == 3
    assert report["unique_images"] == report["promoted_images"] == 2
    assert report["sha256_duplicate_images"] == 1
    assert artifact_evidence["ok"] is True
    assert artifact_evidence["expected_images"] == 3
    assert artifact_evidence["unique_images"] == 2
    assert artifact_evidence["sha256_duplicate_images"] == 1
    assert complete_identities == {item["identity"]}
    assert [image.source_index for image in retained] == [0, 1]
    assert [image.manifest_source_index for image in retained] == [0, 2]
    assert retained[0].sha256_duplicate_sources[0].source_index == 1
    assert result["inserted_rows"] == 1
    assert post_images_count == 2
    assert [row[0] for row in images] == [0, 1]
    assert len({row[2] for row in images}) == 2
    assert "sha256_duplicate_sources" in images[0][3]
    assert len(list(media_root.rglob("*.png"))) == 2


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


def test_failed_multi_image_promotion_rolls_back_new_long_term_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    record = {
        "content_id": "bili-promotion-rollback",
        "content_text": "body",
        "content_images_detail_status": "detail_observed",
        "image_urls": [
            "https://i0.hdslb.com/bfs/article/first.png",
            "https://i0.hdslb.com/bfs/article/second.png",
        ],
    }
    item, _, _ = staged_selection(project_root, "bilibili", record)
    media_root = project_root / "temp" / "formal-media"
    original = mediacrawler_crawl.promote_validated_image
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise mediacrawler_crawl.ImageMaterializationError(
                "image_promotion_conflict", "injected second-image failure"
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(
        mediacrawler_crawl,
        "promote_validated_image",
        fail_second,
    )

    report, materialized, complete_identities = (
        mediacrawler_crawl.materialize_formal_record_images(
            [item],
            project_root=project_root,
            media_root=media_root,
            promote=True,
        )
    )

    assert report["complete"] is False
    assert report["failures"][0]["code"] == "image_promotion_conflict"
    assert report["rolled_back_images"] == 1
    assert report["promoted_images"] == 0
    assert materialized == {}
    assert complete_identities == set()
    assert not list(media_root.rglob("*.*"))


def test_promotion_interrupt_rolls_back_prior_new_long_term_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    record = {
        "content_id": "bili-promotion-interrupt",
        "content_text": "body",
        "content_images_detail_status": "detail_observed",
        "image_urls": [
            "https://i0.hdslb.com/bfs/article/first.png",
            "https://i0.hdslb.com/bfs/article/second.png",
        ],
    }
    item, _, _ = staged_selection(project_root, "bilibili", record)
    media_root = project_root / "temp" / "formal-media"
    original = mediacrawler_crawl.promote_validated_image
    calls = 0

    def interrupt_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("injected promotion interrupt")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        mediacrawler_crawl,
        "promote_validated_image",
        interrupt_second,
    )

    with pytest.raises(KeyboardInterrupt, match="injected promotion interrupt"):
        mediacrawler_crawl.materialize_formal_record_images(
            [item],
            project_root=project_root,
            media_root=media_root,
            promote=True,
        )

    assert not list(media_root.rglob("*.*"))


def test_sqlite_import_failure_rolls_back_newly_promoted_files(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    item, _, _ = staged_selection(
        project_root,
        "xhs",
        five_platform_image_records()["xhs"],
    )
    media_root = project_root / "temp" / "formal-media"
    report, materialized, _ = mediacrawler_crawl.materialize_formal_record_images(
        [item],
        project_root=project_root,
        media_root=media_root,
        promote=True,
    )
    assert len(list(media_root.rglob("*.png"))) == 1
    item["materialized_images"] = materialized[item["identity"]]
    monkeypatch.setattr(
        mediacrawler_crawl,
        "upsert_web_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.Error("injected")),
    )

    result = mediacrawler_crawl.import_valid_records_with_media_rollback(
        {"keyword": "青岛旅游"},
        [item],
        project_root / "temp" / "formal.sqlite",
        materialized_images_by_identity=materialized,
        image_materialization=report,
        project_root=project_root,
        media_root=media_root,
    )

    assert result["reason"] == "sqlite_import_failed"
    assert result["rolled_back_images"] == 1
    assert report["rolled_back_images"] == 1
    assert report["promoted_images"] == 0
    assert not list(media_root.rglob("*.*"))
    assert "reason=sqlite_import_failed removed_new_files=1" in capsys.readouterr().err


def test_postcommit_interrupt_preserves_sqlite_referenced_media(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    item, _, _ = staged_selection(
        project_root,
        "xhs",
        five_platform_image_records()["xhs"],
    )
    media_root = project_root / "temp" / "formal-media"
    report, materialized, _ = mediacrawler_crawl.materialize_formal_record_images(
        [item],
        project_root=project_root,
        media_root=media_root,
        promote=True,
    )
    item["materialized_images"] = materialized[item["identity"]]
    db_path = project_root / "temp" / "formal.sqlite"

    def commit_then_interrupt(conn: sqlite3.Connection) -> None:
        conn.commit()
        raise KeyboardInterrupt("after commit")

    monkeypatch.setattr(
        mediacrawler_crawl,
        "commit_formal_import",
        commit_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt, match="after commit"):
        mediacrawler_crawl.import_valid_records_with_media_rollback(
            {"keyword": "青岛旅游"},
            [item],
            db_path,
            materialized_images_by_identity=materialized,
            image_materialization=report,
            project_root=project_root,
            media_root=media_root,
        )

    with sqlite3.connect(db_path) as conn:
        referenced = conn.execute(
            """
            SELECT COUNT(*)
            FROM web_post_images AS image
            JOIN web_posts AS post ON post.id=image.web_post_id
            WHERE post.platform_post_id='xhs-1' AND image.local_path IS NOT NULL
            """
        ).fetchone()[0]
    assert referenced == 1
    assert report["rolled_back_images"] == 0
    assert len(list(media_root.rglob("*.png"))) == 1


def test_interrupt_before_batch_commit_rolls_back_rows_and_media(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    item, _, _ = staged_selection(
        project_root,
        "xhs",
        five_platform_image_records()["xhs"],
    )
    media_root = project_root / "temp" / "formal-media"
    report, materialized, _ = mediacrawler_crawl.materialize_formal_record_images(
        [item],
        project_root=project_root,
        media_root=media_root,
        promote=True,
    )
    item["materialized_images"] = materialized[item["identity"]]
    db_path = project_root / "temp" / "formal.sqlite"

    def interrupt_before_commit(conn: sqlite3.Connection) -> None:
        assert conn.in_transaction is True
        raise KeyboardInterrupt("before commit")

    monkeypatch.setattr(
        mediacrawler_crawl,
        "commit_formal_import",
        interrupt_before_commit,
    )
    result = mediacrawler_crawl.import_valid_records_with_media_rollback(
        {"keyword": "青岛旅游"},
        [item],
        db_path,
        materialized_images_by_identity=materialized,
        image_materialization=report,
        project_root=project_root,
        media_root=media_root,
    )

    with sqlite3.connect(db_path) as conn:
        posts = conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0]
        images = conn.execute("SELECT COUNT(*) FROM web_post_images").fetchone()[0]
    assert result["reason"] == "sqlite_import_failed"
    assert result["inserted_rows"] == 0
    assert posts == images == 0
    assert report["rolled_back_images"] == 1
    assert not list(media_root.rglob("*.png"))


def test_second_post_failure_rolls_back_entire_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    media_root = project_root / "temp" / "formal-media"
    selected: list[dict] = []
    all_materialized: dict[str, list] = {}
    reports: list[dict] = []
    for platform_key in ("xhs", "weibo"):
        item, _, _ = staged_selection(
            project_root,
            platform_key,
            five_platform_image_records()[platform_key],
        )
        report, materialized, _ = mediacrawler_crawl.materialize_formal_record_images(
            [item],
            project_root=project_root,
            media_root=media_root,
            promote=True,
        )
        item["materialized_images"] = materialized[item["identity"]]
        selected.append(item)
        all_materialized.update(materialized)
        reports.append(report)
    image_materialization = {
        "rolled_back_images": 0,
        "promoted_images": sum(report["promoted_images"] for report in reports),
    }
    real_upsert = mediacrawler_crawl.upsert_web_post
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.Error("second post failed")
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(mediacrawler_crawl, "upsert_web_post", fail_second)
    db_path = project_root / "temp" / "formal.sqlite"
    result = mediacrawler_crawl.import_valid_records_with_media_rollback(
        {"keyword": "青岛旅游"},
        selected,
        db_path,
        materialized_images_by_identity=all_materialized,
        image_materialization=image_materialization,
        project_root=project_root,
        media_root=media_root,
    )

    with sqlite3.connect(db_path) as conn:
        posts = conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0]
        images = conn.execute("SELECT COUNT(*) FROM web_post_images").fetchone()[0]
    assert result["reason"] == "sqlite_import_failed"
    assert result["processed_rows"] == result["inserted_rows"] == 0
    assert posts == images == 0
    assert result["rolled_back_images"] == 2
    assert not list(media_root.rglob("*.png"))


def test_rollback_preserves_file_referenced_by_another_committed_run(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    item, _, _ = staged_selection(
        project_root,
        "xhs",
        five_platform_image_records()["xhs"],
    )
    media_root = project_root / "temp" / "formal-media"
    report_a, materialized_a, _ = mediacrawler_crawl.materialize_formal_record_images(
        [item],
        project_root=project_root,
        media_root=media_root,
        promote=True,
    )
    report_b, materialized_b, _ = mediacrawler_crawl.materialize_formal_record_images(
        [item],
        project_root=project_root,
        media_root=media_root,
        promote=True,
    )
    assert report_a["promoted_images"] == 1
    assert report_b["reused_images"] == 1
    item_b = {**item, "materialized_images": materialized_b[item["identity"]]}
    db_path = project_root / "temp" / "formal.sqlite"
    imported = mediacrawler_crawl.import_valid_records(
        {"keyword": "青岛旅游"},
        [item_b],
        db_path,
        project_root=project_root,
        media_root=media_root,
        require_local_images=True,
    )

    removed = mediacrawler_crawl.rollback_newly_promoted_images(
        materialized_a,
        project_root=project_root,
        media_root=media_root,
        db_path=db_path,
    )

    assert imported["inserted_rows"] == 1
    assert removed == 0
    assert len(list(media_root.rglob("*.png"))) == 1
    with sqlite3.connect(db_path) as conn:
        local_path = conn.execute(
            "SELECT local_path FROM web_post_images WHERE local_path IS NOT NULL"
        ).fetchone()[0]
    assert (project_root / local_path).is_file()


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


def test_long_term_image_promotion_requires_all_completion_gates() -> None:
    behavior_ok = {"ok": True, "behavior_ok": True, "policy_ok": True}
    image_complete = {"complete": True}
    incomplete = mediacrawler_crawl.apply_formal_completion_gates(
        {"completion_met": False, "new_target_met": False, "stop_reason": "runtime_failed"},
        content_validation={"completion_met": False, "new_target_met": False},
        image_materialization=image_complete,
        behavior_validation=behavior_ok,
        download_images=True,
    )

    assert mediacrawler_crawl.formal_image_promotion_allowed(
        download_images=True,
        no_import=False,
        validation=incomplete,
    ) is False

    complete = mediacrawler_crawl.apply_formal_completion_gates(
        {"completion_met": True, "new_target_met": True, "stop_reason": "target_new_met"},
        content_validation={"completion_met": True, "new_target_met": True},
        image_materialization=image_complete,
        behavior_validation=behavior_ok,
        download_images=True,
    )
    assert mediacrawler_crawl.formal_image_promotion_allowed(
        download_images=True,
        no_import=False,
        validation=complete,
    ) is True
    assert mediacrawler_crawl.formal_image_promotion_allowed(
        download_images=True,
        no_import=True,
        validation=complete,
    ) is False

    failed_child = mediacrawler_crawl.apply_formal_completion_gates(
        {"completion_met": True, "new_target_met": True, "stop_reason": "target_new_met"},
        content_validation={"completion_met": True, "new_target_met": True},
        image_materialization=image_complete,
        behavior_validation=behavior_ok,
        download_images=True,
        child_execution_ok=False,
    )
    assert failed_child["completion_met"] is False
    assert failed_child["new_target_met"] is False
    assert failed_child["stop_reason"] == "runtime_failed"
    assert mediacrawler_crawl.formal_image_promotion_allowed(
        download_images=True,
        no_import=False,
        validation=failed_child,
    ) is False

    runtime_with_lower_priority_failures = (
        mediacrawler_crawl.apply_formal_completion_gates(
            {
                "completion_met": False,
                "new_target_met": True,
                "stop_reason": "runtime_failed",
            },
            content_validation={"completion_met": False, "new_target_met": True},
            image_materialization={"complete": False},
            behavior_validation={"ok": False, "behavior_ok": False, "policy_ok": False},
            download_images=True,
        )
    )
    assert runtime_with_lower_priority_failures["completion_met"] is False
    assert runtime_with_lower_priority_failures["new_target_met"] is False
    assert runtime_with_lower_priority_failures["stop_reason"] == "runtime_failed"
