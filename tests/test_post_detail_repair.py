"""TripPostCollect tests for post detail repair."""

from __future__ import annotations

import json
import sqlite3
import signal
import subprocess
import sys
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
            "douyin",
            "123",
            "https://www.douyin.com/video/123?previous_page=search_result",
            "https://www.douyin.com/video/123",
        ),
        (
            "weibo",
            "456",
            "https://m.weibo.cn/detail/456",
            "456",
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
        (1, "1", "https://www.douyin.com/video/1", "青岛旅游", "{}"),
        (
            2,
            "2",
            "https://www.douyin.com/video/2",
            "青岛旅游",
            json.dumps({"content_detail_status": "detail_observed"}),
        ),
        (3, "3", "https://example.test/video/3", "青岛旅游", "not-json"),
    ]
    conn.executemany(
        """
        INSERT INTO web_posts(
            id, platform_key, platform_post_id, canonical_url, keyword,
            raw_sample_json, artifact_dir
        ) VALUES (?, 'douyin', ?, ?, ?, ?, '')
        """,
        rows,
    )
    conn.commit()

    targets, rejected, pending = repair.select_targets(
        conn,
        platform="douyin",
        post_ids=[],
        max_items=0,
    )

    assert pending == 2
    assert [item["platform_post_id"] for item in targets] == ["1"]
    assert rejected[0]["platform_post_id"] == "3"
    assert rejected[0]["reason"] == "invalid_douyin_canonical_url"
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
    assert targets[0]["repair_fallback"]["published_at"] == "2026-07-13T12:00:00+08:00"
    assert targets[0]["repair_fallback"]["followers_observed"] is True
    assert targets[0]["repair_fallback"]["author_followers_source"] == "search_author"
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
        ("douyin", "https://www.douyin.com/video/123"),
        ("weibo", "123"),
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
                    "detail_target": "https://www.douyin.com/video/999",
                    "keyword": "青岛旅游",
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="does not match douyin ID 123"):
        mediacrawler.load_post_repair_targets(path, "douyin")


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
        "creator_hash": "hash-1",
        "user_nickname": "作***",
        "followers_count": 0,
        "followers_observed": False,
        "author_followers_source": "missing",
        "voteup_count": 1,
        "comment_count": 2,
    }
    merged = mediacrawler.merge_repair_fallback_metadata(
        "zhihu",
        detail,
        {
            "published_at": "2026-07-13T12:00:00+08:00",
            "followers_count": 123,
            "author_followers_count": 123,
            "followers_observed": True,
            "author_followers_source": "search_author",
        },
    )

    validation = mediacrawler.validate_formal_record("zhihu", merged, set())
    assert validation["valid"] is True
    assert merged["content_detail_source"] == "answer_detail"
    assert merged["content_text"] == "完整正文"


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
        "creator_hash": "author-weibo",
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
        timeout_seconds=600,
        headless=False,
    )

    assert command[command.index("--platforms") + 1] == "weibo"
    assert "--candidate-hard-limit" not in command
    assert "--target-new-posts" not in command
    assert "--completion-mode" not in command
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


def test_run_repair_child_timeout_uses_formal_process_group_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int, int | None]] = []

    class FakeProcess:
        pid = 4242
        returncode = None

        def communicate(self, *, timeout: int | None = None) -> tuple[str, str]:
            if timeout == 17:
                raise subprocess.TimeoutExpired(
                    ["child"], timeout, output="before", stderr="warning"
                )
            self.returncode = 124
            return "after", "tail"

    process = FakeProcess()

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        del command
        events.append(("popen", int(kwargs["start_new_session"]), int(kwargs["text"])))
        return process

    def fake_killpg(pid: int, sig: int) -> None:
        events.append(("killpg", pid, sig))

    monkeypatch.setattr(repair.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(repair.os, "killpg", fake_killpg)

    result = repair.run_repair_child(
        ["child"],
        cwd=ROOT,
        timeout_seconds=17,
    )

    assert result["timed_out"] is True
    assert result["returncode"] == 124
    assert result["stdout"] == "beforeafter"
    assert result["stderr"] == "warningtail"
    assert events == [("popen", 1, 1), ("killpg", 4242, signal.SIGTERM)]


@pytest.mark.parametrize(
    ("error", "strict"),
    [
        ("post_detail_repair_batch_timeout:2040", False),
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
