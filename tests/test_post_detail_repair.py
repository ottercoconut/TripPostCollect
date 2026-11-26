"""TripPostCollect tests for post detail repair."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from importlib import import_module
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

repair = import_module("repair_post_details")
mediacrawler = import_module("mediacrawler_crawl")


@pytest.mark.parametrize(
    ("platform", "post_id", "url", "expected"),
    [
        (
            "weibo",
            "456",
            "https://weibo.com/88/456",
            "https://weibo.com/88/456",
        ),
        (
            "zhihu",
            "789",
            "https://www.zhihu.com/question/12/answer/789?utm_source=test",
            "https://www.zhihu.com/question/12/answer/789",
        ),
        (
            "zhihu",
            "321",
            "https://zhuanlan.zhihu.com/p/321",
            "https://zhuanlan.zhihu.com/p/321",
        ),
    ],
)
def test_detail_target_is_strictly_bound_to_platform_id(
    platform: str,
    post_id: str,
    url: str,
    expected: str,
) -> None:
    if platform == "weibo":
        assert repair._detail_target(
            platform, post_id, url, author_platform_id="88",
        ) == (expected, "")
        missing, missing_reason = repair._detail_target(platform, post_id, url)
        assert missing is None
        assert missing_reason == repair.MISSING_WEIBO_AUTHOR_PLATFORM_ID
        target, reason = repair._detail_target(
            platform, "different", url, author_platform_id="88",
        )
        assert target is None
        assert reason.startswith("invalid_")
        return
    assert repair._detail_target(platform, post_id, url) == (expected, "")
    target, reason = repair._detail_target(platform, "different", url)
    assert target is None
    assert reason.startswith("invalid_")


def test_select_targets_excludes_observed_and_reports_bad_urls(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "repair.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE web_posts(
            id INTEGER PRIMARY KEY,
            platform_key TEXT,
            platform_post_id TEXT,
            canonical_url TEXT,
            keyword TEXT,
            raw_sample_json TEXT,
            artifact_dir TEXT,
            published_at TEXT,
            author_followers_count INTEGER,
            author_display_name TEXT,
            author_platform_id TEXT,
            author_profile_url TEXT,
            author_description TEXT
        )
        """
    )
    rows = [
        (1, "1", "88", "https://weibo.com/88/1", "青岛旅游", "{}"),
        (
            2,
            "2",
            "88",
            "https://weibo.com/88/2",
            "青岛旅游",
            json.dumps({"content_detail_status": "detail_observed"}),
        ),
        (3, "bad", "88", "https://example.test/video/3", "青岛旅游", "not-json"),
    ]
    conn.executemany(
        """
        INSERT INTO web_posts(
            id, platform_key, platform_post_id, author_platform_id, canonical_url, keyword,
            raw_sample_json, artifact_dir
        ) VALUES (?, 'weibo', ?, ?, ?, ?, ?, '')
        """,
        rows,
    )
    conn.commit()

    targets, rejected, pending = repair.select_targets(
        conn,
        platform="weibo",
        post_ids=[],
        max_items=0,
    )

    assert pending == 2
    assert [item["platform_post_id"] for item in targets] == ["1"]
    assert rejected[0]["platform_post_id"] == "bad"
    assert rejected[0]["reason"] == "invalid_weibo_canonical_url"
    conn.close()


def test_select_targets_carries_existing_repair_metadata(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "repair.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE web_posts(
            id INTEGER PRIMARY KEY,
            platform_key TEXT,
            platform_post_id TEXT,
            canonical_url TEXT,
            keyword TEXT,
            raw_sample_json TEXT,
            artifact_dir TEXT,
            published_at TEXT,
            author_followers_count INTEGER,
            author_display_name TEXT,
            author_platform_id TEXT,
            author_profile_url TEXT,
            author_description TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO web_posts(
            id, platform_key, platform_post_id, canonical_url, keyword,
            raw_sample_json, artifact_dir, published_at, author_followers_count,
            author_display_name, author_platform_id, author_profile_url,
            author_description
        ) VALUES (1, 'zhihu', 'answer-1',
            'https://www.zhihu.com/question/1/answer/answer-1', '青岛旅游', ?, '',
            '2026-07-13T12:00:00+08:00', 123, '作者', 'author-1',
            'https://www.zhihu.com/people/author-1', '简介')
        """,
        (
            json.dumps(
                {
                    "content_type": "answer",
                    "created_time": 1780000000,
                    "followers_count": 123,
                    "followers_observed": True,
                    "author_followers_source": "search_author",
                    "creator_hash": "hash-1",
                    "user_nickname": "作***",
                }
            ),
        ),
    )
    conn.commit()

    targets, rejected, pending = repair.select_targets(
        conn,
        platform="zhihu",
        post_ids=[],
        max_items=0,
    )

    assert pending == 1
    assert not rejected
    fallback = targets[0]["repair_fallback"]
    assert fallback["published_at"] == "2026-07-13T12:00:00+08:00"
    assert fallback["created_time"] == 1780000000
    assert "followers_observed" not in fallback
    assert "author_followers_source" not in fallback
    assert "followers_count" not in fallback
    assert "author_followers_count" not in fallback
    assert "author_display_name" not in fallback
    assert "creator_hash" not in fallback
    assert "user_nickname" not in fallback
    conn.close()


def test_user_waiver_keeps_unobserved_post_but_removes_it_from_targets(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    repair.bootstrap_database(db_path, sync_jobs=False)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            INSERT INTO web_posts(
                platform_key, platform_post_id, source_type, source_url,
                canonical_url, captured_at, keyword, raw_sample_json
            ) VALUES (
                'weibo', 'waived-1', 'search',
                'https://m.weibo.cn/detail/waived-1',
                'https://m.weibo.cn/detail/waived-1',
                '2026-08-24T20:00:00+08:00', '青岛旅游', '{}'
            )
            """
        )
        waived = repair.waive_post_detail_repairs(
            conn,
            platform="weibo",
            post_ids=["waived-1"],
            reason="user_waived_after_bounded_retry",
            authorized_by="user",
            evidence_by_post_id={"waived-1": {"run_id": "weibo-test"}},
        )
        conn.commit()

        targets, rejected, pending = repair.select_targets(
            conn,
            platform="weibo",
            post_ids=[],
            max_items=0,
        )

        assert waived == ["waived-1"]
        assert pending == 1
        assert targets == []
        assert rejected[0]["reason"] == "post_detail_repair_waived"
        assert rejected[0]["repair_waiver"]["evidence"] == {
            "run_id": "weibo-test"
        }
        assert repair.waived_pending_count(conn, "weibo") == 1
        assert repair.all_rejected_targets_waived(rejected) is True
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version=19"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    ("platform", "detail_target"),
    [
        ("weibo", "https://weibo.com/88/123"),
        ("zhihu", "https://www.zhihu.com/question/1/answer/123"),
    ],
)
def test_load_post_repair_targets_accepts_platform_specific_detail_values(
    tmp_path: Path,
    platform: str,
    detail_target: str,
) -> None:
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps(
            [
                {
                    "platform_post_id": "123",
                    "detail_target": detail_target,
                    "keyword": "青岛旅游",
                }
            ]
        ),
        encoding="utf-8",
    )

    targets = mediacrawler.load_post_repair_targets(path, platform)

    assert targets[0]["platform_post_id"] == "123"
    assert targets[0]["keyword"] == "青岛旅游"


