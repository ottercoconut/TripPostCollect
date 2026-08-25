from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import replace
from importlib import import_module
from pathlib import Path

import pytest

from trippostcollect.db.bootstrap import bootstrap_connection


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

repair = import_module("repair_bilibili_articles")
promotion = import_module("promote_bilibili_repair_results")
supervisor = import_module("run_bilibili_full_repair_supervisor")


def create_target(path: Path, count: int = 2) -> None:
    with sqlite3.connect(path) as connection:
        bootstrap_connection(connection, sync_jobs=False)
        for index in range(1, count + 1):
            post_id = str(1000 + index)
            old_text = f"旧搜索摘要-{post_id}"
            raw = {
                "id": post_id,
                "content_id": post_id,
                "content_type": "article",
                "title": f"旧标题-{post_id}",
                "desc": old_text,
                "content_text": old_text,
                "image_urls": [f"https://example.test/preview-{post_id}.jpg"],
                "pubdate": 1_700_000_000,
                "published_at": 1_700_000_000,
                "author": "作者",
                "nickname": "作者",
                "mid": "88",
                "user_id": "88",
                "like": 1,
                "reply": 2,
                "view": 3,
                "liked_count": 1,
                "comment_count": 2,
                "view_count": 3,
                "followers_count": 50,
                "author_followers_count": 50,
                "followers_observed": True,
                "author_followers_source": "relation_stat",
                "source_keyword": "青岛旅游",
            }
            connection.execute(
                """
                INSERT INTO web_posts (
                    platform_key, platform_post_id, source_type, source_url,
                    canonical_url, title, author_display_name, author_platform_id,
                    published_at, captured_at, keyword, content_text, content_length,
                    metrics_json, author_json, raw_sample_json, artifact_dir,
                    capture_method, status, author_followers_count,
                    post_likes_count, post_comments_count, post_views_count,
                    post_images_count
                ) VALUES (
                    'bilibili', ?, 'mediacrawler', ?, ?, ?, '作者', '88',
                    '2023-11-15T06:13:20+08:00', '2026-08-02T00:00:00+00:00',
                    '青岛旅游', ?, ?, ?, ?, ?, '/original/artifact',
                    'import', 'captured', 50, 1, 2, 3, 1
                )
                """,
                (
                    post_id,
                    f"https://www.bilibili.com/read/cv{post_id}/",
                    f"https://www.bilibili.com/read/cv{post_id}/",
                    f"旧标题-{post_id}",
                    old_text,
                    len(old_text),
                    json.dumps(
                        {
                            "liked_count": 1,
                            "comments_count": 2,
                            "views_count": 3,
                        }
                    ),
                    json.dumps(
                        {
                            "followers_observed": True,
                            "followers_source": "relation_stat",
                        }
                    ),
                    json.dumps(raw, ensure_ascii=False),
                ),
            )
            web_post_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.execute(
                """
                INSERT INTO web_post_images (
                    web_post_id, image_index, image_url, image_role, raw_image_json
                ) VALUES (?, 0, ?, 'content', '{}')
                """,
                (web_post_id, f"https://example.test/preview-{post_id}.jpg"),
            )
        connection.commit()


def config_for(
    target: Path,
    state: Path,
    backup: Path,
    report_dir: Path,
    *,
    apply: bool,
    expected_sha256: str,
) -> repair.RepairConfig:
    return repair.RepairConfig(
        db_path=target,
        state_db_path=state,
        report_dir=report_dir,
        backup_path=backup,
        expected_baseline_sha256=expected_sha256,
        apply=apply,
        confirm_default_db_repair=False,
        max_items=10,
        session_size=10,
        pacing_min=0,
        pacing_max=0,
        session_pause_min=0,
        session_pause_max=0,
        retry_delay_seconds=0,
        source_limit=0,
        only_ids=frozenset(),
    )


def fake_detail(post_id: str, cookie_header: str):
    return (
        {
            "title": f"详情标题-{post_id}",
            "content": f"第一段完整正文-{post_id}\n第二段青岛完整正文",
            "image_urls": [f"https://example.test/detail-{post_id}.jpg"],
            "opus": {"content": {"paragraphs": []}},
        },
        1,
        0.0,
    )


