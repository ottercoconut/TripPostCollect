from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from PIL import Image
import pytest

from scripts.gc_local_images import main as gc_main
from scripts.materialize_local_images import main as materialize_main
from trippostcollect.artifacts.historical_image_materialization import (
    database_integrity,
    projection_inventory,
    protected_database_digests,
    sha256_file,
)
from trippostcollect.artifacts.image_candidates import content_image_candidates
from trippostcollect.artifacts.image_materialization import validate_image_file
from trippostcollect.db.bootstrap import bootstrap_connection


def _write_image(path: Path, color: tuple[int, int, int] = (20, 40, 60)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 3), color).save(path, format="JPEG")


def _insert_post(
    conn: sqlite3.Connection,
    *,
    platform_key: str,
    platform_post_id: str,
    raw: dict[str, object],
    noise_urls: tuple[str, ...] = (),
    local_candidate_index: int | None = None,
    project_root: Path,
) -> int:
    candidates = content_image_candidates(platform_key, raw)
    current_count = len(candidates) + len(noise_urls)
    conn.execute(
        """
        INSERT INTO web_posts (
          platform_key, platform_post_id, source_type, source_url, canonical_url,
          content_text, content_length, post_images_count, raw_sample_json,
          artifact_dir, capture_method, captured_at
        ) VALUES (?, ?, 'mediacrawler_search', ?, ?, '正文', 2, ?, ?, 'fixture', 'import',
                  '2026-08-07T00:00:00+00:00')
        """,
        (
            platform_key,
            platform_post_id,
            f"https://example.invalid/{platform_key}/{platform_post_id}",
            f"https://example.invalid/{platform_key}/{platform_post_id}",
            current_count,
            json.dumps(raw, ensure_ascii=False, sort_keys=True),
        ),
    )
    post_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    for candidate in candidates:
        local: dict[str, object] = {}
        if candidate.source_index == local_candidate_index:
            image_path = project_root / "legacy" / platform_key / f"{platform_post_id}.jpg"
            _write_image(image_path)
            verified = validate_image_file(image_path, allowed_root=project_root)
            local = {
                "local_path": image_path.relative_to(project_root).as_posix(),
                "width": verified.width,
                "height": verified.height,
                "mime_type": verified.mime_type,
                "sha256": verified.sha256,
            }
        conn.execute(
            """
            INSERT INTO web_post_images (
              web_post_id, image_index, image_url, image_role, local_path,
              width, height, mime_type, sha256, raw_image_json
            ) VALUES (?, ?, ?, 'content', ?, ?, ?, ?, ?, '{}')
            """,
            (
                post_id,
                candidate.source_index,
                candidate.source_url,
                local.get("local_path"),
                local.get("width"),
                local.get("height"),
                local.get("mime_type"),
                local.get("sha256"),
            ),
        )
    for offset, noise_url in enumerate(noise_urls, start=len(candidates)):
        conn.execute(
            """
            INSERT INTO web_post_images (
              web_post_id, image_index, image_url, image_role, raw_image_json
            ) VALUES (?, ?, ?, 'content', '{"source_key":"noise"}')
            """,
            (post_id, offset, noise_url),
        )
    avatar_url = str(raw.get("avatar_url") or "")
    if avatar_url:
        conn.execute(
            """
            INSERT INTO web_post_images (
              web_post_id, image_index, image_url, image_role, raw_image_json
            ) VALUES (?, 0, ?, 'author_avatar', '{"source_key":"avatar_url"}')
            """,
            (post_id, avatar_url),
        )
    return post_id


