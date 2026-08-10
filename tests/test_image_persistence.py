from __future__ import annotations

from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import sys

from PIL import Image
import pytest

from trippostcollect.artifacts.image_candidates import content_image_candidates
from trippostcollect.artifacts.image_materialization import (
    MaterializedImage,
    PIL_FORMAT_MIME,
    promote_validated_image,
    write_staging_image,
)
from trippostcollect.db.bootstrap import bootstrap_connection


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mediacrawler_crawl import (  # noqa: E402
    ImagePersistenceError,
    row_for_record,
    upsert_web_post,
)


def png_bytes(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (5, 4), color=color).save(output, format="PNG")
    return output.getvalue()


def test_pillow_mpo_decoder_is_treated_as_jpeg_bytes() -> None:
    assert PIL_FORMAT_MIME["MPO"] == "image/jpeg"


def xhs_record(urls: list[str], *, title: str = "title") -> dict:
    return {
        "note_id": "post-1",
        "title": title,
        "desc": "body",
        "image_list": ",".join(urls),
        "avatar_url": "https://avatar.test/user.jpg",
    }


def douyin_record(url: str, uri: str, *, title: str = "title") -> dict:
    return {
        "aweme_id": "post-1",
        "title": title,
        "desc": "body",
        "note_download_url": url,
        "image_assets": [{"uri": uri, "url": url}],
    }


def materialize_record(
    platform_key: str,
    record: dict,
    project_root: Path,
) -> list[MaterializedImage]:
    staging_root = project_root / "temp/staging"
    media_root = project_root / "data/media"
    result: list[MaterializedImage] = []
    for candidate in content_image_candidates(platform_key, record):
        payload = png_bytes((candidate.source_index + 1, 20, 30))
        staged = write_staging_image(
            [payload],
            staging_root=staging_root,
            relative_stem=f"{platform_key}/images/{candidate.platform_post_id}/{candidate.source_index:03d}",
        )
        promoted = promote_validated_image(
            staged,
            candidate,
            staging_root=staging_root,
            media_root=media_root,
            project_root=project_root,
        )
        result.append(
            replace(
                promoted,
                manifest_source_index=candidate.source_index,
                manifest_path=f"temp/staging/{platform_key}/image_manifest.jsonl",
                manifest_line=candidate.source_index + 1,
            )
        )
    return result


def persistence_row(
    platform_key: str,
    record: dict,
    materialized: list[MaterializedImage] | None,
) -> dict:
    return row_for_record(
        platform_key,
        record,
        artifact_dir="temp/test-artifacts",
        captured_at="2026-08-07T00:00:00+00:00",
        keyword="青岛旅游",
        materialized_images=materialized,
    )


def strict_upsert(
    conn: sqlite3.Connection,
    row: dict,
    project_root: Path,
) -> tuple[int, bool]:
    return upsert_web_post(
        conn,
        row,
        project_root=project_root,
        media_root=project_root / "data/media",
        require_local_images=True,
    )


def test_new_post_persists_complete_content_relationships_and_url_only_avatar(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    record = xhs_record(
        [
            "https://sns.test/notes_pre_post/asset-a",
            "https://sns.test/notes_pre_post/asset-b",
        ]
    )
    materialized = materialize_record("xhs", record, project_root)
    row = persistence_row("xhs", record, materialized)

    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, sync_jobs=False)
        post_id, inserted = strict_upsert(conn, row, project_root)
        assert inserted is True
        _, inserted = strict_upsert(conn, persistence_row("xhs", record, materialized), project_root)
        assert inserted is False
        post_images_count = conn.execute(
            "SELECT post_images_count FROM web_posts WHERE id=?",
            (post_id,),
        ).fetchone()[0]
        images = conn.execute(
            """
            SELECT image_index, image_role, local_path, width, height, mime_type, sha256, raw_image_json
            FROM web_post_images WHERE web_post_id=? ORDER BY image_role, image_index
            """,
            (post_id,),
        ).fetchall()
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()

    content = [row for row in images if row[1] == "content"]
    avatar = [row for row in images if row[1] == "author_avatar"]
    assert post_images_count == len(content) == sum(bool(row[2]) for row in content) == 2
    assert [row[0] for row in content] == [0, 1]
    assert all(row[3:7] == (5, 4, "image/png", materialized[row[0]].sha256) for row in content)
    assert len(avatar) == 1 and avatar[0][0] == 0 and avatar[0][2] is None
    raw = json.loads(content[0][7])
    assert raw["source_asset_key"].startswith("xhs:path:")
    assert raw["source_index"] == 0
    assert raw["local_file"] == {
        "manifest_line": 1,
        "manifest_path": "temp/staging/xhs/image_manifest.jsonl",
        "manifest_source_index": 0,
        "size_bytes": materialized[0].size_bytes,
        "source": "formal_image_materialization_v1",
        "source_url": materialized[0].source_url,
    }
    assert foreign_keys == []