@pytest.fixture(autouse=True)
def valid_bilibili_login(monkeypatch) -> None:
    monkeypatch.setattr(
        repair,
        "load_cookie_snapshot",
        lambda platform: {"cookie_header": "SESSDATA=test-session"},
    )
    monkeypatch.setattr(
        repair,
        "check_bilibili_login",
        lambda cookie_header: {
            "ok": True,
            "code": 0,
            "is_login": True,
            "source": "test",
        },
    )


def test_dry_run_creates_manifest_without_changing_target(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target)
    baseline = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    config = config_for(
        target,
        tmp_path / "state.sqlite",
        backup,
        tmp_path / "reports",
        apply=False,
        expected_sha256=baseline,
    )

    return_code, result = repair.run_repair(config)

    assert return_code == 0
    assert result["selected_count"] == 2
    assert result["stopped_reason"] == "dry_run"
    assert repair.sha256_file(target) == baseline
    with sqlite3.connect(config.state_db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM repair_items").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM repair_items WHERE status='pending'"
        ).fetchone()[0] == 2


def test_source_limit_restricts_pilot_scope(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=3)
    baseline = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    config = replace(
        config_for(
            target,
            tmp_path / "state.sqlite",
            backup,
            tmp_path / "reports",
            apply=False,
            expected_sha256=baseline,
        ),
        source_limit=2,
    )

    return_code, result = repair.run_repair(config)

    assert return_code == 0
    assert result["selected_count"] == 2
    assert result["scope_status_counts"]["total"] == 2
    assert result["scope_status_counts"]["pending"] == 2


def test_retryable_selection_uses_oldest_due_time(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=3)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    state = tmp_path / "state.sqlite"
    config = config_for(
        target,
        state,
        backup,
        tmp_path / "reports",
        apply=False,
        expected_sha256=repair.sha256_file(target),
    )
    repair.run_repair(config)
    with repair.sqlite_connect(state) as connection:
        rows = connection.execute(
            "SELECT web_post_id FROM repair_items ORDER BY web_post_id"
        ).fetchall()
        due_times = (
            "2026-01-03T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
        )
        for row, due_time in zip(rows, due_times, strict=True):
            connection.execute(
                "UPDATE repair_items SET status='retryable', next_retry_at=? WHERE web_post_id=?",
                (due_time, int(row["web_post_id"])),
            )
        connection.commit()
        selected = repair.selected_items(
            connection,
            max_items=3,
            source_limit=0,
            only_ids=frozenset(),
        )

    assert [str(row["platform_post_id"]) for row in selected] == ["1002", "1003", "1001"]


def test_operator_exclusion_terminalizes_retryables_without_target_changes(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=2)
    target_sha256 = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    state = tmp_path / "state.sqlite"
    config = config_for(
        target,
        state,
        backup,
        tmp_path / "reports",
        apply=False,
        expected_sha256=target_sha256,
    )
    repair.run_repair(config)
    with repair.sqlite_connect(state) as connection:
        connection.execute(
            """
            UPDATE repair_items
            SET status='retryable', attempt_count=6,
                last_error_type='BilibiliArticleDetailError',
                last_error_code=0, last_error='no parseable body',
                next_retry_at='2026-08-08T00:00:00+00:00'
            """
        )
        repair.set_meta(
            connection,
            {
                "global_next_request_at": "2026-08-08T00:00:00+00:00",
                "global_cooldown_reason": "BilibiliArticleDetailError:0",
                "global_retryable_streak": 10,
            },
        )
        connection.commit()

    result = repair.operator_exclude_retryable_items(
        db_path=target,
        state_path=state,
        report_dir=tmp_path / "exclusion-reports",
        platform_post_ids=frozenset({"1001", "1002"}),
        reason="用户批准保留原记录并排除反复不可解析项",
        apply=True,
        confirm_default_db_repair=False,
    )

    assert result["excluded_count"] == 2
    assert result["status_counts_before"]["retryable"] == 2
    assert result["status_counts_after"]["retryable"] == 0
    assert result["status_counts_after"]["invalid_detail"] == 0
    assert result["status_counts_after"]["operator_excluded"] == 2
    assert result["repair_state_validation_after"]["ok"] is True
    assert result["external_invariants_after"]["ok"] is True
    assert repair.sha256_file(target) == target_sha256
    with repair.sqlite_connect(state, readonly=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM repair_events WHERE event_type='operator_excluded'"
        ).fetchone()[0] == 2
        meta = repair.meta_values(connection)
    assert "global_next_request_at" not in meta
    assert "global_cooldown_reason" not in meta
    assert meta["global_retryable_streak"] == "0"
    assert Path(result["report_path"]).is_file()


