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


def _weibo_fixture_database(root: Path) -> tuple[Path, dict[str, object]]:
    db_path = root / "data" / "weibo-fixture.sqlite"
    db_path.parent.mkdir(parents=True)
    raw: dict[str, object] = {
        "note_id": "wb-101",
        "note_url": "https://m.weibo.cn/detail/wb-101",
        "image_list_source": "mblog.pics",
        "image_list": [
            {
                "url": "https://wx1.sinaimg.cn/orj360/weibo-a.jpg",
                "pid": "weibo-a",
            }
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
              'weibo', 'wb-101', 'mediacrawler_search',
              'https://m.weibo.cn/detail/wb-101',
              'https://m.weibo.cn/detail/wb-101',
              '正文', 2, 1, ?, 'fixture', 'import', '2026-08-08T00:00:00+00:00'
            )
            """,
            (json.dumps(raw, ensure_ascii=False, sort_keys=True),),
        )
        web_post_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO web_post_images (
              web_post_id, image_index, image_url, image_role, raw_image_json
            ) VALUES (?, 0, ?, 'content', '{}')
            """,
            (web_post_id, raw["image_list"][0]["url"]),
        )
        conn.commit()
    return db_path, raw


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
        resolved = resolution._resolve_targets(
            conn, state, "bilibili", [("101", 0)]
        )

    result = resolution._apply_database(
        db_path,
        resolved,
        platform_key="bilibili",
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


def test_operator_resolution_resolves_weibo_identity(tmp_path: Path) -> None:
    db_path, raw = _weibo_fixture_database(tmp_path)
    candidate = content_image_candidates("weibo", raw)[0]
    state = {
        "deferred_posts": {
            "weibo": {
                "wb-101": {
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
                                    "http_status": 400,
                                    "attempts": 3,
                                }
                            ],
                        }
                    ],
                }
            }
        }
    }

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        resolved = resolution._resolve_targets(conn, state, "weibo", [("wb-101", 0)])

    assert resolved == [
        {
            "web_post_id": resolved[0]["web_post_id"],
            "platform_post_id": "wb-101",
            "source_index": 0,
            "source_asset_key": candidate.source_asset_key,
            "source_url": "https://wx1.sinaimg.cn/orj360/weibo-a.jpg",
            "image_row_id": resolved[0]["image_row_id"],
            "candidate_count_before": 1,
            "evidence": {
                "deferred_reason": "terminal_image_failure",
                "failure_codes": ["image_source_unavailable"],
                "failure": state["deferred_posts"]["weibo"]["wb-101"]["history"][0][
                    "failures"
                ][0],
                "source_report": "fixture-report.json",
            },
        }
    ]


def test_release_state_only_clears_selected_platform(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "background-worker"
    runtime_dir.mkdir(parents=True)
    state_path = runtime_dir / "state.json"
    state = {
        "status": "review_required",
        "historical_data_complete": False,
        "deferred_posts": {
            "weibo": {"wb-101": {"reason": "terminal_image_failure"}},
            "bilibili": {"bili-101": {"reason": "terminal_image_failure"}},
        },
        "retry_events": [],
    }

    resolution._release_state(
        state_path,
        state,
        platform_key="weibo",
        post_ids={"wb-101"},
        report_path=tmp_path / "report.json",
    )

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "ready_to_resume"
    assert persisted["deferred_posts"]["weibo"] == {}
    assert set(persisted["deferred_posts"]["bilibili"]) == {"bili-101"}
    assert persisted["retry_events"][-1]["platform"] == "weibo"
