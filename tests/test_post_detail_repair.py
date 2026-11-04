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


REPAIR_FOLLOWER_SOURCES = {
    "weibo": "search_author",
    "douyin": "creator_profile",
    "zhihu": "search_author",
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


def _seed_pending_douyin(db_path: Path) -> None:
    from trippostcollect.db.bootstrap import bootstrap_database

    bootstrap_database(db_path, sync_jobs=False)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO web_posts(
                platform_key, platform_post_id, source_type, source_url,
                canonical_url, keyword, captured_at, raw_sample_json
            ) VALUES (
                'douyin', '123', 'search', 'https://www.douyin.com/video/123',
                'https://www.douyin.com/video/123', '青岛崂山',
                '2026-01-01T00:00:00+08:00', '{}'
            )
            """
        )
        conn.commit()


def _repair_argv(db_path: Path) -> list[str]:
    return [
        "repair_post_details.py",
        "--platform",
        "douyin",
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
                "    'repair_post_details.py', '--platform', 'douyin', '--db',",
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