def test_operator_exclusion_rejects_non_retryable_items(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=1)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    state = tmp_path / "state.sqlite"
    repair.run_repair(
        config_for(
            target,
            state,
            backup,
            tmp_path / "reports",
            apply=False,
            expected_sha256=repair.sha256_file(target),
        )
    )

    with pytest.raises(RuntimeError, match="only accepts retryable items"):
        repair.operator_exclude_retryable_items(
            db_path=target,
            state_path=state,
            report_dir=tmp_path / "exclusion-reports",
            platform_post_ids=frozenset({"1001"}),
            reason="不能排除尚未尝试的记录",
            apply=True,
            confirm_default_db_repair=False,
        )


def test_legacy_state_migrates_trailing_retryables_to_exponential_cooldown(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=1)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    state = tmp_path / "state.sqlite"
    config = config_for(
        target,
        state,
        backup,
        tmp_path / "reports",
        apply=False,
        expected_sha256=repair.sha256_file(target),
    )
    repair.run_repair(config)
    before = repair.datetime.now(repair.UTC)
    with repair.sqlite_connect(state) as connection:
        repair.add_event(connection, "retryable")
        repair.add_event(connection, "retryable")
        repair.set_meta(
            connection,
            {
                "global_next_request_at": (
                    before + repair.timedelta(seconds=300)
                ).isoformat(timespec="seconds"),
                "global_cooldown_reason": "legacy-test",
            },
        )
        connection.commit()
        cooldown = repair.active_global_cooldown(
            connection,
            base_delay_seconds=300,
        )
        meta = repair.meta_values(connection)

    assert cooldown is not None
    assert meta["global_retryable_streak"] == "2"
    deadline = repair.datetime.fromisoformat(meta["global_next_request_at"])
    assert (deadline - before).total_seconds() >= 599


def test_apply_updates_same_rows_and_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target)
    baseline = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    config = config_for(
        target,
        tmp_path / "state.sqlite",
        backup,
        tmp_path / "reports",
        apply=True,
        expected_sha256=baseline,
    )
    monkeypatch.setattr(repair, "fetch_repair_article_detail", fake_detail)

    first_code, first = repair.run_repair(config)
    second_code, second = repair.run_repair(config)

    assert first_code == second_code == 0
    assert first["updated"] == 2
    assert second["selected_count"] == 0
    assert second["updated"] == 0
    with sqlite3.connect(target) as connection:
        rows = connection.execute(
            """
            SELECT platform_post_id, keyword, artifact_dir, content_text,
                   post_images_count, capture_method, topic_relevant,
                   json_extract(raw_sample_json, '$.content_detail_status')
            FROM web_posts ORDER BY id
            """
        ).fetchall()
        images = connection.execute(
            "SELECT image_url FROM web_post_images ORDER BY web_post_id, image_index"
        ).fetchall()
    assert len(rows) == 2
    assert all(row[1] == "青岛旅游" for row in rows)
    assert all(row[2] == "/original/artifact" for row in rows)
    assert all(row[3].startswith("第一段完整正文") for row in rows)
    assert all(row[4:] == (1, "import", 1, "detail_observed") for row in rows)
    assert images == [
        ("https://example.test/detail-1001.jpg",),
        ("https://example.test/detail-1002.jpg",),
    ]
    latest_report = json.loads((config.report_dir / "latest.json").read_text())
    assert latest_report["repair_state_validation"]["ok"] is True
    assert latest_report["repair_state_validation"]["checked_succeeded"] == 2