def test_load_post_repair_targets_rejects_cross_id_target(tmp_path: Path) -> None:
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps(
            [
                {
                    "platform_post_id": "123",
                    "detail_target": "https://weibo.com/88/999",
                    "keyword": "青岛旅游",
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="does not match weibo ID 123"):
        mediacrawler.load_post_repair_targets(path, "weibo")


def test_generic_repair_evidence_accounts_for_every_unrecovered_target() -> None:
    evidence = mediacrawler.post_repair_pagination_evidence(
        [
            {"platform_post_id": "ok"},
            {"platform_post_id": "missing-detail"},
            {"platform_post_id": "bad-image"},
        ],
        platform="weibo",
        successful_identities={"weibo:id:ok"},
        materialization_failures=[
            {
                "identity": "weibo:id:bad-image",
                "code": "image_too_large",
                "message": "oversized image",
                "attempts": 1,
            }
        ],
    )

    assert evidence["successful_candidate_count"] == 1
    assert evidence["skipped_candidate_count"] == 2
    failures = {
        item["platform_post_id"]: item
        for item in evidence["skipped_candidate_failures"]
    }
    assert failures["missing-detail"]["error_code"] == "repair_target_no_valid_output"
    assert failures["missing-detail"]["evidence_source"] == "repair_target_output_difference"
    assert failures["bad-image"]["failure_scope"] == "image"
    assert failures["bad-image"]["error_code"] == "image_too_large"
    assert all(item["terminal_for_run"] for item in failures.values())


def test_repair_fallback_makes_sparse_zhihu_detail_formally_valid() -> None:
    detail = {
        "content_id": "answer-1",
        "content_type": "answer",
        "content_url": "https://www.zhihu.com/question/1/answer/answer-1",
        "content_text": "完整正文",
        "content_detail_status": "detail_observed",
        "content_detail_source": "answer_detail",
        "image_list": ["https://example.test/body.jpg"],
        "author_platform_id": "hash-1",
        "user_nickname": "作***",
        "followers_count": 0,
        "followers_observed": False,
        "author_followers_source": "missing",
        "voteup_count": 1,
        "comment_count": 2,
        "collected_count": 3,
        "share_count": 0,
    }
    merged = mediacrawler.merge_repair_fallback_metadata(
        "zhihu",
        detail,
        {
            "published_at": "2026-07-13T12:00:00+08:00",
            "followers_count": 123,
            "author_followers_count": 123,
            "followers_observed": True,
            "author_followers_source": "author_page",
            "following_count": 9,
            "author_posts_count": 8,
            "author_verified": 1,
            "verified_text": "旧认证",
        },
    )

    validation = mediacrawler.validate_formal_record("zhihu", merged, set())
    assert merged["published_at"] == "2026-07-13T12:00:00+08:00"
    assert merged["content_detail_source"] == "answer_detail"
    assert merged["content_text"] == "完整正文"
    assert merged["followers_observed"] is False
    assert merged["author_followers_source"] == "missing"
    assert merged["followers_count"] == 0
    assert "author_followers_count" not in merged
    assert "following_count" not in merged
    assert "author_posts_count" not in merged
    assert "author_verified" not in merged
    assert "verified_text" not in merged
    assert validation["valid"] is False
    assert {
        "followers_not_observed",
        "missing_followers_source",
        "untrusted_followers_source",
    } & set(validation["reasons"])


REPAIR_FOLLOWER_SOURCES = {
    "weibo": "search_author",
    "douyin": "creator_profile",
    "zhihu": "author_page",
    "xhs": "creator_profile",
}


def _repair_follower_fallback(platform: str) -> dict[str, object]:
    # 与 load_post_repair_fallbacks 从旧 web_posts 行读出的粉丝字段形状一致。
    return {
        "published_at": "2026-07-13T12:00:00+08:00",
        "author_followers_count": 1234,
        "followers_count": 1234,
        "followers_observed": True,
        "author_followers_source": REPAIR_FOLLOWER_SOURCES[platform],
    }


@pytest.mark.parametrize("platform", ["weibo", "douyin", "zhihu", "xhs"])
def test_repair_fallback_keeps_fresh_observed_zero_followers(platform: str) -> None:
    fresh_source = REPAIR_FOLLOWER_SOURCES[platform]
    detail = {
        "followers_count": 0,
        "followers_observed": True,
        "author_followers_source": fresh_source,
    }

    merged = mediacrawler.merge_repair_fallback_metadata(
        platform,
        dict(detail),
        _repair_follower_fallback(platform),
    )

    validation = mediacrawler.validate_formal_record(platform, merged, set())
    assert merged["followers_count"] == 0
    assert merged.get("author_followers_count") is None
    assert merged["followers_observed"] is True
    assert merged["author_followers_source"] == fresh_source
    assert validation["followers_count"] == 0
    assert validation["followers_observed"] is True
    assert validation["followers_source"] == fresh_source
    assert "missing_followers_count" not in validation["reasons"]


@pytest.mark.parametrize("platform", ["weibo", "douyin", "zhihu", "xhs"])
@pytest.mark.parametrize("fresh_count", [None, "", 0])
def test_repair_fallback_restores_unobserved_followers_with_matching_source(
    platform: str,
    fresh_count: object,
) -> None:
    fallback = _repair_follower_fallback(platform)
    detail = {
        "followers_count": fresh_count,
        "followers_observed": False,
        "author_followers_source": "missing",
    }

    merged = mediacrawler.merge_repair_fallback_metadata(platform, dict(detail), fallback)
    if platform in {"zhihu", "weibo"}:
        # 知乎与微博未观察到的粉丝不得用旧行补上。微博简介、关注和作品数同样不回填。
        assert merged["followers_count"] == fresh_count
        assert merged["followers_observed"] is False
        assert merged["author_followers_source"] == "missing"
        assert "author_followers_count" not in merged
        assert merged["published_at"] == fallback["published_at"]
        return

    validation = mediacrawler.validate_formal_record(platform, merged, set())
    assert merged["followers_count"] == 1234
    assert merged["author_followers_count"] == 1234
    assert merged["followers_observed"] is True
    assert merged["author_followers_source"] == fallback["author_followers_source"]
    assert validation["followers_count"] == 1234
    assert validation["followers_observed"] is True
    assert validation["followers_source"] == fallback["author_followers_source"]
    assert not {
        "missing_followers_count",
        "followers_not_observed",
        "missing_followers_source",
        "untrusted_followers_source",
    } & set(validation["reasons"])


def test_partial_repair_separates_import_gate_from_full_completion(tmp_path: Path) -> None:
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts(platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        conn.execute("INSERT INTO web_posts VALUES ('weibo', 'ok', '')")
    record = {
        "note_id": "ok",
        "content": "微博完整正文",
        "content_detail_status": "detail_observed",
        "content_detail_source": "mobile_detail",
        "image_list_source": "mblog.pics",
        "image_list": ["https://wx1.sinaimg.cn/large/body.jpg"],
        "create_time": 1_700_000_000,
        "author_platform_id": "author-weibo",
        "nickname": "author",
        "followers_count": 10,
        "followers_observed": True,
        "author_followers_source": "search_author",
        "liked_count": 1,
        "comments_count": 2,
        "shared_count": 3,
    }
    jsonl_path = (
        tmp_path / "weibo" / "data" / "weibo" / "jsonl" / "detail_contents_1.jsonl"
    )
    jsonl_path.parent.mkdir(parents=True)
    jsonl_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    evidence = mediacrawler.post_repair_pagination_evidence(
        [{"platform_post_id": "ok"}, {"platform_post_id": "missing"}],
        platform="weibo",
        successful_identities={"weibo:id:ok"},
    )

    validation, selected = mediacrawler.collect_formal_records(
        {"records": [{"output": {"jsonl_files": [str(jsonl_path)]}}]},
        db_path=db_path,
        pagination_evidence=evidence,
        allowed_identities={"weibo:id:ok", "weibo:id:missing"},
        repair_mode=True,
    )

    assert len(selected) == 1
    assert validation["repair_import_met"] is True
    assert validation["completion_met"] is False
    assert validation["all_repair_targets_valid"] is False
    assert validation["skipped_candidate_count"] == 1
    assert validation["stop_reason"] == "repair_targets_partially_processed"


def test_build_child_command_disables_discovery_writes(tmp_path: Path) -> None:
    command = repair.build_child_command(
        platform="weibo",
        keyword="青岛旅游",
        db_path=tmp_path / "db.sqlite",
        output_root=tmp_path / "output",
        targets_path=tmp_path / "targets.json",
        headless=False,
    )

    assert command[command.index("--platforms") + 1] == "weibo"
    assert "--candidate-hard-limit" not in command
    assert "--target-new-posts" not in command
    assert "--completion-mode" not in command
    assert "--timeout-per-platform" not in command
    assert "--post-repair" in command
    assert "--repair-targets-file" in command
    assert "--download-images" in command
    assert "--no-checkpoint-write" in command
    assert "--headed" in command


def test_repaired_rows_requires_trusted_detail_and_local_images(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "repair.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE web_posts(
            id INTEGER PRIMARY KEY,
            platform_key TEXT,
            platform_post_id TEXT,
            content_text TEXT,
            raw_sample_json TEXT
        );
        CREATE TABLE web_post_images(
            id INTEGER PRIMARY KEY,
            web_post_id INTEGER,
            image_role TEXT,
            local_path TEXT,
            sha256 TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO web_posts(id, platform_key, platform_post_id, content_text, raw_sample_json)
        VALUES (1, 'weibo', 'post-1', '完整正文', ?)
        """,
        (
            json.dumps(
                {
                    "content_detail_status": "detail_observed",
                    "content_detail_source": "mobile_detail",
                }
            ),
        ),
    )
    conn.execute(
        """
        INSERT INTO web_post_images(web_post_id, image_role, local_path, sha256)
        VALUES (1, 'content', 'data/media/weibo/post-1/0.jpg', 'abc')
        """
    )
    conn.commit()

    statuses = repair.repaired_rows(
        conn,
        "weibo",
        ["post-1", *[f"missing-{index}" for index in range(1_100)]],
    )

    assert statuses["post-1"]["recovered"] is True
    conn.close()


def test_sqlite_backup_is_consistent_and_hashed(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    destination = tmp_path / "backups" / "source.sqlite"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('ok')")
        conn.commit()

    result = repair.create_sqlite_backup(source, destination)

    assert result["quick_check"] == ["ok"]
    assert result["foreign_key_error_count"] == 0
    assert len(result["sha256"]) == 64
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "ok"


def test_partial_generic_repair_can_commit_valid_subset() -> None:
    assert mediacrawler.repair_partial_child_execution_allowed(
        repair_mode=True,
        child_execution_ok=False,
        validation={"valid_total_count": 1},
        image_materialization={"complete": True},
        behavior_validation={"ok": True},
    ) is True
    assert mediacrawler.repair_partial_child_execution_allowed(
        repair_mode=True,
        child_execution_ok=False,
        runtime_blocked=True,
        validation={"valid_total_count": 1},
        image_materialization={"complete": True},
        behavior_validation={"ok": True},
    ) is False

    validation = {
        "repair_mode": True,
        "repair_import_met": True,
        "completion_met": False,
    }
    assert mediacrawler.formal_import_gate_met(validation) is True
    assert mediacrawler.formal_image_promotion_allowed(
        download_images=True,
        no_import=False,
        validation=validation,
    ) is True


def test_zero_progress_is_distinct_from_persistence_failure() -> None:
    assert repair.repair_persistence_failure_reason(
        recovered_ids=[],
        successful_child_summaries=[],
        persistence_checks=[],
    ) == "post_detail_repair_no_progress"
    assert repair.repair_persistence_failure_reason(
        recovered_ids=[],
        successful_child_summaries=[{"import_completion_met": True}],
        persistence_checks=[{"ok": True}],
    ) == "post_detail_repair_persistence_not_verified"
    assert repair.repair_persistence_failure_reason(
        recovered_ids=["post-1"],
        successful_child_summaries=[{"import_completion_met": True}],
        persistence_checks=[{"ok": True}],
    ) == ""


def test_clean_zero_output_repair_is_not_misclassified_as_runtime_failure() -> None:
    records = [
        {
            "platform": "douyin",
            "status": "failed",
            "run": {"returncode": 0, "timed_out": False},
            "failure_classification": {"failure_type": "success"},
            "output": {"content_records": 0},
        }
    ]

    assert mediacrawler.repair_candidate_execution_completed(records, ["douyin"])

    records[0]["failure_classification"] = {"failure_type": "rate_limited"}
    assert not mediacrawler.repair_candidate_execution_completed(records, ["douyin"])

    records[0]["failure_classification"] = {"failure_type": "success"}
    records[0]["run"].pop("returncode")
    assert not mediacrawler.repair_candidate_execution_completed(records, ["douyin"])


def test_repair_runtime_stop_reason_preserves_structured_blocker() -> None:
    records = [
        {
            "platform": "weibo",
            "failure_classification": {"failure_type": "rate_limited"},
        }
    ]

    assert mediacrawler.repair_runtime_stop_reason(records, ["weibo"]) == "rate_limited"
    assert mediacrawler.repair_runtime_stop_reason(records, ["douyin"]) == ""


def _repair_state(tmp_path: Path) -> repair.FrozenExecutionState:
    frozen = tmp_path / "frozen.txt"
    frozen.write_text("frozen\n", encoding="utf-8")
    return repair.FrozenExecutionState.create(
        tmp_path / "execution_state.json",
        run_id="douyin-interrupt",
        job_key="post_detail_repair:douyin",
        site_key="douyin",
        job_kind="post_detail_repair",
        plan={"run_id": "douyin-interrupt"},
        frozen_inputs=[frozen],
    )


def test_repair_child_grace_covers_middle_layer_cleanup() -> None:
    assert repair.REPAIR_CHILD_INTERRUPT_GRACE_SECONDS == (
        repair.PROCESS_CLEANUP_GRACE_SECONDS + repair.PROCESS_FINAL_REAP_SECONDS + 15.0
    )
    assert repair.REPAIR_CHILD_INTERRUPT_GRACE_SECONDS == 40.0


def test_run_repair_child_does_not_launch_after_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[str] = []

    def fake_popen(*_args: object, **_kwargs: object) -> object:
        launched.append("popen")
        raise AssertionError("child must not start after the first signal")

    monkeypatch.setattr(repair.subprocess, "Popen", fake_popen)

    result = repair.run_repair_child(
        ["child"],
        cwd=ROOT,
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "execution_state.json",
        interrupt_signal=lambda: signal.SIGINT,
    )

    assert launched == []
    assert result["launched"] is False
    assert result["interrupt_signum"] == signal.SIGINT
    assert result["returncode"] is None
    assert (tmp_path / "logs" / "command.txt").is_file()
    assert "avatar" not in (tmp_path / "logs" / "stdout.log").read_text(encoding="utf-8")


def test_run_repair_child_forwards_one_sigterm_and_redacts_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals_sent: list[int] = []
    seen = {"n": 0}

    class FakeProcess:
        pid = 5151
        returncode = None
        stdout = None
        stderr = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            signals_sent.append(signal.SIGTERM)

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            del timeout
            if signal.SIGTERM not in signals_sent:
                raise subprocess.TimeoutExpired(["child"], 0.01, output=b"", stderr=b"")
            self.returncode = -signal.SIGTERM
            return b"avatar_url=https://img.example/a.jpg\n", b"ok"

    def fake_popen(*_args: object, **kwargs: object) -> FakeProcess:
        assert kwargs["start_new_session"] is True
        assert kwargs.get("text") is not True
        assert kwargs["env"]["TRIPPOSTCOLLECT_EXECUTION_STATE_PATH"].endswith(
            "execution_state.json"
        )
        return FakeProcess()

    monkeypatch.setattr(repair.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        "trippostcollect.runtime.process.process_group_exists",
        lambda _pid: False,
    )

    def interrupt() -> int | None:
        seen["n"] += 1
        return signal.SIGINT if seen["n"] > 1 else None

    result = repair.run_repair_child(
        ["child"],
        cwd=ROOT,
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "execution_state.json",
        interrupt_signal=interrupt,
        grace_seconds=0,
        poll_seconds=0.01,
    )

    assert signals_sent == [signal.SIGTERM]
    assert result["interrupt_signum"] == signal.SIGINT
    assert result["forced_termination"] is False
    assert result["launched"] is True
    assert "img.example" not in result["stdout"]
    assert "author-avatar" in result["stdout"]
    logged = (tmp_path / "logs" / "stdout.log").read_text(encoding="utf-8")
    assert logged == result["stdout"]
    assert "img.example" not in logged


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_terminalize_repair_interrupt_fails_open_step(tmp_path: Path) -> None:
    state = _repair_state(tmp_path)
    state.begin("command_executed")

    error = repair.terminalize_repair_interrupt(
        state,
        error="runtime_failed:operator_interrupt:SIGINT",
        evidence={"interrupt": {"signum": signal.SIGINT}},
    )

    assert error is None
    payload = state.load()
    assert payload["status"] == "failed"
    assert payload["steps"]["command_executed"]["status"] == "failed"
    assert payload["steps"]["command_executed"]["error"] == (
        "runtime_failed:operator_interrupt:SIGINT"
    )
    assert payload["steps"]["task_finalized"]["status"] == "failed"


def test_terminalize_repair_interrupt_fails_later_in_progress_step(tmp_path: Path) -> None:
    state = _repair_state(tmp_path)
    state.begin("command_executed")
    state.complete("command_executed", evidence={"ok": True})
    state.begin("artifacts_verified")

    error = repair.terminalize_repair_interrupt(
        state,
        error="runtime_failed:operator_interrupt:SIGINT",
        evidence={"exception": "OSError: disk"},
    )

    assert error is None
    payload = state.load()
    assert payload["status"] == "failed"
    assert payload["steps"]["command_executed"]["status"] == "completed"
    assert payload["steps"]["artifacts_verified"]["status"] == "failed"
    assert payload["steps"]["artifacts_verified"]["evidence"]["exception"] == "OSError: disk"
    assert payload["steps"]["task_finalized"]["status"] == "failed"


def test_terminalize_repair_interrupt_leaves_completed_state(tmp_path: Path) -> None:
    state = _repair_state(tmp_path)
    payload = state.load()
    payload["status"] = "completed"
    state.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    error = repair.terminalize_repair_interrupt(
        state,
        error="runtime_failed:operator_interrupt:SIGINT",
        evidence={},
    )

    assert error is None
    assert state.load()["status"] == "completed"
    assert state.load()["steps"]["command_executed"]["status"] == "pending"


def test_run_repair_child_waits_without_batch_wall_clock_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 4242
        returncode = None

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            assert timeout == repair.REPAIR_CHILD_POLL_SECONDS
            self.returncode = 0
            return b"out", b"err"

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        del command
        assert kwargs["start_new_session"] is True
        assert kwargs.get("text") is not True
        return FakeProcess()

    monkeypatch.setattr(repair.subprocess, "Popen", fake_popen)

    result = repair.run_repair_child(
        ["child"],
        cwd=ROOT,
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "execution_state.json",
        interrupt_signal=lambda: None,
        poll_seconds=repair.REPAIR_CHILD_POLL_SECONDS,
    )

    assert result == {"returncode": 0, "stdout": "out", "stderr": "err"}


@pytest.mark.parametrize(
    ("error", "strict"),
    [
        ("missing_child_summary_exit_2", False),
        ("runtime_failed", False),
        ("repair_no_valid_detail", False),
        ("login_required", True),
        ("captcha_detected", True),
        ("rate_limited", True),
        ("platform_security_limit", True),
        ("policy_blocked", True),
        ("blocked_or_forbidden", True),
        ("runtime_permission_error", True),
        ("browser_launch_failed", True),
        ("browser_target_closed", True),
        ("behavior_evidence_failed", True),
        ("crawl_policy_evidence_failed", True),
        ("sqlite_import_failed", True),
    ],
)
def test_strict_batch_blocker_only_stops_unattended_repair(error: str, strict: bool) -> None:
    assert repair._strict_batch_blocker(error) is strict


@pytest.mark.parametrize(
    "stop_reason",
    [
        "rate_limited",
        "platform_security_limit",
        "policy_blocked",
        "blocked_or_forbidden",
        "runtime_permission_error",
        "browser_launch_failed",
        "browser_target_closed",
    ],
)
def test_fatal_child_failure_preserves_structured_stop_reason(stop_reason: str) -> None:
    assert (
        repair._fatal_child_failure(
            {"formal_validation": {"stop_reason": stop_reason}}
        )
        == stop_reason
    )


def _redirect_repair_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_OUTPUT", tmp_path / "out")
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_RUNTIME_ROOT", tmp_path / "rt")
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_BACKUP_ROOT", tmp_path / "bk")
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_PAUSE_ROOT", tmp_path / "rt" / "pause")


def _seed_pending_douyin(db_path: Path) -> None:
    from trippostcollect.db.bootstrap import bootstrap_database

    bootstrap_database(db_path, sync_jobs=False)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO web_posts(
                platform_key, platform_post_id, author_platform_id, source_type, source_url,
                canonical_url, keyword, captured_at, raw_sample_json
            ) VALUES (
                'weibo', '123', '88', 'search', 'https://weibo.com/88/123',
                'https://weibo.com/88/123', '青岛崂山',
                '2026-01-01T00:00:00+08:00', '{}'
            )
            """
        )
        conn.commit()


def _repair_argv(db_path: Path) -> list[str]:
    return [
        "repair_post_details.py",
        "--platform",
        "weibo",
        "--db",
        str(db_path),
        "--keyword",
        "青岛旅游",
        "--batch-size",
        "1",
        "--headless",
    ]


def _summary_and_state(tmp_path: Path) -> tuple[dict, dict]:
    summaries = list((tmp_path / "out").glob("*/run_summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8"))
    state = json.loads(Path(summary["execution_state"]).read_text(encoding="utf-8"))
    return summary, state


def test_main_signal_during_successful_verification_keeps_completed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _repair_argv(db_path))
    child_summary = tmp_path / "child-summary.json"
    child_summary.write_text(
        json.dumps(
            {
                "import_completion_met": True,
                "formal_validation": {"valid_total_count": 1, "skipped_candidate_failures": []},
                "import_result": {},
            }
        ),
        encoding="utf-8",
    )

    def fake_child(*_args: object, **_kwargs: object) -> dict:
        return {
            "returncode": 0,
            "stdout": json.dumps({"summary": str(child_summary)}),
            "stderr": "",
        }

    def fake_artifacts(*_args: object, **_kwargs: object) -> dict:
        os.kill(os.getpid(), signal.SIGINT)
        return {"ok": True}

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    monkeypatch.setattr(repair, "verify_image_artifacts", fake_artifacts)
    monkeypatch.setattr(repair, "verify_image_persistence", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(
        repair,
        "repaired_rows",
        lambda _conn, _platform, ids: {
            ids[0]: {
                "recovered": True,
                "detail_status": "detail_observed",
                "detail_source": "aweme_detail",
                "content_images": 1,
                "persisted_images": 1,
            }
        },
    )
    monkeypatch.setattr(repair, "pending_count", lambda *_a, **_k: 0)
    monkeypatch.setattr(repair, "waived_pending_count", lambda *_a, **_k: 0)

    exit_code = repair.main()
    summary, state = _summary_and_state(tmp_path)

    assert exit_code == 128 + signal.SIGINT
    assert state["status"] == "completed"
    assert summary["status"] == "completed"
    assert summary["error"] == ""
    assert summary["interrupt"]["signal"] == "SIGINT"
    assert summary["interrupt"]["exit_code"] == exit_code


def test_main_signal_during_verification_error_finalizes_open_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _repair_argv(db_path))
    child_summary = tmp_path / "child-summary.json"
    child_summary.write_text(
        json.dumps(
            {
                "import_completion_met": True,
                "formal_validation": {"valid_total_count": 1, "skipped_candidate_failures": []},
                "import_result": {},
            }
        ),
        encoding="utf-8",
    )

    def fake_child(*_args: object, **_kwargs: object) -> dict:
        return {
            "returncode": 0,
            "stdout": json.dumps({"summary": str(child_summary)}),
            "stderr": "",
        }

    def fake_artifacts(*_args: object, **_kwargs: object) -> dict:
        os.kill(os.getpid(), signal.SIGINT)
        raise OSError("disk")

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    monkeypatch.setattr(repair, "verify_image_artifacts", fake_artifacts)

    exit_code = repair.main()
    summary, state = _summary_and_state(tmp_path)

    assert exit_code == 128 + signal.SIGINT
    assert state["status"] == "failed"
    assert state["steps"]["command_executed"]["status"] == "completed"
    assert state["steps"]["artifacts_verified"]["status"] == "failed"
    assert state["steps"]["artifacts_verified"]["evidence"]["interrupt"]["exception"] == (
        "OSError: disk"
    )
    assert state["steps"]["task_finalized"]["status"] == "failed"
    assert summary["status"] == "failed"
    assert summary["error"] == "runtime_failed:operator_interrupt:SIGINT"
    assert summary["interrupt"]["exception"] == "OSError: disk"
    assert (tmp_path / "out").joinpath(summary["run_id"], "run_summary.json").is_file()


def test_main_stuck_worker_is_killed_after_middle_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _repair_argv(db_path))
    monkeypatch.setattr(repair, "REPAIR_CHILD_INTERRUPT_GRACE_SECONDS", 0.3)
    worker_pid_path = tmp_path / "worker.pid"
    ready = tmp_path / "middle-ready"
    middle = tmp_path / "middle.py"
    middle.write_text(
        "\n".join(
            [
                "import os, signal, subprocess, sys, time",
                "from pathlib import Path",
                "from trippostcollect.runtime.process import record_child_process_group",
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                "worker = subprocess.Popen(",
                "    [sys.executable, '-c',",
                "     'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '",
                "     'print(\"worker-ready\", flush=True); time.sleep(120)'],",
                "    start_new_session=True,",
                "    stdout=subprocess.PIPE,",
                "    stderr=subprocess.DEVNULL,",
                "    text=True,",
                ")",
                "assert worker.stdout is not None",
                "assert worker.stdout.readline() == 'worker-ready\\n'",
                "record_child_process_group(worker)",
                f"Path({str(worker_pid_path)!r}).write_text(str(worker.pid), encoding='utf-8')",
                f"Path({str(ready)!r}).write_text('1', encoding='utf-8')",
                "time.sleep(120)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        repair,
        "build_child_command",
        lambda **_kwargs: [sys.executable, str(middle)],
    )

    def interrupt_after_ready() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=interrupt_after_ready, daemon=True)
    thread.start()
    try:
        exit_code = repair.main()
    finally:
        thread.join(timeout=2)
        if worker_pid_path.is_file():
            worker_pid = int(worker_pid_path.read_text(encoding="utf-8"))
            if _process_alive(worker_pid):
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    summary, state = _summary_and_state(tmp_path)
    worker_pid = int(worker_pid_path.read_text(encoding="utf-8"))
    assert exit_code == 128 + signal.SIGINT
    assert summary["forced_termination"] is True
    assert summary["batch_results"][0]["killed_child_process_groups"]
    assert state["status"] == "failed"
    assert state["steps"]["task_finalized"]["status"] == "failed"
    assert not _process_alive(worker_pid)


def test_main_sighup_still_writes_summary_and_exits_129(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del monkeypatch
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    ready = tmp_path / "child-ready"
    child_code = (
        "from pathlib import Path; import time; "
        f"Path({str(ready)!r}).write_text('1'); time.sleep(120)"
    )
    driver = tmp_path / "drive.py"
    driver.write_text(
        "\n".join(
            [
                "import sys",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(SCRIPTS)!r})",
                "import repair_post_details as repair",
                f"repair.POST_DETAIL_REPAIR_OUTPUT = Path({str(tmp_path / 'out')!r})",
                f"repair.POST_DETAIL_REPAIR_RUNTIME_ROOT = Path({str(tmp_path / 'rt')!r})",
                f"repair.POST_DETAIL_REPAIR_BACKUP_ROOT = Path({str(tmp_path / 'bk')!r})",
                "repair.REPAIR_CHILD_INTERRUPT_GRACE_SECONDS = 0.3",
                "sys.argv = [",
                "    'repair_post_details.py', '--platform', 'weibo', '--db',",
                f"    {str(db_path)!r}, '--keyword', '青岛旅游',",
                "    '--batch-size', '1', '--headless',",
                "]",
                "def build_child_command(**_kwargs):",
                f"    return [sys.executable, '-c', {child_code!r}]",
                "repair.build_child_command = build_child_command",
                "raise SystemExit(repair.main())",
                "",
            ]
        ),
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(driver)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not ready.exists():
            if process.poll() is not None:
                break
            time.sleep(0.02)
        assert ready.exists()
        os.kill(process.pid, signal.SIGHUP)
        process.stdout.close()
        process.stderr.close()
        exit_code = process.wait(timeout=15)
    finally:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    summary, _state = _summary_and_state(tmp_path)
    assert exit_code == 128 + signal.SIGHUP
    assert summary["interrupt"]["signal"] == "SIGHUP"
    assert summary["status"] == "failed"


def _successful_child_stdout(summary_path: Path) -> str:
    summary_path.write_text(
        json.dumps(
            {
                "import_completion_met": True,
                "formal_validation": {
                    "stop_reason": "login_required",
                    "valid_total_count": 0,
                    "skipped_candidate_failures": [],
                },
                "import_result": {},
            }
        ),
        encoding="utf-8",
    )
    return json.dumps({"summary": str(summary_path)})


def test_main_keeps_login_required_when_signal_arrives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _repair_argv(db_path))
    stdout = _successful_child_stdout(tmp_path / "child-summary.json")

    def fake_child(*_args: object, **_kwargs: object) -> dict:
        os.kill(os.getpid(), signal.SIGINT)
        return {"returncode": 2, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    exit_code = repair.main()
    summary, state = _summary_and_state(tmp_path)

    assert exit_code == 128 + signal.SIGINT
    assert summary["error"] == "batch_1:login_required"
    assert summary["interrupt"]["signal"] == "SIGINT"
    assert state["steps"]["command_executed"]["error"] == "batch_1:login_required"
    assert "operator_interrupt" not in state["steps"]["command_executed"]["error"]


def test_main_keeps_image_failure_when_signal_arrives_during_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _repair_argv(db_path))
    child_summary = tmp_path / "child-summary.json"
    child_summary.write_text(
        json.dumps(
            {
                "import_completion_met": True,
                "formal_validation": {"valid_total_count": 1, "skipped_candidate_failures": []},
                "import_result": {},
            }
        ),
        encoding="utf-8",
    )

    def fake_child(*_args: object, **_kwargs: object) -> dict:
        return {
            "returncode": 0,
            "stdout": json.dumps({"summary": str(child_summary)}),
            "stderr": "",
        }

    def fake_artifacts(*_args: object, **_kwargs: object) -> dict:
        os.kill(os.getpid(), signal.SIGINT)
        return {"ok": False}

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    monkeypatch.setattr(repair, "verify_image_artifacts", fake_artifacts)
    exit_code = repair.main()
    summary, state = _summary_and_state(tmp_path)

    assert exit_code == 128 + signal.SIGINT
    assert summary["error"] == "post_detail_repair_image_artifacts_incomplete"
    assert summary["interrupt"]["reason"] == "operator_interrupt"
    assert state["steps"]["artifacts_verified"]["error"] == (
        "post_detail_repair_image_artifacts_incomplete"
    )
    assert state["steps"]["task_finalized"]["error"] == (
        "post_detail_repair_image_artifacts_incomplete"
    )


def test_main_locked_database_after_interrupt_still_writes_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _repair_argv(db_path))

    def fake_child(*_args: object, **_kwargs: object) -> dict:
        os.kill(os.getpid(), signal.SIGINT)
        return {
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "interrupt_signum": signal.SIGINT,
            "forced_termination": False,
            "launched": False,
            "killed_groups": [],
            "logs": {},
        }

    def locked(*_args: object, **_kwargs: object) -> dict:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    monkeypatch.setattr(repair, "repaired_rows", locked)
    exit_code = repair.main()
    summary, state = _summary_and_state(tmp_path)

    assert exit_code == 128 + signal.SIGINT
    assert summary["error"] == "runtime_failed:operator_interrupt:SIGINT"
    assert summary["tail_error"] == "OperationalError: database is locked"
    assert summary["interrupt"]["signal"] == "SIGINT"
    assert state["status"] == "failed"
    assert state["steps"]["task_finalized"]["status"] == "failed"