def test_signature_change_with_same_asset_key_preserves_verified_local_file(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    original = douyin_record("https://dy.test/images/a.jpeg?signature=one", "uri-a")
    materialized = materialize_record("douyin", original, project_root)

    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, sync_jobs=False)
        post_id, _ = strict_upsert(conn, persistence_row("douyin", original, materialized), project_root)
        original_path = conn.execute(
            "SELECT local_path FROM web_post_images WHERE web_post_id=?",
            (post_id,),
        ).fetchone()[0]

        refreshed = douyin_record("https://other-cdn.test/images/changed.jpeg?signature=two", "uri-a")
        strict_upsert(conn, persistence_row("douyin", refreshed, None), project_root)
        image_url, local_path, raw_json = conn.execute(
            "SELECT image_url, local_path, raw_image_json FROM web_post_images WHERE web_post_id=?",
            (post_id,),
        ).fetchone()

    assert image_url == refreshed["note_download_url"]
    assert local_path == original_path
    assert json.loads(raw_json)["source_asset_key"] == "douyin:uri:uri-a"


def test_asset_key_change_or_missing_file_does_not_partially_update_existing_post(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    original = douyin_record("https://dy.test/images/a.jpeg?signature=one", "uri-a", title="before")
    materialized = materialize_record("douyin", original, project_root)

    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, sync_jobs=False)
        post_id, _ = strict_upsert(conn, persistence_row("douyin", original, materialized), project_root)
        before = conn.execute(
            """
            SELECT p.title, i.image_url, i.local_path, i.sha256
            FROM web_posts p JOIN web_post_images i ON i.web_post_id=p.id
            WHERE p.id=?
            """,
            (post_id,),
        ).fetchone()

        changed = douyin_record("https://dy.test/images/a.jpeg?signature=two", "uri-b", title="after")
        with pytest.raises(ImagePersistenceError, match="lacks verified local file"):
            strict_upsert(conn, persistence_row("douyin", changed, None), project_root)
        assert conn.execute(
            """
            SELECT p.title, i.image_url, i.local_path, i.sha256
            FROM web_posts p JOIN web_post_images i ON i.web_post_id=p.id
            WHERE p.id=?
            """,
            (post_id,),
        ).fetchone() == before

        (project_root / materialized[0].local_path).unlink()
        refreshed = douyin_record("https://dy.test/images/a.jpeg?signature=three", "uri-a", title="missing")
        with pytest.raises(ImagePersistenceError, match="lacks verified local file"):
            strict_upsert(conn, persistence_row("douyin", refreshed, None), project_root)
        assert conn.execute("SELECT title FROM web_posts WHERE id=?", (post_id,)).fetchone()[0] == "before"


def test_reordering_preserves_each_asset_and_reindexes_content(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    first_url = "https://sns.test/notes_pre_post/asset-a"
    second_url = "https://sns.test/notes_pre_post/asset-b"
    original = xhs_record([first_url, second_url])
    materialized = materialize_record("xhs", original, project_root)

    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, sync_jobs=False)
        post_id, _ = strict_upsert(conn, persistence_row("xhs", original, materialized), project_root)
        old_paths = dict(
            conn.execute(
                "SELECT image_url, local_path FROM web_post_images WHERE web_post_id=? AND image_role='content'",
                (post_id,),
            ).fetchall()
        )
        reordered = xhs_record([second_url, first_url])
        strict_upsert(conn, persistence_row("xhs", reordered, None), project_root)
        rows = conn.execute(
            """
            SELECT image_index, image_url, local_path FROM web_post_images
            WHERE web_post_id=? AND image_role='content' ORDER BY image_index
            """,
            (post_id,),
        ).fetchall()

    assert rows == [(0, second_url, old_paths[second_url]), (1, first_url, old_paths[first_url])]