def test_retryable_failure_preserves_original_row(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=1)
    baseline = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    config = config_for(
        target,
        tmp_path / "state.sqlite",
        backup,
        tmp_path / "reports",
        apply=True,
        expected_sha256=baseline,
    )

    config = replace(config, retry_delay_seconds=300)
    calls = 0

    def fail(post_id, cookie_header):
        nonlocal calls
        calls += 1
        raise repair.BilibiliArticleDetailError(
            "rate limited",
            retryable=True,
            code=-509,
            attempts=3,
            retry_wait_seconds=12.0,
        )

    monkeypatch.setattr(repair, "fetch_repair_article_detail", fail)

    return_code, result = repair.run_repair(config)
    cooldown_code, cooldown_result = repair.run_repair(config)

    assert return_code == 2
    assert result["retryable"] == 1
    assert cooldown_code == 3
    assert cooldown_result["selected_count"] == 0
    assert cooldown_result["stopped_reason"] == "global_cooldown"
    assert calls == 1
    with sqlite3.connect(target) as connection:
        row = connection.execute(
            "SELECT content_text, post_images_count, capture_method FROM web_posts"
        ).fetchone()
    assert row == ("旧搜索摘要-1001", 1, "import")
    with sqlite3.connect(config.state_db_path) as connection:
        assert connection.execute("SELECT status FROM repair_items").fetchone()[0] == "retryable"
        assert connection.execute(
            "SELECT value FROM repair_meta WHERE key='global_next_request_at'"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT value FROM repair_meta WHERE key='global_retryable_streak'"
        ).fetchone()[0] == "1"
    latest_report = json.loads((config.report_dir / "latest.json").read_text())
    assert latest_report["repair_state_validation"]["ok"] is True
    assert latest_report["repair_state_validation"]["checked_untouched"] == 1


def test_consecutive_retryable_failures_double_global_cooldown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=1)
    baseline = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    config = replace(
        config_for(
            target,
            tmp_path / "state.sqlite",
            backup,
            tmp_path / "reports",
            apply=True,
            expected_sha256=baseline,
        ),
        retry_delay_seconds=300,
    )

    def fail(post_id, cookie_header):
        raise repair.BilibiliArticleDetailError(
            "rate limited",
            retryable=True,
            code=-509,
            attempts=1,
        )

    monkeypatch.setattr(repair, "fetch_repair_article_detail", fail)
    first_code, _ = repair.run_repair(config)
    with repair.sqlite_connect(config.state_db_path) as connection:
        connection.execute(
            "UPDATE repair_meta SET value='2026-01-01T00:00:00+00:00' WHERE key='global_next_request_at'"
        )
        connection.execute(
            "UPDATE repair_items SET next_retry_at='2026-01-01T00:00:00+00:00'"
        )
        connection.commit()

    before = repair.datetime.now(repair.UTC)
    second_code, _ = repair.run_repair(config)

    assert first_code == second_code == 2
    with repair.sqlite_connect(config.state_db_path) as connection:
        meta = repair.meta_values(connection)
    assert meta["global_retryable_streak"] == "2"
    deadline = repair.datetime.fromisoformat(meta["global_next_request_at"])
    assert (deadline - before).total_seconds() >= 599


def test_successful_detail_resets_global_retryable_streak(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=1)
    baseline = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    state = tmp_path / "state.sqlite"
    report_dir = tmp_path / "reports"
    dry_config = config_for(
        target,
        state,
        backup,
        report_dir,
        apply=False,
        expected_sha256=baseline,
    )
    repair.run_repair(dry_config)
    with repair.sqlite_connect(state) as connection:
        repair.set_meta(
            connection,
            {
                "global_retryable_streak": 3,
                "global_next_request_at": "2026-01-01T00:00:00+00:00",
                "global_cooldown_reason": "test",
            },
        )
        connection.commit()
    monkeypatch.setattr(repair, "fetch_repair_article_detail", fake_detail)

    return_code, result = repair.run_repair(
        replace(dry_config, apply=True)
    )

    assert return_code == 0
    assert result["updated"] == 1
    with repair.sqlite_connect(state) as connection:
        meta = repair.meta_values(connection)
        reset_events = connection.execute(
            "SELECT COUNT(*) FROM repair_events WHERE event_type='global_retryable_streak_reset'"
        ).fetchone()[0]
    assert meta["global_retryable_streak"] == "0"
    assert reset_events == 1