def test_default_grace_is_read_when_main_calls_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _repair_argv(db_path))
    seen: dict[str, object] = {}

    def fake_child(*_args: object, **kwargs: object) -> dict:
        seen["grace"] = kwargs.get("grace_seconds")
        os.kill(os.getpid(), signal.SIGINT)
        return {
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "interrupt_signum": signal.SIGINT,
            "forced_termination": False,
            "launched": False,
            "killed_groups": [],
            "logs": {},
        }

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    repair.main()

    assert "grace_seconds" not in seen or seen["grace"] is None
    assert seen["grace"] is None


def test_omitted_grace_uses_constant_at_call_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, float] = {}

    def fake_terminate(proc: object, *, grace_seconds: float) -> tuple[bytes, bytes, bool]:
        seen["grace"] = grace_seconds
        proc.returncode = -signal.SIGINT  # type: ignore[attr-defined]
        return b"", b"", False

    class FakeProcess:
        pid = 77
        returncode = None

        def poll(self) -> int | None:
            return self.returncode

    calls = {"n": 0}

    def interrupt() -> int | None:
        calls["n"] += 1
        return signal.SIGINT if calls["n"] > 1 else None

    monkeypatch.setattr(repair, "terminate_managed_process", fake_terminate)
    monkeypatch.setattr(repair.subprocess, "Popen", lambda *_a, **_k: FakeProcess())
    monkeypatch.setattr(repair, "REPAIR_CHILD_INTERRUPT_GRACE_SECONDS", 40.0)
    repair.run_repair_child(
        ["child"],
        cwd=ROOT,
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "execution_state.json",
        interrupt_signal=interrupt,
        poll_seconds=0.01,
    )

    assert seen["grace"] == 40.0


