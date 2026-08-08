from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

from trippostcollect.artifacts.historical_image_materialization import (
    approved_historical_image_exclusions,
    build_relationship_plan,
    projection_inventory,
)
from trippostcollect.artifacts.image_candidates import content_image_candidates
from trippostcollect.db.bootstrap import bootstrap_connection

SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import resolve_historical_image_deferred as resolution  # noqa: E402


def _fixture_database(root: Path) -> Path:
    db_path = root / "data" / "fixture.sqlite"
    db_path.parent.mkdir(parents=True)
    raw = {
        "content_id": "101",
        "content_images_detail_status": "detail_observed",
        "image_urls": [
            "https://i0.hdslb.com/bfs/article/bili-a.jpg",
            "https://i0.hdslb.com/bfs/article/bili-b.jpg",
        ],
    }
    with sqlite3.connect(db_path) as conn:
        bootstrap_connection(conn, sync_jobs=False)
        conn.execute(
            """
            INSERT INTO web_posts (
              platform_key, platform_post_id, source_type, source_url, canonical_url,
              content_text, content_length, post_images_count, raw_sample_json,
              artifact_dir, capture_method, captured_at
            ) VALUES (
              'bilibili', '101', 'mediacrawler_search',
              'https://www.bilibili.com/read/cv101/',
              'https://www.bilibili.com/read/cv101/',
              '正文', 2, 2, ?, 'fixture', 'import', '2026-08-08T00:00:00+00:00'
            )
            """,
            (json.dumps(raw, ensure_ascii=False, sort_keys=True),),
        )
        web_post_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for index, url in enumerate(raw["image_urls"]):
            conn.execute(
                """
                INSERT INTO web_post_images (
                  web_post_id, image_index, image_url, image_role, raw_image_json
                ) VALUES (?, ?, ?, 'content', '{}')
                """,
                (web_post_id, index, url),
            )
        conn.commit()
    return db_path


def test_operator_resolution_deletes_only_approved_image_relation(tmp_path: Path) -> None:
    db_path = _fixture_database(tmp_path)
    with sqlite3.connect(db_path) as conn:
        raw = json.loads(
            conn.execute(
                "SELECT raw_sample_json FROM web_posts WHERE platform_key='bilibili' AND platform_post_id='101'"
            ).fetchone()[0]
        )
        candidate = content_image_candidates("bilibili", raw)[0]
    state = {
        "deferred_posts": {
            "bilibili": {
                "101": {
                    "reason": "terminal_image_failure",
                    "failure_codes": ["image_source_unavailable"],
                    "history": [
                        {
                            "report": "fixture-report.json",
                            "failures": [
                                {
                                    "source_index": 0,
                                    "source_asset_key": candidate.source_asset_key,
                                    "error_code": "image_source_unavailable",
                                    "http_status": 404,
                                    "attempts": 1,
                                }
                            ],
                        }
                    ],
                }
            }
        }
    }
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        resolved = resolution._resolve_targets(conn, state, [("101", 0)])

    result = resolution._apply_database(
        db_path,
        resolved,
        reason="user approved fixture exclusion",
        backup_dir=tmp_path / "backup",
    )

    assert result["deleted_image_rows"] == 1
    assert result["approved_exclusions"] == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM web_post_images AS i JOIN web_posts AS p ON p.id=i.web_post_id WHERE p.platform_post_id='101' AND i.image_role='content'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT post_images_count FROM web_posts WHERE platform_post_id='101'"
        ).fetchone()[0] == 1
        assert approved_historical_image_exclusions(conn) == {
            ("bilibili", "101"): {candidate.source_asset_key}
        }
        inventory = projection_inventory(conn, platforms=("bilibili",))
        plan = build_relationship_plan(
            conn,
            platforms=("bilibili",),
            batch_size=10,
            project_root=tmp_path,
            media_root=tmp_path / "data" / "media",
            require_missing_local=True,
        )
    assert inventory["bilibili"] == {
        "posts": 1,
        "current_content_rows": 1,
        "authoritative_images": 1,
        "misclassified_rows": 0,
        "existing_local_rows": 0,
        "local_gap": 1,
    }
    assert len(plan.posts) == 1
    assert plan.posts[0].prepared_images[0]["source_index"] == 0
    assert plan.posts[0].prepared_images[0]["url"].endswith("bili-b.jpg")