def test_continuous_pilot_stops_when_scope_has_been_attempted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=1)
    config = config_for(
        target,
        tmp_path / "state.sqlite",
        tmp_path / "backup.sqlite",
        tmp_path / "reports",
        apply=True,
        expected_sha256=repair.sha256_file(target),
    )
    calls = 0

    def completed_scope(value):
        nonlocal calls
        calls += 1
        return 2, {
            "scope_status_counts": {"pending": 0, "retryable": 1},
            "global_cooldown": {"next_request_at": "2099-01-01T00:00:00+00:00"},
        }

    monkeypatch.setattr(repair, "run_repair", completed_scope)
    monkeypatch.setattr(
        repair.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(AssertionError("unexpected sleep")),
    )

    return_code, result = repair.run_continuous_repair(
        config,
        stop_when_scope_attempted=True,
    )

    assert return_code == 0
    assert result["continuous_stop_reason"] == "scope_attempted"
    assert calls == 1


def test_promote_validated_successes_into_original_target(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=2)
    target_sha256 = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    staged = tmp_path / "staged.sqlite"
    repair.create_online_backup(target, backup)
    repair.create_online_backup(target, staged)
    state = tmp_path / "state.sqlite"
    repair_report_dir = tmp_path / "repair-reports"
    repair_config = config_for(
        staged,
        state,
        backup,
        repair_report_dir,
        apply=True,
        expected_sha256=repair.sha256_file(staged),
    )
    monkeypatch.setattr(repair, "fetch_repair_article_detail", fake_detail)
    repair_code, repair_result = repair.run_repair(repair_config)
    assert repair_code == 0
    assert repair_result["updated"] == 2

    config = promotion.PromotionConfig(
        target_db_path=target,
        staged_db_path=staged,
        state_db_path=state,
        backup_path=backup,
        report_dir=tmp_path / "promotion-reports",
        expected_target_sha256=target_sha256,
        apply=False,
        confirm_default_db_promotion=False,
    )
    dry_code, dry_result = promotion.run_promotion(config)
    apply_code, apply_result = promotion.run_promotion(replace(config, apply=True))

    assert dry_code == apply_code == 0
    assert dry_result["planned_count"] == 2
    assert dry_result["promoted_count"] == 0
    assert apply_result["promoted_count"] == 2
    assert apply_result["target_validation"]["ok"] is True
    with sqlite3.connect(target) as connection:
        rows = connection.execute(
            "SELECT id, content_text, post_images_count FROM web_posts ORDER BY id"
        ).fetchall()
        image_urls = connection.execute(
            "SELECT image_url FROM web_post_images ORDER BY web_post_id, image_index"
        ).fetchall()
    assert [row[0] for row in rows] == [1, 2]
    assert all(str(row[1]).startswith("第一段完整正文") for row in rows)
    assert all(row[2] == 1 for row in rows)
    assert image_urls == [
        ("https://example.test/detail-1001.jpg",),
        ("https://example.test/detail-1002.jpg",),
    ]


def test_live_login_loss_stops_before_detail_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=1)
    baseline = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    config = config_for(
        target,
        tmp_path / "state.sqlite",
        backup,
        tmp_path / "reports",
        apply=True,
        expected_sha256=baseline,
    )
    detail_calls = 0

    def login_lost(cookie_header):
        raise repair.BilibiliRepairLoginRequiredError("code=-101 isLogin=False")

    def unexpected_detail(post_id, cookie_header):
        nonlocal detail_calls
        detail_calls += 1
        return fake_detail(post_id, cookie_header)

    monkeypatch.setattr(repair, "check_bilibili_login", login_lost)
    monkeypatch.setattr(repair, "fetch_repair_article_detail", unexpected_detail)

    return_code, result = repair.run_repair(config)

    assert return_code == 4
    assert result["stopped_reason"] == "login_required"
    assert detail_calls == 0
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT content_text FROM web_posts"
        ).fetchone()[0] == "旧搜索摘要-1001"
    with sqlite3.connect(config.state_db_path) as connection:
        assert connection.execute(
            "SELECT status FROM repair_items"
        ).fetchone()[0] == "pending"
        assert connection.execute(
            "SELECT COUNT(*) FROM repair_events WHERE event_type='login_required'"
        ).fetchone()[0] == 1