def _fixture_database(project_root: Path, *, two_xhs_posts: bool = False) -> Path:
    db_path = project_root / "data" / "fixture.sqlite"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        bootstrap_connection(conn, sync_jobs=False)
        _insert_post(
            conn,
            platform_key="xhs",
            platform_post_id="xhs-1",
            raw={
                "note_id": "xhs-1",
                "image_list": ["https://sns-webpic-qc.xhscdn.com/notes/xhs-a.jpg"],
                "avatar_url": "https://sns-avatar-qc.xhscdn.com/avatar/xhs-1.jpg",
            },
            noise_urls=("https://www.xiaohongshu.com/user/profile/noise",),
            local_candidate_index=0,
            project_root=project_root,
        )
        if two_xhs_posts:
            _insert_post(
                conn,
                platform_key="xhs",
                platform_post_id="xhs-2",
                raw={
                    "note_id": "xhs-2",
                    "image_list": ["https://sns-webpic-qc.xhscdn.com/notes/xhs-b.jpg"],
                },
                project_root=project_root,
            )
        _insert_post(
            conn,
            platform_key="bilibili",
            platform_post_id="101",
            raw={
                "content_id": "101",
                "content_images_detail_status": "detail_observed",
                "image_urls": ["https://i0.hdslb.com/bfs/article/bili-a.jpg"],
            },
            project_root=project_root,
        )
        _insert_post(
            conn,
            platform_key="weibo",
            platform_post_id="wb-1",
            raw={
                "note_id": "wb-1",
                "image_list_source": "mblog.pics",
                "image_list": [{"url": "https://wx1.sinaimg.cn/large/wb-a.jpg", "pid": "pid-a"}],
            },
            project_root=project_root,
        )
        _insert_post(
            conn,
            platform_key="zhihu",
            platform_post_id="zh-1",
            raw={
                "content_id": "zh-1",
                "image_list": ["https://pic1.zhimg.com/v2-zh-a_r.jpg"],
            },
            noise_urls=("https://www.zhihu.com/people/author",),
            project_root=project_root,
        )
        _insert_post(
            conn,
            platform_key="douyin",
            platform_post_id="dy-1",
            raw={
                "aweme_id": "dy-1",
                "note_download_url": ["https://p3-sign.douyinpic.com/tos-cn-i/dy-a.jpeg"],
            },
            noise_urls=("https://p3-sign.douyinpic.com/tos-cn-i/cover.jpeg",),
            project_root=project_root,
        )
        conn.commit()
    return db_path


def _campaign(db_path: Path, campaign_path: Path) -> dict[str, object]:
    with sqlite3.connect(db_path) as conn:
        projection = projection_inventory(conn)
        protected = protected_database_digests(conn)
    payload: dict[str, object] = {
        "schema_version": 1,
        "campaign_id": "test-historical-images",
        "status": "input_frozen",
        "input": {"database_sha256": sha256_file(db_path)},
        "projection": {
            "platforms": {
                platform: {
                    "posts": row["posts"],
                    "current_content_rows": row["current_content_rows"],
                    "authoritative_images": row["authoritative_images"],
                    "misclassified_rows": row["misclassified_rows"],
                }
                for platform, row in projection.items()
            }
        },
        "invariants": protected,
    }
    campaign_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _materialize_args(
    *,
    project_root: Path,
    db_path: Path,
    campaign_path: Path,
    report_path: Path,
) -> list[str]:
    return [
        "--db",
        str(db_path),
        "--campaign",
        str(campaign_path),
        "--project-root",
        str(project_root),
        "--media-root",
        str(project_root / "data" / "media"),
        "--report",
        str(report_path),
        "--platform",
        "all",
        "--batch-size",
        "0",
    ]