def _arm_completed_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    child_stdout: str,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _repair_argv(db_path))

    def fake_child(*_args: object, **_kwargs: object) -> dict:
        return {"returncode": 0, "stdout": child_stdout, "stderr": ""}

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    monkeypatch.setattr(repair, "verify_image_persistence", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(repair, "pending_count", lambda *_a, **_k: 0)
    monkeypatch.setattr(repair, "waived_pending_count", lambda *_a, **_k: 0)


def test_main_signal_after_summary_write_only_changes_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _ok_child_summary(tmp_path / "child-summary.json")
    _arm_completed_repair(tmp_path, monkeypatch, child_stdout=stdout)
    monkeypatch.setattr(repair, "verify_image_artifacts", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(
        repair,
        "repaired_rows",
        lambda _conn, _platform, ids: _recovered(ids),
    )
    original = repair.write_json
    snapshot: dict[str, bytes] = {}

    def write_then_signal(path: Path, value: object) -> Path:
        written = original(path, value)
        if path.name == "run_summary.json":
            snapshot["state"] = Path(value["execution_state"]).read_bytes()  # type: ignore[index]
            os.kill(os.getpid(), signal.SIGINT)
        return written

    monkeypatch.setattr(repair, "write_json", write_then_signal)
    exit_code = repair.main()
    summary, state_path_payload = _summary_and_state(tmp_path)
    state_path = Path(summary["execution_state"])

    assert exit_code == 130
    assert "interrupt" not in summary
    assert state_path.read_bytes() == snapshot["state"]
    assert state_path_payload["status"] == "completed"


def test_main_signal_and_query_failure_in_final_stats_uses_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _ok_child_summary(tmp_path / "child-summary.json")
    _arm_completed_repair(tmp_path, monkeypatch, child_stdout=stdout)
    monkeypatch.setattr(repair, "verify_image_artifacts", lambda *_a, **_k: {"ok": True})
    calls = {"n": 0}

    def rows(_conn: object, _platform: str, ids: list[str]) -> dict:
        calls["n"] += 1
        if calls["n"] > 1:
            os.kill(os.getpid(), signal.SIGINT)
            raise sqlite3.OperationalError("database is locked")
        return _recovered(ids)

    monkeypatch.setattr(repair, "repaired_rows", rows)
    exit_code = repair.main()
    summary, state = _summary_and_state(tmp_path)

    assert exit_code == 130
    assert summary["interrupt"]["signal"] == "SIGINT"
    assert summary["tail_error"] == "OperationalError: database is locked"
    assert "recovered_count" not in summary
    assert state["status"] == "completed"


def test_main_keyerror_without_signal_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _ok_child_summary(tmp_path / "child-summary.json")
    _arm_completed_repair(tmp_path, monkeypatch, child_stdout=stdout)

    def fake_artifacts(*_args: object, **_kwargs: object) -> dict:
        raise KeyError("verification")

    monkeypatch.setattr(repair, "verify_image_artifacts", fake_artifacts)

    with pytest.raises(KeyError, match="verification"):
        repair.main()


def test_main_oserror_without_signal_records_exception_and_exits_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _ok_child_summary(tmp_path / "child-summary.json")
    _arm_completed_repair(tmp_path, monkeypatch, child_stdout=stdout)

    def fake_artifacts(*_args: object, **_kwargs: object) -> dict:
        raise OSError("disk")

    monkeypatch.setattr(repair, "verify_image_artifacts", fake_artifacts)
    exit_code = repair.main()
    summary, state = _summary_and_state(tmp_path)

    assert exit_code == 2
    assert summary["error"].startswith("post_detail_repair_exception:OSError")
    assert state["steps"]["artifacts_verified"]["status"] == "failed"
    assert state["steps"]["artifacts_verified"]["error"].startswith(
        "post_detail_repair_exception:OSError"
    )


def test_main_stderr_only_avatar_is_redacted_when_log_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _repair_argv(db_path))
    ready = tmp_path / "child-ready"
    avatar = "https://img.example/stderr-only-face.jpg"
    child_code = (
        "import sys, time; from pathlib import Path; "
        f"print('avatar_url={avatar}', file=sys.stderr, flush=True); "
        f"Path({str(ready)!r}).write_text('1'); time.sleep(120)"
    )
    monkeypatch.setattr(
        repair,
        "build_child_command",
        lambda **_kwargs: [sys.executable, "-c", child_code],
    )

    def fail_logs(*_args: object, **_kwargs: object) -> dict:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(repair, "write_command_logs", fail_logs)

    def interrupt_after_ready() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=interrupt_after_ready, daemon=True)
    thread.start()
    try:
        exit_code = repair.main()
    finally:
        thread.join(timeout=2)

    summary, _state = _summary_and_state(tmp_path)
    rendered = json.dumps(summary, ensure_ascii=False)
    batch = summary["batch_results"][0]
    assert exit_code == 130
    assert avatar not in rendered
    assert batch["stdout_tail"] == batch["stderr_tail"]
    assert batch["stdout_tail"].startswith(
        "[TripPostCollect redacted child output containing author-avatar fields]"
    )
    assert "stdout_lines=0" in batch["stdout_tail"]
    assert "stderr_lines=1" in batch["stderr_tail"]