def test_article_api_login_code_stops_without_invalidating_item(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=1)
    baseline = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    config = config_for(
        target,
        tmp_path / "state.sqlite",
        backup,
        tmp_path / "reports",
        apply=True,
        expected_sha256=baseline,
    )

    def login_code(post_id, cookie_header):
        raise repair.BilibiliArticleDetailError(
            "login required",
            retryable=False,
            code=-101,
            attempts=1,
        )

    monkeypatch.setattr(repair, "fetch_repair_article_detail", login_code)

    return_code, result = repair.run_repair(config)

    assert return_code == 4
    assert result["stopped_reason"] == "login_required"
    with sqlite3.connect(config.state_db_path) as connection:
        row = connection.execute(
            "SELECT status, last_error_type, last_error_code FROM repair_items"
        ).fetchone()
    assert row == ("pending", "login_required", -101)


def test_incremental_promotion_and_target_state_clone(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=2)
    backup = tmp_path / "backup.sqlite"
    staged = tmp_path / "staged.sqlite"
    repair.create_online_backup(target, backup)
    repair.create_online_backup(target, staged)
    state = tmp_path / "state.sqlite"
    repair_config = config_for(
        staged,
        state,
        backup,
        tmp_path / "repair-reports",
        apply=True,
        expected_sha256=repair.sha256_file(staged),
    )
    monkeypatch.setattr(repair, "fetch_repair_article_detail", fake_detail)

    first_code, first_result = repair.run_repair(
        replace(repair_config, max_items=1)
    )
    assert first_code == 0
    assert first_result["updated"] == 1
    first_promotion = promotion.PromotionConfig(
        target_db_path=target,
        staged_db_path=staged,
        state_db_path=state,
        backup_path=backup,
        report_dir=tmp_path / "promotion-reports",
        expected_target_sha256=repair.sha256_file(target),
        apply=True,
        confirm_default_db_promotion=False,
    )
    _, first_promoted = promotion.run_promotion(first_promotion)
    assert first_promoted["promoted_count"] == 1
    assert first_promoted["already_promoted_count"] == 0

    second_code, second_result = repair.run_repair(repair_config)
    assert second_code == 0
    assert second_result["updated"] == 1
    second_promotion = replace(
        first_promotion,
        expected_target_sha256=repair.sha256_file(target),
    )
    _, second_promoted = promotion.run_promotion(second_promotion)
    assert second_promoted["promoted_count"] == 1
    assert second_promoted["already_promoted_count"] == 1

    cloned_state = tmp_path / "full-state.sqlite"
    clone = promotion.clone_repair_state_for_target(
        source_state_path=state,
        destination_state_path=cloned_state,
        staged_db_path=staged,
        target_db_path=target,
    )

    assert clone["cloned_validation"]["ok"] is True
    with repair.sqlite_connect(cloned_state, readonly=True) as connection:
        meta = repair.meta_values(connection)
        counts = repair.state_counts(connection)
    assert meta["target_db_path"] == str(target.resolve())
    assert meta["cloned_from_state_db"] == str(state.resolve())
    assert counts["succeeded"] == 2


def test_pilot_gate_rejects_retryable_rows(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=2)
    source_backup = tmp_path / "source-backup.sqlite"
    pilot = tmp_path / "pilot.sqlite"
    repair.create_online_backup(target, source_backup)
    repair.create_online_backup(target, pilot)
    pilot_state = tmp_path / "pilot-state.sqlite"
    with repair.sqlite_connect(pilot_state) as connection:
        repair.initialize_repair_state(
            pilot,
            connection,
            tmp_path / "pilot-reports",
        )
        connection.execute(
            "UPDATE repair_items SET status='retryable', updated_at=?",
            (repair.utc_now(),),
        )
        connection.commit()

    config = supervisor.SupervisorConfig(
        pilot_db_path=pilot,
        pilot_state_path=pilot_state,
        source_backup_path=source_backup,
        target_db_path=target,
        expected_target_sha256=repair.sha256_file(target),
        pre_full_backup_path=tmp_path / "pre-full.sqlite",
        full_state_path=tmp_path / "full-state.sqlite",
        pilot_report_dir=tmp_path / "pilot-reports",
        promotion_report_dir=tmp_path / "promotion-reports",
        full_report_dir=tmp_path / "full-reports",
        supervisor_report_path=tmp_path / "supervisor.json",
        confirm_default_db_full_repair=False,
        source_limit=2,
        max_items=2,
        session_size=1,
        pacing_min=0,
        pacing_max=0,
        session_pause_min=0,
        session_pause_max=0,
        retry_delay_seconds=0,
    )

    with pytest.raises(RuntimeError, match="still has retryable rows"):
        supervisor.validate_pilot_gate(config)