def test_dry_run_is_read_only_and_apply_rebuilds_a_verified_backup_copy(tmp_path: Path) -> None:
    db_path = _fixture_database(tmp_path)
    campaign_path = tmp_path / "campaign.json"
    _campaign(db_path, campaign_path)
    dry_report = tmp_path / "dry-report.json"
    db_sha_before = sha256_file(db_path)
    legacy_file = tmp_path / "legacy" / "xhs" / "xhs-1.jpg"
    image_sha_before = hashlib.sha256(legacy_file.read_bytes()).hexdigest()

    assert materialize_main(
        _materialize_args(
            project_root=tmp_path,
            db_path=db_path,
            campaign_path=campaign_path,
            report_path=dry_report,
        )
    ) == 0
    dry_payload = json.loads(dry_report.read_text(encoding="utf-8"))
    assert dry_payload["mode"] == "dry-run"
    assert dry_payload["backup"] is None
    assert sha256_file(db_path) == db_sha_before
    assert hashlib.sha256(legacy_file.read_bytes()).hexdigest() == image_sha_before

    with sqlite3.connect(db_path) as conn:
        protected_before = protected_database_digests(conn)
    apply_report = tmp_path / "apply-report.json"
    resume_path = tmp_path / "resume.json"
    backup_dir = tmp_path / "backups" / "batch-1"
    apply_args = _materialize_args(
        project_root=tmp_path,
        db_path=db_path,
        campaign_path=campaign_path,
        report_path=apply_report,
    ) + [
        "--resume-state",
        str(resume_path),
        "--backup-dir",
        str(backup_dir),
        "--apply",
    ]
    assert materialize_main(apply_args) == 0

    payload = json.loads(apply_report.read_text(encoding="utf-8"))
    backup_path = Path(payload["backup"]["path"])
    assert backup_path.is_file()
    with sqlite3.connect(backup_path) as conn:
        assert database_integrity(conn) == {"quick_check": "ok", "foreign_key_violations": 0}
    with sqlite3.connect(db_path) as conn:
        assert protected_database_digests(conn) == protected_before
        inventory = projection_inventory(conn)
        assert all(
            row["current_content_rows"] == row["authoritative_images"]
            for row in inventory.values()
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM web_post_images WHERE image_role='content'"
        ).fetchone() == (5,)
        assert conn.execute(
            "SELECT COUNT(*) FROM web_post_images WHERE image_role='author_avatar'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM web_post_images WHERE local_path IS NOT NULL"
        ).fetchone() == (1,)
        raw_image = json.loads(
            conn.execute(
                """
                SELECT raw_image_json FROM web_post_images
                WHERE local_path IS NOT NULL
                """
            ).fetchone()[0]
        )
        assert raw_image["source_asset_key"].startswith("xhs:path:")
    assert payload["apply_result"] == {
        "post_images_count_updates": 3,
        "preserved_local_rows": 1,
        "processed_posts": 5,
        "removed_misclassified_rows": 3,
        "replaced_content_rows": 5,
    }
    assert hashlib.sha256(legacy_file.read_bytes()).hexdigest() == image_sha_before