def test_image_deletion_and_addition_replace_the_whole_authoritative_sequence(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    first_url = "https://sns.test/notes_pre_post/asset-a"
    second_url = "https://sns.test/notes_pre_post/asset-b"
    third_url = "https://sns.test/notes_pre_post/asset-c"
    original = xhs_record([first_url, second_url])
    materialized = materialize_record("xhs", original, project_root)

    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, sync_jobs=False)
        post_id, _ = strict_upsert(conn, persistence_row("xhs", original, materialized), project_root)
        deleted = xhs_record([second_url])
        strict_upsert(conn, persistence_row("xhs", deleted, None), project_root)
        assert conn.execute(
            """
            SELECT image_index, image_url FROM web_post_images
            WHERE web_post_id=? AND image_role='content'
            """,
            (post_id,),
        ).fetchall() == [(0, second_url)]

        added = xhs_record([second_url, third_url])
        replacement_files = materialize_record("xhs", added, project_root)
        strict_upsert(conn, persistence_row("xhs", added, replacement_files), project_root)
        rows = conn.execute(
            """
            SELECT image_index, image_url, local_path FROM web_post_images
            WHERE web_post_id=? AND image_role='content' ORDER BY image_index
            """,
            (post_id,),
        ).fetchall()

    assert [(row[0], row[1]) for row in rows] == [(0, second_url), (1, third_url)]
    assert all(row[2] for row in rows)


def test_non_strict_asset_change_clears_old_local_metadata(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    original = douyin_record("https://dy.test/images/a.jpeg?signature=one", "uri-a")
    materialized = materialize_record("douyin", original, project_root)

    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, sync_jobs=False)
        post_id, _ = strict_upsert(conn, persistence_row("douyin", original, materialized), project_root)
        changed = douyin_record("https://dy.test/images/a.jpeg?signature=two", "uri-b")
        upsert_web_post(
            conn,
            persistence_row("douyin", changed, None),
            project_root=project_root,
            media_root=project_root / "data/media",
            require_local_images=False,
        )
        image_url, local_path, sha_value = conn.execute(
            "SELECT image_url, local_path, sha256 FROM web_post_images WHERE web_post_id=?",
            (post_id,),
        ).fetchone()

    assert image_url == changed["note_download_url"]
    assert local_path is None
    assert sha_value is None


def test_one_missing_materialized_file_prevents_new_post_and_sql_failure_rolls_back(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    record = xhs_record(
        [
            "https://sns.test/notes_pre_post/asset-a",
            "https://sns.test/notes_pre_post/asset-b",
        ]
    )
    materialized = materialize_record("xhs", record, project_root)
    missing_row = persistence_row("xhs", record, materialized)
    (project_root / materialized[1].local_path).unlink()

    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, sync_jobs=False)
        with pytest.raises(ImagePersistenceError, match="verification failed"):
            strict_upsert(conn, missing_row, project_root)
        assert conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM web_post_images").fetchone()[0] == 0

    materialized = materialize_record("xhs", record, project_root)
    row = persistence_row("xhs", record, materialized)
    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, sync_jobs=False)
        conn.execute(
            """
            CREATE TRIGGER fail_second_image
            BEFORE INSERT ON web_post_images
            WHEN NEW.image_role='content' AND NEW.image_index=1
            BEGIN SELECT RAISE(ABORT, 'simulated image insert failure'); END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="simulated image insert failure"):
            strict_upsert(conn, row, project_root)
        assert conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM web_post_images").fetchone()[0] == 0


def test_materialized_identity_mismatch_is_rejected_before_row_creation(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    record = xhs_record(["https://sns.test/notes_pre_post/asset-a"])
    materialized = materialize_record("xhs", record, project_root)

    with pytest.raises(ImagePersistenceError, match="identity mismatch"):
        persistence_row(
            "xhs",
            record,
            [replace(materialized[0], source_asset_key="xhs:path:/notes_pre_post/other")],
        )