def test_supervisor_promotes_pilot_and_prepares_full_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=2)
    source_backup = tmp_path / "source-backup.sqlite"
    pilot = tmp_path / "pilot.sqlite"
    repair.create_online_backup(target, source_backup)
    repair.create_online_backup(target, pilot)
    pilot_state = tmp_path / "pilot-state.sqlite"
    pilot_config = config_for(
        pilot,
        pilot_state,
        source_backup,
        tmp_path / "pilot-reports",
        apply=True,
        expected_sha256=repair.sha256_file(pilot),
    )
    monkeypatch.setattr(repair, "fetch_repair_article_detail", fake_detail)
    repair_code, repair_result = repair.run_repair(pilot_config)
    assert repair_code == 0
    assert repair_result["updated"] == 2

    continuous_calls = 0
    stop_modes: list[bool] = []

    def completed_repair(config, *, stop_when_scope_attempted):
        nonlocal continuous_calls
        continuous_calls += 1
        stop_modes.append(stop_when_scope_attempted)
        return 0, {
            "scope_status_counts": {
                "pending": 0,
                "retryable": 0,
                "succeeded": 2,
                "total": 2,
            }
        }

    monkeypatch.setattr(supervisor, "run_continuous_repair", completed_repair)
    config = supervisor.SupervisorConfig(
        pilot_db_path=pilot,
        pilot_state_path=pilot_state,
        source_backup_path=source_backup,
        target_db_path=target,
        expected_target_sha256=repair.sha256_file(target),
        pre_full_backup_path=tmp_path / "pre-full.sqlite",
        full_state_path=tmp_path / "full-state.sqlite",
        pilot_report_dir=tmp_path / "pilot-reports",
        promotion_report_dir=tmp_path / "promotion-reports",
        full_report_dir=tmp_path / "full-reports",
        supervisor_report_path=tmp_path / "supervisor.json",
        confirm_default_db_full_repair=False,
        source_limit=2,
        max_items=2,
        session_size=1,
        pacing_min=0,
        pacing_max=0,
        session_pause_min=0,
        session_pause_max=0,
        retry_delay_seconds=0,
    )

    return_code = supervisor.run_supervisor(config)
    resume_code = supervisor.run_supervisor(config)

    assert return_code == resume_code == 0
    assert continuous_calls == 3
    assert stop_modes == [False, False, False]
    assert config.pre_full_backup_path.is_file()
    assert config.full_state_path.is_file()
    payload = json.loads(config.supervisor_report_path.read_text())
    assert payload["stage"] == "completed"
    assert payload["status"] == "completed"
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM web_posts WHERE content_text LIKE '第一段完整正文%'"
        ).fetchone()[0] == 2


def test_optimistic_lock_conflict_does_not_overwrite(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    create_target(target, count=1)
    baseline = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    state = tmp_path / "state.sqlite"
    report_dir = tmp_path / "reports"
    dry_run = config_for(
        target,
        state,
        backup,
        report_dir,
        apply=False,
        expected_sha256=baseline,
    )
    repair.run_repair(dry_run)
    with sqlite3.connect(target) as connection:
        connection.execute(
            "UPDATE web_posts SET content_text='并发修改', content_length=4 WHERE platform_key='bilibili'"
        )
        connection.commit()
    apply_config = config_for(
        target,
        state,
        backup,
        report_dir,
        apply=True,
        expected_sha256=baseline,
    )
    monkeypatch.setattr(repair, "fetch_repair_article_detail", fake_detail)

    return_code, result = repair.run_repair(apply_config)

    assert return_code == 0
    assert result["conflict"] == 1
    with sqlite3.connect(target) as connection:
        row = connection.execute(
            "SELECT content_text, capture_method FROM web_posts"
        ).fetchone()
    assert row == ("并发修改", "import")