def _ok_child_summary(path: Path) -> str:
    path.write_text(
        json.dumps(
            {
                "import_completion_met": True,
                "formal_validation": {"valid_total_count": 1, "skipped_candidate_failures": []},
                "import_result": {},
            }
        ),
        encoding="utf-8",
    )
    return json.dumps({"summary": str(path)})


def _recovered(ids: list[str]) -> dict[str, dict[str, object]]:
    return {
        ids[0]: {
            "recovered": True,
            "detail_status": "detail_observed",
            "detail_source": "aweme_detail",
            "content_images": 1,
            "persisted_images": 1,
        }
    }


def test_main_keyerror_with_latched_signal_finalizes_and_exits_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _repair_argv(db_path))
    stdout = _ok_child_summary(tmp_path / "child-summary.json")

    def fake_child(*_args: object, **_kwargs: object) -> dict:
        return {"returncode": 0, "stdout": stdout, "stderr": ""}

    def fake_artifacts(*_args: object, **_kwargs: object) -> dict:
        os.kill(os.getpid(), signal.SIGINT)
        raise KeyError("verification")

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    monkeypatch.setattr(repair, "verify_image_artifacts", fake_artifacts)
    exit_code = repair.main()
    summary, state = _summary_and_state(tmp_path)

    assert exit_code == 130
    assert state["status"] == "failed"
    assert state["steps"]["task_finalized"]["status"] == "failed"
    assert summary["interrupt"]["signal"] == "SIGINT"
    assert "KeyError" in summary["interrupt"]["exception"]


def test_main_completed_tail_failure_keeps_error_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _repair_argv(db_path))
    stdout = _ok_child_summary(tmp_path / "child-summary.json")
    calls = {"n": 0}

    def fake_child(*_args: object, **_kwargs: object) -> dict:
        return {"returncode": 0, "stdout": stdout, "stderr": ""}

    def fake_artifacts(*_args: object, **_kwargs: object) -> dict:
        os.kill(os.getpid(), signal.SIGINT)
        return {"ok": True}

    def rows(_conn: object, _platform: str, ids: list[str]) -> dict:
        calls["n"] += 1
        if calls["n"] > 1:
            raise sqlite3.OperationalError("database is locked")
        return _recovered(ids)

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    monkeypatch.setattr(repair, "verify_image_artifacts", fake_artifacts)
    monkeypatch.setattr(repair, "verify_image_persistence", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(repair, "repaired_rows", rows)
    monkeypatch.setattr(repair, "pending_count", lambda *_a, **_k: 0)
    monkeypatch.setattr(repair, "waived_pending_count", lambda *_a, **_k: 0)
    exit_code = repair.main()
    summary, state = _summary_and_state(tmp_path)

    assert exit_code == 130
    assert summary["status"] == "completed"
    assert summary["error"] == ""
    assert summary["interrupt"]["signal"] == "SIGINT"
    assert summary["tail_error"] == "OperationalError: database is locked"
    assert state["status"] == "completed"


def test_main_signal_during_final_stats_sets_exit_code_without_rewriting_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _repair_argv(db_path))
    stdout = _ok_child_summary(tmp_path / "child-summary.json")
    calls = {"n": 0}

    def fake_child(*_args: object, **_kwargs: object) -> dict:
        return {"returncode": 0, "stdout": stdout, "stderr": ""}

    def rows(_conn: object, _platform: str, ids: list[str]) -> dict:
        calls["n"] += 1
        if calls["n"] > 1:
            os.kill(os.getpid(), signal.SIGINT)
        return _recovered(ids)

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    monkeypatch.setattr(repair, "verify_image_artifacts", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(repair, "verify_image_persistence", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(repair, "repaired_rows", rows)
    monkeypatch.setattr(repair, "pending_count", lambda *_a, **_k: 0)
    monkeypatch.setattr(repair, "waived_pending_count", lambda *_a, **_k: 0)
    exit_code = repair.main()
    summary, state = _summary_and_state(tmp_path)

    assert exit_code == 130
    assert summary["status"] == "completed"
    assert summary["error"] == ""
    assert summary["interrupt"]["signal"] == "SIGINT"
    assert state["status"] == "completed"
    assert state["steps"]["task_finalized"]["status"] == "completed"


def test_main_log_write_failure_keeps_redacted_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _repair_argv(db_path))
    ready = tmp_path / "child-ready"
    avatar = "https://img.example/secret-face.jpg"
    child_code = (
        "from pathlib import Path; import time; "
        f"print('avatar_url={avatar}', flush=True); "
        f"Path({str(ready)!r}).write_text('1'); time.sleep(120)"
    )
    monkeypatch.setattr(
        repair,
        "build_child_command",
        lambda **_kwargs: [sys.executable, "-c", child_code],
    )

    def fail_logs(*_args: object, **_kwargs: object) -> dict:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(repair, "write_command_logs", fail_logs)

    def interrupt_after_ready() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=interrupt_after_ready, daemon=True)
    thread.start()
    try:
        exit_code = repair.main()
    finally:
        thread.join(timeout=2)

    summary, _state = _summary_and_state(tmp_path)
    rendered = json.dumps(summary, ensure_ascii=False)
    batch = summary["batch_results"][0]
    assert exit_code == 130
    assert summary["forced_termination"] is False
    assert "killed_child_process_groups" in batch
    assert batch["log_error"].startswith("OSError:")
    assert avatar not in rendered
    assert "author-avatar" in batch["stdout_tail"]