def test_apply_can_resume_from_committed_batch_state(tmp_path: Path) -> None:
    db_path = _fixture_database(tmp_path, two_xhs_posts=True)
    campaign_path = tmp_path / "campaign.json"
    _campaign(db_path, campaign_path)
    state_path = tmp_path / "resume.json"
    base = [
        "--db",
        str(db_path),
        "--campaign",
        str(campaign_path),
        "--project-root",
        str(tmp_path),
        "--media-root",
        str(tmp_path / "data" / "media"),
        "--platform",
        "xhs",
        "--batch-size",
        "1",
        "--resume-state",
        str(state_path),
        "--apply",
    ]
    assert materialize_main(
        base
        + [
            "--report",
            str(tmp_path / "batch-1.json"),
            "--backup-dir",
            str(tmp_path / "backups" / "batch-1"),
        ]
    ) == 0
    first = json.loads(state_path.read_text(encoding="utf-8"))
    assert first["completed_platforms"] == []

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO xhs_accounts (
              account_id, status, profile_dir, encrypted_state_path, last_verified_at
            ) VALUES ('xhs-test', 'active', 'profile/xhs-test', 'state/xhs-test.enc',
                      '2026-08-07T13:04:17+00:00')
            """
        )
        conn.commit()

    with pytest.raises(ValueError, match="database SHA does not match resume state"):
        materialize_main(
            base
            + [
                "--report",
                str(tmp_path / "batch-2-refused.json"),
                "--backup-dir",
                str(tmp_path / "backups" / "batch-2-refused"),
            ]
        )

    assert materialize_main(
        base
        + [
            "--report",
            str(tmp_path / "batch-2.json"),
            "--backup-dir",
            str(tmp_path / "backups" / "batch-2"),
            "--resume-report",
            str(tmp_path / "batch-1.json"),
            "--allow-control-plane-drift",
        ]
    ) == 0
    second = json.loads(state_path.read_text(encoding="utf-8"))
    assert second["completed_platforms"] == ["xhs"]
    assert second["database_sha256_after"] == sha256_file(db_path)


def test_existing_xhs_promotion_is_dry_run_first_and_preserves_source_bytes(tmp_path: Path) -> None:
    db_path = _fixture_database(tmp_path)
    legacy_file = tmp_path / "legacy" / "xhs" / "xhs-1.jpg"
    Image.new("RGB", (4, 3), (80, 60, 40)).save(legacy_file, format="WEBP")
    legacy_verified = validate_image_file(
        legacy_file,
        allowed_root=tmp_path,
        require_suffix_match=False,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE web_post_images
            SET width=?, height=?, mime_type=?, sha256=?
            WHERE local_path='legacy/xhs/xhs-1.jpg'
            """,
            (
                legacy_verified.width,
                legacy_verified.height,
                legacy_verified.mime_type,
                legacy_verified.sha256,
            ),
        )
        conn.commit()
    campaign_path = tmp_path / "campaign.json"
    _campaign(db_path, campaign_path)
    legacy_sha = hashlib.sha256(legacy_file.read_bytes()).hexdigest()
    media_root = tmp_path / "data" / "media"
    base = [
        "--db",
        str(db_path),
        "--campaign",
        str(campaign_path),
        "--project-root",
        str(tmp_path),
        "--media-root",
        str(media_root),
        "--platform",
        "xhs",
        "--batch-size",
        "10",
        "--promote-existing",
    ]
    db_sha_before = sha256_file(db_path)
    assert materialize_main(base + ["--report", str(tmp_path / "promotion-dry.json")]) == 0
    assert sha256_file(db_path) == db_sha_before
    assert not media_root.exists()
    assert hashlib.sha256(legacy_file.read_bytes()).hexdigest() == legacy_sha

    assert materialize_main(
        base
        + [
            "--report",
            str(tmp_path / "promotion-apply.json"),
            "--resume-state",
            str(tmp_path / "promotion-resume.json"),
            "--backup-dir",
            str(tmp_path / "backups" / "promotion"),
            "--apply",
        ]
    ) == 0
    report = json.loads((tmp_path / "promotion-apply.json").read_text(encoding="utf-8"))
    assert report["operation"] == "promote_existing"
    assert report["promotion"] == {
        "promoted_bytes": legacy_file.stat().st_size,
        "promoted_images": 1,
        "reused_images": 0,
    }
    with sqlite3.connect(db_path) as conn:
        local_path, raw_image = conn.execute(
            """
            SELECT local_path, raw_image_json
            FROM web_post_images i
            JOIN web_posts p ON p.id=i.web_post_id
            WHERE p.platform_key='xhs' AND i.image_role='content'
            """
        ).fetchone()
        assert str(local_path).startswith("data/media/xhs/xhs-1/000-")
        assert json.loads(raw_image)["local_file"]["source"] == "historical_existing_promotion_v1"
        assert conn.execute(
            """
            SELECT local_path FROM web_post_images i
            JOIN web_posts p ON p.id=i.web_post_id
            WHERE p.platform_key='xhs' AND i.image_role='author_avatar'
            """
        ).fetchone() == (None,)
    promoted_file = tmp_path / local_path
    assert promoted_file.is_file()
    assert promoted_file.suffix == ".webp"
    assert hashlib.sha256(promoted_file.read_bytes()).hexdigest() == legacy_sha
    assert hashlib.sha256(legacy_file.read_bytes()).hexdigest() == legacy_sha


def test_gc_defaults_to_report_only_and_requires_explicit_confirmation(tmp_path: Path) -> None:
    db_path = _fixture_database(tmp_path)
    media_root = tmp_path / "data" / "media"
    orphan = media_root / "xhs" / "orphan.jpg"
    _write_image(orphan)
    db_sha_before = sha256_file(db_path)
    report_path = tmp_path / "gc-report.json"
    args = [
        "--db",
        str(db_path),
        "--project-root",
        str(tmp_path),
        "--media-root",
        str(media_root),
        "--report",
        str(report_path),
        "--campaign-id",
        "test-historical-images",
    ]
    assert gc_main(args) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["mode"] == "dry-run"
    assert report["unreferenced_files"] == 1
    assert report["deleted_files"] == 0
    assert orphan.is_file()
    assert sha256_file(db_path) == db_sha_before

    with pytest.raises(SystemExit, match="--confirm-delete"):
        gc_main(args + ["--apply"])
    assert orphan.is_file()