def test_main_tail_error_without_signal_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_pending_douyin(db_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _repair_argv(db_path))
    stdout = _ok_child_summary(tmp_path / "child-summary.json")
    calls = {"n": 0}

    def fake_child(*_args: object, **_kwargs: object) -> dict:
        return {"returncode": 0, "stdout": stdout, "stderr": ""}

    def rows(_conn: object, _platform: str, ids: list[str]) -> dict:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("tail-must-propagate")
        return _recovered(ids)

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    monkeypatch.setattr(repair, "verify_image_artifacts", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(repair, "verify_image_persistence", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(repair, "repaired_rows", rows)
    monkeypatch.setattr(repair, "pending_count", lambda *_a, **_k: 0)
    monkeypatch.setattr(repair, "waived_pending_count", lambda *_a, **_k: 0)

    with pytest.raises(RuntimeError, match="tail-must-propagate"):
        repair.main()


def _seed_refresh_posts(db_path: Path, post_ids: list[str]) -> None:
    from trippostcollect.db.bootstrap import bootstrap_database

    bootstrap_database(db_path, sync_jobs=False)
    with sqlite3.connect(db_path) as conn:
        for post_id in post_ids:
            url = f"https://www.zhihu.com/question/10/answer/{post_id}"
            conn.execute(
                """
                INSERT INTO web_posts(
                    platform_key, platform_post_id, source_type, source_url,
                    canonical_url, captured_at, keyword, content_text,
                    raw_sample_json, post_images_count
                ) VALUES (
                    'zhihu', ?, 'import', ?, ?, '2026-11-01T00:00:00+08:00',
                    '青岛旅游', '青岛正文', ?, 0
                )
                """,
                (
                    post_id,
                    url,
                    url,
                    json.dumps(
                        {
                            "content_detail_status": "detail_observed",
                            "content_detail_source": "answer_detail",
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        conn.commit()


def _refresh_argv(db_path: Path) -> list[str]:
    return [
        "repair_post_details.py",
        "--platform",
        "zhihu",
        "--db",
        str(db_path),
        "--keyword",
        "青岛旅游",
        "--batch-size",
        "1",
        "--refresh-all",
        "--headless",
    ]


def _target_post_id(command: list[str]) -> str:
    path = Path(command[command.index("--repair-targets-file") + 1])
    targets = json.loads(path.read_text(encoding="utf-8"))
    return str(targets[0]["platform_post_id"])


def _write_child_summary(path: Path, payload: dict) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return json.dumps({"summary": str(path)})


def _mark_refreshed(db_path: Path, post_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE web_posts
            SET content_text=?, raw_sample_json=?
            WHERE platform_post_id=?
            """,
            (
                "刷新后的正文",
                json.dumps(
                    {
                        "content_detail_status": "detail_observed",
                        "content_detail_source": "answer_detail",
                        "author_followers_source": "author_page",
                        "author_page_badges": {},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                post_id,
            ),
        )
        conn.commit()


def test_refresh_stops_after_policy_refusal_and_leaves_later_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_refresh_posts(db_path, ["row-a", "row-b", "row-c"])
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _refresh_argv(db_path))
    launched: list[str] = []

    def fake_child(command: list[str], **_kwargs: object) -> dict:
        post_id = _target_post_id(command)
        launched.append(post_id)
        if post_id == "row-a":
            _mark_refreshed(db_path, post_id)
            stdout = _write_child_summary(
                tmp_path / "child-a.json",
                {
                    "import_completion_met": True,
                    "formal_validation": {
                        "valid_total_count": 1,
                        "skipped_candidate_failures": [],
                    },
                    "import_result": {"processed_rows": 1},
                    "records": [{
                        "policy_events": [{"allowed": True, "reason": ""}],
                    }],
                },
            )
            return {"returncode": 0, "stdout": stdout, "stderr": "batch finished"}
        assert post_id == "row-b"
        stdout = _write_child_summary(
            tmp_path / "child-b.json",
            {
                "import_completion_met": False,
                "formal_validation": {
                    "stop_reason": "runtime_failed",
                    "valid_total_count": 0,
                    "skipped_candidate_failures": [{
                        "platform_post_id": "row-b",
                        "error_code": "repair_target_no_valid_output",
                        "detail": "explicit repair target produced no formally valid persisted detail record",
                    }],
                },
                "records": [{
                    "status": "policy_blocked",
                    "policy_events": [{
                        "allowed": False,
                        "reason": "max_requests_per_session",
                        "cooldown_until": "2026-11-16T20:00:00+00:00",
                        "wait_seconds": 3600,
                    }],
                }],
            },
        )
        return {"returncode": 2, "stdout": stdout, "stderr": "worker refused before navigation"}

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    exit_code = repair.main()
    summary, state = _summary_and_state(tmp_path)
    by_id = {item["platform_post_id"]: item for item in summary["target_results"]}

    assert launched == ["row-a", "row-b"]
    assert exit_code == 2
    assert summary["status"] == "throttle_wait"
    assert summary["error"] == "batch_2:throttle_wait"
    assert summary["throttle"]["reason"] == "max_requests_per_session"
    assert summary["throttle"]["cooldown_until"] == "2026-11-16T20:00:00+00:00"
    assert summary["candidate_failure_count"] == 0
    assert summary["not_attempted_count"] == 1
    assert summary["batch_runtime_failed_count"] == 1
    assert by_id["row-a"]["outcome"] == "refreshed"
    assert by_id["row-b"]["outcome"] == "batch_runtime_failed"
    assert by_id["row-b"]["reason"] == "throttle_wait"
    assert by_id["row-c"]["outcome"] == "not_attempted"
    assert state["steps"]["command_executed"]["error"] == "batch_2:throttle_wait"
    with sqlite3.connect(db_path) as conn:
        badges = {
            row[0]: row[1]
            for row in conn.execute(
                """
                SELECT platform_post_id, json_type(raw_sample_json, '$.author_page_badges')
                FROM web_posts
                ORDER BY id
                """
            )
        }
    assert badges["row-a"] == "object"
    assert badges["row-b"] is None
    assert badges["row-c"] is None


def test_batch_runtime_failure_is_not_a_candidate_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_refresh_posts(db_path, ["row-a", "row-b", "row-c"])
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _refresh_argv(db_path))
    launched: list[str] = []

    def fake_child(command: list[str], **_kwargs: object) -> dict:
        post_id = _target_post_id(command)
        launched.append(post_id)
        if post_id == "row-a":
            stdout = _write_child_summary(
                tmp_path / "child-a.json",
                {
                    "import_completion_met": False,
                    "formal_validation": {
                        "stop_reason": "runtime_failed",
                        "stop_detail": "ERR_NETWORK_CHANGED",
                        "valid_total_count": 0,
                        "skipped_candidate_failures": [{
                            "platform_post_id": "row-a",
                            "error_code": "repair_target_no_valid_output",
                            "detail": "browser context closed",
                        }],
                    },
                },
            )
            return {"returncode": 2, "stdout": stdout, "stderr": "ERR_NETWORK_CHANGED"}
        if post_id == "row-b":
            stdout = _write_child_summary(
                tmp_path / "child-b.json",
                {
                    "import_completion_met": False,
                    "formal_validation": {
                        "valid_total_count": 0,
                        "skipped_candidate_failures": [{
                            "platform_post_id": "row-b",
                            "error_code": "detail_entity_missing",
                            "detail": "page state has no answer entity",
                        }],
                    },
                },
            )
            return {"returncode": 2, "stdout": stdout, "stderr": ""}
        _mark_refreshed(db_path, post_id)
        stdout = _write_child_summary(
            tmp_path / "child-c.json",
            {
                "import_completion_met": True,
                "formal_validation": {
                    "valid_total_count": 1,
                    "skipped_candidate_failures": [],
                },
                "import_result": {"processed_rows": 1},
            },
        )
        return {"returncode": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    monkeypatch.setattr(repair, "verify_image_artifacts", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(repair, "verify_image_persistence", lambda *_a, **_k: {"ok": True})
    exit_code = repair.main()
    summary, _state = _summary_and_state(tmp_path)
    by_id = {item["platform_post_id"]: item for item in summary["target_results"]}

    assert launched == ["row-a", "row-b", "row-c"]
    assert exit_code == 0
    assert summary["status"] == "completed_with_remaining"
    assert summary["candidate_failure_count"] == 1
    assert summary["candidate_failures_sample"][0]["platform_post_id"] == "row-b"
    assert summary["batch_runtime_failed_count"] == 1
    assert summary["not_attempted_count"] == 0
    assert by_id["row-a"]["outcome"] == "batch_runtime_failed"
    assert by_id["row-a"]["reason"] == "runtime_failed"
    assert by_id["row-b"]["outcome"] == "candidate_failed"
    assert by_id["row-b"]["reason"] == "detail_entity_missing"
    assert by_id["row-c"]["outcome"] == "refreshed"


def _insert_refresh_post(
    conn: sqlite3.Connection,
    *,
    post_id: str,
    platform: str = "weibo",
    topic_relevant: int = 1,
    raw: dict | None = None,
    author_id: str = "88",
    keyword: str = "青岛旅游",
    content: str = "旧青岛正文",
    likes: int = 1,
    comments: int = 2,
    shares: int = 3,
    author_name: str = "旧昵称",
    followers: int = 3,
    verified: int | None = 1,
    verified_text: str | None = "旧认证",
    image_url: str | None = "https://wx1.sinaimg.cn/large/old.jpg",
) -> int:
    if platform == "zhihu":
        url = f"https://www.zhihu.com/question/1/answer/{post_id}"
    else:
        url = f"https://weibo.com/{author_id}/{post_id}"
    cursor = conn.execute(
        """
        INSERT INTO web_posts(
            platform_key, platform_post_id, author_platform_id, source_type, source_url,
            canonical_url, keyword, captured_at, topic_relevant, content_text,
            post_likes_count, post_comments_count, post_shares_count,
            author_display_name, author_followers_count, author_following_count,
            author_posts_count, author_description, author_profile_url,
            author_verified, author_verified_text, raw_sample_json, post_images_count
        ) VALUES (
            ?, ?, ?, 'import', ?, ?, ?, '2026-11-01T00:00:00+08:00', ?, ?,
            ?, ?, ?, ?, ?, 1, 2, '旧简介', ?, ?, ?, ?, ?
        )
        """,
        (
            platform,
            post_id,
            author_id if platform == "weibo" else "people-1",
            url,
            url,
            keyword,
            topic_relevant,
            content,
            likes,
            comments,
            shares,
            author_name,
            followers,
            f"https://weibo.com/u/{author_id}" if platform == "weibo" else url,
            verified,
            verified_text,
            json.dumps(raw or {}, ensure_ascii=False, sort_keys=True),
            1 if image_url else 0,
        ),
    )
    web_post_id = int(cursor.lastrowid)
    if image_url:
        conn.execute(
            """
            INSERT INTO web_post_images(
                web_post_id, image_index, image_url, image_role, local_path, sha256
            ) VALUES (?, 0, ?, 'content', ?, 'abc')
            """,
            (web_post_id, image_url, f"data/media/{platform}/{post_id}/0.jpg"),
        )
    return web_post_id


def _waive(conn: sqlite3.Connection, web_post_id: int) -> None:
    conn.execute(
        """
        INSERT INTO post_detail_repair_waivers(
            web_post_id, reason, authorized_by, authorized_at, evidence_json
        ) VALUES (?, 'operator_waived', 'user', '2026-11-14T00:00:00+00:00', '{}')
        """,
        (web_post_id,),
    )


def _seed_weibo_refresh_db(tmp_path: Path) -> Path:
    from trippostcollect.db.bootstrap import bootstrap_database

    db_path = tmp_path / "weibo-refresh.sqlite"
    bootstrap_database(db_path, sync_jobs=False)
    with sqlite3.connect(db_path) as conn:
        _insert_refresh_post(
            conn,
            post_id="101",
            raw={"content_detail_status": "detail_observed", "content_detail_source": "mobile_detail"},
        )
        _insert_refresh_post(
            conn,
            post_id="102",
            topic_relevant=0,
            raw={"content_detail_status": "detail_observed", "content_detail_source": "desktop_detail"},
        )
        _insert_refresh_post(
            conn,
            post_id="103",
            raw={
                "content_detail_status": "detail_observed",
                "content_detail_source": "desktop_detail",
                "author_json": {"weibo_verify": {}},
            },
        )
        _insert_refresh_post(
            conn,
            post_id="104",
            raw={
                "content_detail_status": "detail_observed",
                "content_detail_source": "desktop_detail",
                "author_json": {"weibo_verify": {"verified": False}},
            },
        )
        waived = _insert_refresh_post(
            conn,
            post_id="105",
            raw={"content_detail_status": "detail_observed", "content_detail_source": "search_mblog_complete"},
        )
        _waive(conn, waived)
        _insert_refresh_post(
            conn,
            post_id="106",
            raw={"content_detail_status": "search_payload"},
        )
        _insert_refresh_post(
            conn,
            post_id="z-old",
            platform="zhihu",
            topic_relevant=0,
            raw={"content_detail_status": "detail_observed", "content_detail_source": "answer_detail"},
            image_url=None,
        )
        _insert_refresh_post(
            conn,
            post_id="z-marked",
            platform="zhihu",
            topic_relevant=1,
            raw={
                "content_detail_status": "detail_observed",
                "content_detail_source": "answer_detail",
                "author_followers_source": "author_page",
                "author_page_badges": {},
            },
            image_url=None,
        )
        conn.commit()
    return db_path


def test_weibo_refresh_all_selects_only_relevant_unmarked_rows(tmp_path: Path) -> None:
    db_path = _seed_weibo_refresh_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        targets, rejected, pending = repair.select_targets(
            conn, platform="weibo", post_ids=[], max_items=0, refresh_all=True,
        )
        limited, _, _ = repair.select_targets(
            conn, platform="weibo", post_ids=["106"], max_items=0, refresh_all=True,
        )
        capped, _, _ = repair.select_targets(
            conn, platform="weibo", post_ids=[], max_items=1, refresh_all=True,
        )
        default_targets, _, default_pending = repair.select_targets(
            conn, platform="weibo", post_ids=[], max_items=0,
        )
        zhihu_targets, zhihu_rejected, _ = repair.select_targets(
            conn, platform="zhihu", post_ids=[], max_items=0, refresh_all=True,
        )
        assert repair.pending_count(conn, "weibo", refresh_all=True) == pending
        assert repair.waived_pending_count(conn, "weibo", refresh_all=True) == 1

    assert [item["platform_post_id"] for item in targets] == ["101", "106"]
    assert [item["platform_post_id"] for item in rejected] == ["105"]
    assert rejected[0]["reason"] == "post_detail_repair_waived"
    assert pending == 3
    assert [item["platform_post_id"] for item in limited] == ["106"]
    assert [item["platform_post_id"] for item in capped] == ["101"]
    assert default_pending == 1
    assert [item["platform_post_id"] for item in default_targets] == ["106"]
    assert [item["platform_post_id"] for item in zhihu_targets] == ["z-old"]
    assert zhihu_rejected == []
    assert repair._selection_predicate(True, platform="zhihu") == (
        "json_type(raw_sample_json, '$.author_page_badges') IS NULL"
    )
    assert "topic_relevant" not in repair._selection_predicate(
        True, platform="zhihu", column="p.raw_sample_json",
    )


def test_weibo_refresh_dry_run_reports_refreshable_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _seed_weibo_refresh_db(tmp_path)
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "repair_post_details.py",
            "--platform",
            "weibo",
            "--db",
            str(db_path),
            "--refresh-all",
            "--dry-run",
        ],
    )

    assert repair.main() == 0
    summary, _state = _summary_and_state(tmp_path)
    by_id = {item["platform_post_id"]: item for item in summary["target_results"]}
    assert summary["plan"]["selection"] == "refresh_all"
    assert summary["selection"] == "refresh_all"
    assert summary["plan"]["initial_pending_count"] == 3
    assert summary["plan"]["initial_waived_pending_count"] == 1
    assert summary["plan"]["initial_unwaived_pending_count"] == 2
    assert summary["plan"]["target_count"] == 2
    assert by_id["101"]["outcome"] == "planned"
    assert by_id["106"]["outcome"] == "planned"
    assert by_id["105"]["outcome"] == "rejected"
    assert "102" not in by_id
    assert "103" not in by_id
    assert "104" not in by_id


def test_refresh_all_rejects_douyin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "unused.sqlite"
    monkeypatch.setattr(
        sys,
        "argv",
        ["repair_post_details.py", "--platform", "douyin", "--db", str(db_path), "--refresh-all"],
    )
    with pytest.raises(SystemExit, match="requires --fill-missing"):
        repair.main()

    class _Args:
        dedupe_images = False
        refresh_all = True
        platform = "douyin"
        post_ids: list[str] = []
        max_items = 0

    with pytest.raises(SystemExit, match="requires --fill-missing"):
        repair._validate_repair_modes(_Args())


def test_weibo_refresh_repaired_rows_requires_every_condition(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "weibo-repaired.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE web_posts(
            id INTEGER PRIMARY KEY,
            platform_key TEXT,
            platform_post_id TEXT,
            content_text TEXT,
            raw_sample_json TEXT
        );
        CREATE TABLE web_post_images(
            id INTEGER PRIMARY KEY,
            web_post_id INTEGER,
            image_role TEXT,
            local_path TEXT,
            sha256 TEXT
        );
        """
    )

    def add(post_id: str, text: str, raw: dict, images: list[tuple[str, str]]) -> None:
        cursor = conn.execute(
            """
            INSERT INTO web_posts(platform_key, platform_post_id, content_text, raw_sample_json)
            VALUES ('weibo', ?, ?, ?)
            """,
            (post_id, text, json.dumps(raw)),
        )
        for local_path, digest in images:
            conn.execute(
                """
                INSERT INTO web_post_images(web_post_id, image_role, local_path, sha256)
                VALUES (?, 'content', ?, ?)
                """,
                (int(cursor.lastrowid), local_path, digest),
            )

    marked = {
        "content_detail_status": "detail_observed",
        "content_detail_source": "desktop_detail",
        "author_json": {"weibo_verify": {}},
    }
    unverified = {
        "content_detail_status": "detail_observed",
        "content_detail_source": "desktop_detail",
        "author_json": {"weibo_verify": {"verified": False}},
    }
    add("empty-mark", "正文", marked, [("data/media/weibo/a.jpg", "abc")])
    add("unverified", "正文", unverified, [("data/media/weibo/b.jpg", "abc")])
    add(
        "no-mark",
        "正文",
        {"content_detail_status": "detail_observed", "content_detail_source": "desktop_detail"},
        [("data/media/weibo/c.jpg", "abc")],
    )
    add(
        "mobile",
        "正文",
        {
            "content_detail_status": "detail_observed",
            "content_detail_source": "mobile_detail",
            "author_json": {"weibo_verify": {}},
        },
        [("data/media/weibo/d.jpg", "abc")],
    )
    add("blank", "  ", marked, [("data/media/weibo/e.jpg", "abc")])
    add("unsaved", "正文", marked, [("data/media/weibo/f.jpg", "")])
    add("zero", "正文", marked, [])
    add(
        "unobserved",
        "正文",
        {
            "content_detail_status": "search_payload",
            "content_detail_source": "desktop_detail",
            "author_json": {"weibo_verify": {}},
        },
        [("data/media/weibo/g.jpg", "abc")],
    )
    add("partial", "正文", marked, [("data/media/weibo/h.jpg", "abc"), ("", "")])
    conn.commit()
    ids = [
        "empty-mark", "unverified", "no-mark", "mobile", "blank", "unsaved", "zero", "unobserved", "partial",
    ]
    refresh = repair.repaired_rows(conn, "weibo", ids, refresh_all=True)
    plain = repair.repaired_rows(conn, "weibo", ids)
    assert refresh["empty-mark"]["recovered"] is True
    assert refresh["unverified"]["recovered"] is True
    assert refresh["no-mark"]["recovered"] is False
    assert refresh["mobile"]["recovered"] is False
    assert refresh["blank"]["recovered"] is False
    assert refresh["unsaved"]["recovered"] is False
    assert refresh["zero"]["recovered"] is False
    assert refresh["unobserved"]["recovered"] is False
    assert refresh["partial"]["recovered"] is False
    assert plain["mobile"]["recovered"] is True
    assert plain["no-mark"]["recovered"] is True
    assert plain["zero"]["recovered"] is False
    conn.close()


def test_weibo_refresh_failure_keeps_original_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trippostcollect.db.bootstrap import bootstrap_database

    db_path = tmp_path / "weibo-fail.sqlite"
    bootstrap_database(db_path, sync_jobs=False)
    with sqlite3.connect(db_path) as conn:
        _insert_refresh_post(conn, post_id="201", content="待刷新青岛", likes=4)
        _insert_refresh_post(conn, post_id="202", content="失败青岛", likes=5, image_url="https://wx1.sinaimg.cn/large/keep.jpg")
        _waive(conn, _insert_refresh_post(conn, post_id="203", content="豁免青岛"))
        conn.commit()
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "repair_post_details.py",
            "--platform",
            "weibo",
            "--db",
            str(db_path),
            "--refresh-all",
            "--batch-size",
            "10",
        ],
    )

    def fake_child(command: list[str], **_kwargs: object) -> dict:
        targets = json.loads(Path(command[command.index("--repair-targets-file") + 1]).read_text(encoding="utf-8"))
        ids = [item["platform_post_id"] for item in targets]
        assert "203" not in ids
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE web_posts
                SET content_text=?, post_likes_count=?, raw_sample_json=?
                WHERE platform_post_id='201'
                """,
                (
                    "刷新后的青岛正文",
                    9,
                    json.dumps(
                        {
                            "author_json": {"weibo_verify": {}},
                            "content_detail_source": "desktop_detail",
                            "content_detail_status": "detail_observed",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
            conn.commit()
            before = conn.execute(
                """
                SELECT content_text, post_likes_count, author_display_name, raw_sample_json
                FROM web_posts WHERE platform_post_id='202'
                """
            ).fetchone()
        stdout = _write_child_summary(
            tmp_path / "child.json",
            {
                "import_completion_met": True,
                "formal_validation": {
                    "valid_total_count": 1,
                    "skipped_candidate_failures": [{
                        "platform_post_id": "202",
                        "error_code": "detail_unavailable",
                        "detail": "status deleted",
                    }],
                },
                "import_result": {"processed_rows": 1},
            },
        )
        fake_child.before = before
        return {"returncode": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    monkeypatch.setattr(repair, "verify_image_artifacts", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(repair, "verify_image_persistence", lambda *_a, **_k: {"ok": True})

    assert repair.main() == 0
    summary, _state = _summary_and_state(tmp_path)
    by_id = {item["platform_post_id"]: item for item in summary["target_results"]}
    assert by_id["201"]["outcome"] == "refreshed"
    assert by_id["202"]["outcome"] == "candidate_failed"
    assert by_id["202"]["reason"] == "detail_unavailable"
    assert by_id["203"]["outcome"] == "rejected"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 3
        failed = conn.execute(
            """
            SELECT content_text, post_likes_count, author_display_name, raw_sample_json
            FROM web_posts WHERE platform_post_id='202'
            """
        ).fetchone()
        images = conn.execute(
            """
            SELECT image_url FROM web_post_images
            WHERE web_post_id=(SELECT id FROM web_posts WHERE platform_post_id='202')
            """
        ).fetchall()
        targets, rejected, _pending = repair.select_targets(
            conn, platform="weibo", post_ids=[], max_items=0, refresh_all=True,
        )
    assert tuple(failed) == tuple(fake_child.before)
    assert [row[0] for row in images] == ["https://wx1.sinaimg.cn/large/keep.jpg"]
    assert [item["platform_post_id"] for item in targets] == ["202"]
    assert [item["platform_post_id"] for item in rejected] == ["203"]


def _parsed_weibo_record(*, post_id: str, author_id: int, screen_name: str, followers: int, text: str, likes: int, comments: int, shares: int, pid: str, verify: dict | None) -> dict:
    from trippostcollect.platforms.weibo.parser import parse_weibo_detail

    user = {
        "id": author_id,
        "screen_name": screen_name,
        "followers_count": followers,
        "friends_count": followers + 1,
        "statuses_count": followers + 2,
        "description": f"{screen_name}简介",
    }
    if verify is not None:
        user.update(verify)
    parsed = parse_weibo_detail(
        {
            "idstr": post_id,
            "mid": post_id,
            "mblogid": f"M{post_id}",
            "created_at": "Sat Jun 14 12:00:00 +0800 2025",
            "text_raw": text,
            "attitudes_count": likes,
            "comments_count": comments,
            "reposts_count": shares,
            "pic_ids": [pid],
            "pic_infos": {
                pid: {"type": "pic", "largest": {"url": f"https://wx1.sinaimg.cn/large/{pid}.jpg"}},
            },
            "user": {"id": author_id},
        },
        user,
        mblogid=f"M{post_id}",
        source_keyword="青岛旅游",
        current_timestamp=lambda: 1,
    )
    assert parsed.record is not None
    return parsed.record


def _stage_weibo_record(project_root: Path, record: dict, payload: bytes) -> tuple[dict, Path, str]:
    from trippostcollect.artifacts.image_candidates import content_image_candidates
    from trippostcollect.artifacts.image_manifest import ImageManifestEntry, write_manifest_atomic
    from trippostcollect.artifacts.image_materialization import write_staging_image
    from trippostcollect.records.formal import formal_record_identity

    data_root = project_root / "temp" / "batch" / record["note_id"] / "data"
    manifest_path = data_root / "weibo" / "image_manifest.jsonl"
    candidate = content_image_candidates("weibo", record)[0]
    staged = write_staging_image(
        [payload],
        staging_root=data_root,
        relative_stem=f"weibo/images/{candidate.platform_post_id}/{candidate.source_index:03d}",
    )
    write_manifest_atomic(
        manifest_path,
        [ImageManifestEntry(
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
            staging_path=staged.path.relative_to(data_root).as_posix(),
            size_bytes=staged.size_bytes,
            mime_type=staged.mime_type,
            width=staged.width,
            height=staged.height,
            sha256=staged.sha256,
            error_code=None,
        )],
    )
    selected = {
        "platform": "weibo",
        "record": record,
        "identity": formal_record_identity("weibo", record),
        "manifest_paths": [str(manifest_path)],
    }
    return selected, manifest_path, staged.sha256


def _repair_visit(ledger, *, post_id: str, author_id: str, asset_key: str, digest: str) -> dict:
    at = "2026-11-24T00:00:00+00:00"

    def seq() -> int:
        return ledger.next_seq()

    trigger = ledger.append(
        "action", seq=seq(), page_role="detail", action="navigate", target={"note_id": post_id},
    )
    detail = ledger.append(
        "visit", seq=seq(), at=at, recorded_at=at, page_role="detail", stage="note_detail",
        source="page_response", rule="weibo.statuses_show", status=200, subject_id=post_id, trigger=trigger,
    )
    start = seq()
    ledger.append(
        "pause", seq=seq(), page_role="detail", stage="note_detail", start_seq=start, seconds=1.0, continuity="n/a",
    )
    author = ledger.append(
        "visit", seq=seq(), at=at, recorded_at=at, page_role="detail", stage="creator_profile",
        source="page_response", rule="weibo.side_user", status=200, subject_id=author_id, trigger=trigger,
    )
    image = ledger.append(
        "visit", seq=seq(), at=at, recorded_at=at, page_role="detail", stage="note_image",
        source="cdn_direct", rule="weibo.content_image", status=200, subject_id=post_id, trigger=trigger,
        asset_key=asset_key, sha256=digest,
    )
    return {
        "ledger_id": ledger.ledger_id,
        "note_detail": detail,
        "creator_profile": author,
        "images": [image],
    }


def test_weibo_refresh_import_updates_row_and_skips_rerun(tmp_path: Path) -> None:
    from io import BytesIO

    from PIL import Image

    from trippostcollect.application.visit_gate import validate_content_visits
    from trippostcollect.artifacts.formal_images import materialize_formal_record_images
    from trippostcollect.db.bootstrap import bootstrap_database
    from trippostcollect.db.content import import_valid_records
    from trippostcollect.runtime.visit_ledger import VisitLedger

    def png(color: tuple[int, int, int]) -> bytes:
        output = BytesIO()
        Image.new("RGB", (7, 5), color=color).save(output, format="PNG")
        return output.getvalue()

    unverified = _parsed_weibo_record(
        post_id="525", author_id=88, screen_name="新海边", followers=42, text="青岛新正文夜景",
        likes=19, comments=18, shares=17, pid="pid-525",
        verify={"verified": False, "verified_type": -1, "verified_reason": ""},
    )
    empty_verify = _parsed_weibo_record(
        post_id="526", author_id=89, screen_name="空认证", followers=5, text="青岛空认证正文",
        likes=4, comments=6, shares=8, pid="pid-526", verify=None,
    )
    project_root = tmp_path / "project"
    project_root.mkdir()
    staged = [
        _stage_weibo_record(project_root, unverified, png((1, 2, 3))),
        _stage_weibo_record(project_root, empty_verify, png((4, 5, 6))),
    ]
    ledger_path = project_root / "repair.content_visits.jsonl"
    ledger = VisitLedger.create(
        ledger_path, platform="weibo", mode="repair", clock=lambda: "2026-11-24T00:00:00+00:00",
    )
    for record, _manifest, digest in staged:
        asset_key = record["record"]["image_assets"][0]["source_asset_key"]
        record["record"]["content_visit_refs"] = _repair_visit(
            ledger,
            post_id=record["record"]["note_id"],
            author_id=record["record"]["author_platform_id"],
            asset_key=asset_key,
            digest=digest,
        )
    visit = validate_content_visits(
        [item[0]["record"] for item in staged],
        [ledger_path],
        platform="weibo",
        mode="repair",
        manifests=[item[1] for item in staged],
    )
    assert visit["content_visit_ok"] is True, visit

    db_path = project_root / "refresh.sqlite"
    bootstrap_database(db_path, sync_jobs=False)
    with sqlite3.connect(db_path) as conn:
        _insert_refresh_post(
            conn, post_id="525", content="旧青岛正文", likes=1, comments=2, shares=3,
            author_name="旧海边", followers=3, image_url="https://wx1.sinaimg.cn/large/old-525.jpg",
        )
        _insert_refresh_post(
            conn, post_id="526", author_id="89", content="旧空认证", likes=1, comments=1, shares=1,
            author_name="旧空", followers=1, image_url="https://wx1.sinaimg.cn/large/old-526.jpg",
        )
        _insert_refresh_post(conn, post_id="527", content="仍待刷新青岛")
        conn.commit()
        before_count = conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0]

    media_root = project_root / "media"
    selected = [item[0] for item in staged]
    _report, materialized, complete = materialize_formal_record_images(
        selected, project_root=project_root, media_root=media_root, promote=True,
    )
    assert complete == {item["identity"] for item in selected}
    for item in selected:
        item["materialized_images"] = materialized[item["identity"]]
    imported = import_valid_records(
        {"captured_at": "2026-11-24T00:00:00+00:00", "keyword": "青岛旅游", "batch_dir": str(project_root)},
        selected,
        db_path,
        project_root=project_root,
        media_root=media_root,
        require_local_images=True,
    )
    assert imported["updated_rows"] == 2
    assert imported["inserted_rows"] == 0

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == before_count
        rows = {
            row["platform_post_id"]: row
            for row in conn.execute(
                """
                SELECT platform_post_id, content_text, topic_relevant, post_likes_count,
                       post_comments_count, post_shares_count, author_display_name,
                       author_platform_id, author_followers_count, author_following_count,
                       author_posts_count, author_description, author_profile_url,
                       author_verified, author_verified_text, author_json, raw_sample_json
                FROM web_posts ORDER BY id
                """
            )
        }
        images = {
            row["platform_post_id"]: row["image_url"]
            for row in conn.execute(
                """
                SELECT p.platform_post_id, i.image_url
                FROM web_post_images i JOIN web_posts p ON p.id=i.web_post_id
                WHERE i.image_role='content'
                """
            )
        }
        targets, _rejected, _pending = repair.select_targets(
            conn, platform="weibo", post_ids=[], max_items=0, refresh_all=True,
        )
    fresh = rows["525"]
    blank = rows["526"]
    assert fresh["content_text"] == "青岛新正文夜景"
    assert fresh["topic_relevant"] == 1
    assert (fresh["post_likes_count"], fresh["post_comments_count"], fresh["post_shares_count"]) == (19, 18, 17)
    assert fresh["author_display_name"] == "新海边"
    assert fresh["author_platform_id"] == "88"
    assert fresh["author_followers_count"] == 42
    assert fresh["author_following_count"] == 43
    assert fresh["author_posts_count"] == 44
    assert fresh["author_description"] == "新海边简介"
    assert fresh["author_profile_url"] == "https://weibo.com/u/88"
    assert fresh["author_verified"] == 0
    assert fresh["author_verified_text"] is None
    assert json.loads(fresh["author_json"])["weibo_verify"]["verified"] is False
    assert json.loads(fresh["raw_sample_json"])["author_json"]["weibo_verify"]["verified"] is False
    assert images["525"] == "https://wx1.sinaimg.cn/large/pid-525.jpg"
    assert blank["content_text"] == "青岛空认证正文"
    assert blank["topic_relevant"] == 1
    assert blank["author_display_name"] == "空认证"
    assert blank["author_verified"] is None
    assert json.loads(blank["author_json"])["weibo_verify"] == {}
    assert json.loads(blank["raw_sample_json"])["author_json"]["weibo_verify"] == {}
    assert images["526"] == "https://wx1.sinaimg.cn/large/pid-526.jpg"
    assert rows["527"]["content_text"] == "仍待刷新青岛"
    assert images["527"] == "https://wx1.sinaimg.cn/large/old.jpg"
    assert [item["platform_post_id"] for item in targets] == ["527"]


def test_weibo_refresh_image_failure_does_not_rewrite_row(tmp_path: Path) -> None:
    from trippostcollect.db.bootstrap import bootstrap_database
    from trippostcollect.db.content import FormalImportBeforeCommitError, import_valid_records
    from trippostcollect.records.formal import formal_record_identity

    record = _parsed_weibo_record(
        post_id="601", author_id=88, screen_name="新海边", followers=42, text="青岛失败不应入库",
        likes=9, comments=8, shares=7, pid="pid-601",
        verify={"verified": False},
    )
    project_root = tmp_path / "project"
    project_root.mkdir()
    db_path = project_root / "fail.sqlite"
    bootstrap_database(db_path, sync_jobs=False)
    with sqlite3.connect(db_path) as conn:
        _insert_refresh_post(
            conn, post_id="601", content="原青岛正文", likes=1,
            image_url="https://wx1.sinaimg.cn/large/old-601.jpg",
        )
        conn.commit()
        before_posts = conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0]
        before = conn.execute(
            "SELECT content_text, post_likes_count, author_display_name, raw_sample_json FROM web_posts"
        ).fetchone()
        before_images = conn.execute("SELECT image_url FROM web_post_images").fetchall()

    with pytest.raises(FormalImportBeforeCommitError):
        import_valid_records(
            {"captured_at": "2026-11-24T00:00:00+08:00", "keyword": "青岛旅游", "batch_dir": str(project_root)},
            [{
                "platform": "weibo",
                "record": record,
                "identity": formal_record_identity("weibo", record),
            }],
            db_path,
            project_root=project_root,
            media_root=project_root / "media",
            require_local_images=True,
        )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == before_posts
        assert conn.execute(
            "SELECT content_text, post_likes_count, author_display_name, raw_sample_json FROM web_posts"
        ).fetchone() == before
        assert conn.execute("SELECT image_url FROM web_post_images").fetchall() == before_images
