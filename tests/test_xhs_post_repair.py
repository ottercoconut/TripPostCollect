"""TripPostCollect tests for xhs post repair."""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.xhs import accounts


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

repair = import_module("repair_xhs_posts")
mediacrawler = import_module("mediacrawler_crawl")
entrypoint = import_module("mediacrawler_export_entrypoint")


@pytest.mark.parametrize("interrupt_kind", ["lease_signal", "keyboard_interrupt"])
def test_xhs_repair_interrupt_writes_terminal_audit_before_exact_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_kind: str,
) -> None:
    target_path = tmp_path / "targets.json"
    target_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "targets": [
                    {
                        "target_key": "test",
                        "keyword": "青岛旅游",
                        "top_refresh_max_pages": 1,
                        "timeout_seconds": 1800,
                        "required_fields_profile": "image_post_with_followers_v1",
                        "followers_policy": "required",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    pool_path = tmp_path / "pool.json"
    pool_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "lease_seconds": 2400,
                "behavior_profile": "xhs_guarded",
                "headed": True,
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "content.sqlite"
    bootstrap_database(db_path, sync_jobs=False)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        accounts.ensure_xhs_schema(conn)
        accounts.register_account_slot(conn, "xhs-a01")
        conn.execute(
            """
            INSERT OR IGNORE INTO source_platforms(
                platform_key, display_name, status, default_url,
                recommended_scrapling_mode
            ) VALUES ('xhs', '小红书', 'active', 'https://www.xiaohongshu.com', 'dynamic')
            """
        )
        conn.execute(
            """
            INSERT INTO web_posts(
                platform_key, platform_post_id, source_type, source_url,
                canonical_url, captured_at, keyword, raw_sample_json,
                artifact_dir
            ) VALUES ('xhs', 'note-1', 'post', ?, ?, ?, '青岛旅游', ?, ?)
            """,
            (
                "https://www.xiaohongshu.com/explore/note-1",
                "https://www.xiaohongshu.com/explore/note-1?"
                "xsec_token=token-1&xsec_source=pc_search",
                "2026-09-03T08:00:00+00:00",
                json.dumps({"content_detail_status": "search_only"}),
                "artifacts/note-1",
            ),
        )
        conn.commit()

    run_id = f"repair-{interrupt_kind}"
    output_root = tmp_path / "outputs"
    runtime_root = tmp_path / "runtime"
    session_root = tmp_path / "sessions"
    monkeypatch.setattr(repair, "XHS_REPAIR_OUTPUT", output_root)
    monkeypatch.setattr(repair, "XHS_REPAIR_RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(repair, "utc_stamp", lambda: run_id)
    monkeypatch.setattr(
        "trippostcollect.xhs.runtime.XHS_SESSION_ROOT",
        session_root,
    )
    monkeypatch.setattr(accounts, "XHS_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(
        repair,
        "parse_args",
        lambda: SimpleNamespace(
            target_key="test",
            account_id="xhs-a01",
            db=str(db_path),
            target_config=str(target_path),
            pool_config=str(pool_path),
            keyword=None,
            max_items=1,
            batch_size=1,
            post_ids=["note-1"],
            dry_run=False,
            post_interaction="none",
        ),
    )
    watchdogs = []
    close_observations: list[dict[str, object]] = []
    original_close = repair.LeaseGuard.close

    def interrupt_child(self, *_args, **kwargs):
        watchdogs.append(kwargs.get("runtime_watchdog"))
        if interrupt_kind == "lease_signal":
            self.signal_received = int(repair.signal.SIGINT)
            raise repair.XhsLeaseSignal(repair.signal.SIGINT)
        raise KeyboardInterrupt

    monkeypatch.setattr(repair.LeaseGuard, "run_subprocess", interrupt_child)

    def observe_close(self):
        state_value = json.loads(
            (runtime_root / run_id / "execution_state.json").read_text(
                encoding="utf-8"
            )
        )
        summary_value = json.loads(
            (output_root / run_id / "run_summary.json").read_text(encoding="utf-8")
        )
        with sqlite3.connect(db_path) as conn:
            run_row = conn.execute(
                "SELECT status, finished_at FROM xhs_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            finished_events = conn.execute(
                """
                SELECT COUNT(*) FROM xhs_account_events
                WHERE run_id=? AND event_type='xhs_post_repair_finished'
                """,
                (run_id,),
            ).fetchone()[0]
        close_observations.append(
            {
                "state_status": state_value["status"],
                "summary_reason": summary_value["reason"],
                "run_row": run_row,
                "finished_events": finished_events,
            }
        )
        return original_close(self)

    monkeypatch.setattr(repair.LeaseGuard, "close", observe_close)

    assert repair._run_main() == 130
    assert len(watchdogs) == 1
    assert watchdogs[0].startup_grace_seconds == 120.0
    assert watchdogs[0].stale_after_seconds == 60.0
    assert len(close_observations) == 1
    close_observation = close_observations[0]
    assert close_observation["state_status"] == "failed"
    assert close_observation["summary_reason"] == "operator_interrupt"
    assert close_observation["finished_events"] == 1
    assert close_observation["run_row"][0] == "failed"
    assert close_observation["run_row"][1]

    summary_path = output_root / run_id / "run_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["failure_type"] == "runtime_failed"
    assert summary["stop_reason"] == "runtime_failed"
    assert summary["reason"] == "operator_interrupt"
    assert summary["interrupt"] == {
        "reason": "operator_interrupt",
        "source": interrupt_kind,
        "signum": int(repair.signal.SIGINT),
        "signal": "SIGINT",
        "exit_code": 130,
    }
    assert summary["lease_released"] is True
    assert summary["runtime_session_removed"] is True
    assert summary["lease_cleanup"]["ok"] is True
    assert not (session_root / run_id).exists()

    state_path = runtime_root / run_id / "execution_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert (
        state["steps"]["command_executed"]["error"]
        == "xhs_repair_runtime_failed:operator_interrupt:SIGINT"
    )
    assert state["steps"]["command_executed"]["evidence"]["interrupt"] == summary[
        "interrupt"
    ]
    assert "adaptive_search_stopped" not in {
        event.get("type") for event in state.get("events", [])
    }

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM xhs_account_leases").fetchone()[0] == 0
        row = conn.execute(
            "SELECT status, finished_at, report_json FROM xhs_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "failed"
        assert row[1]
        assert json.loads(row[2])["lease_cleanup"]["ok"] is True


def test_detail_url_preserves_authoritative_xsec_query() -> None:
    row = {
        "platform_post_id": "note-1",
        "canonical_url": (
            "https://www.xiaohongshu.com/explore/note-1?"
            "xsec_token=token-1&xsec_source=pc_search"
        ),
        "raw_sample_json": json.dumps({"xsec_token": "token-1"}),
    }

    url, reason = repair._detail_url(row)

    assert reason == ""
    assert url is not None
    assert "xsec_token=token-1" in url
    assert "xsec_source=pc_search" in url


def test_detail_url_rejects_missing_source_instead_of_guessing() -> None:
    row = {
        "platform_post_id": "note-1",
        "canonical_url": "https://www.xiaohongshu.com/explore/note-1?xsec_token=token-1",
        "raw_sample_json": json.dumps({}),
    }

    assert repair._detail_url(row) == (None, "missing_xsec_token_or_source")


def test_load_xhs_detail_urls_requires_both_tokens(tmp_path: Path) -> None:
    path = tmp_path / "urls.json"
    path.write_text(
        json.dumps(
            [
                "https://www.xiaohongshu.com/explore/note-1?"
                "xsec_token=token-1&xsec_source=pc_search"
            ]
        ),
        encoding="utf-8",
    )

    assert mediacrawler.load_xhs_detail_urls(path)[0].endswith(
        "xsec_token=token-1&xsec_source=pc_search"
    )

    path.write_text(
        json.dumps(["https://www.xiaohongshu.com/explore/note-1?xsec_token=token-1"]),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="xsec_token and xsec_source"):
        mediacrawler.load_xhs_detail_urls(path)


def test_select_targets_excludes_observed_rows_and_limits_batch(tmp_path: Path) -> None:
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
            artifact_dir TEXT
        )
        """
    )
    for index, status in enumerate(("", "detail_observed", ""), start=1):
        post_id = f"note-{index}"
        conn.execute(
            """
            INSERT INTO web_posts(
                id, platform_key, platform_post_id, canonical_url, keyword,
                raw_sample_json, artifact_dir
            ) VALUES (?, 'xhs', ?, ?, '青岛旅游', ?, ?)
            """,
            (
                index,
                post_id,
                f"https://www.xiaohongshu.com/explore/{post_id}?"
                f"xsec_token=token-{index}&xsec_source=pc_search",
                json.dumps({"content_detail_status": status, "xsec_token": f"token-{index}"}),
                f"artifact/{post_id}",
            ),
        )
    conn.commit()

    targets, rejected = repair.select_targets(conn, post_ids=[], max_items=1)

    assert len(targets) == 1
    assert targets[0]["platform_post_id"] == "note-1"
    assert rejected == []
    conn.close()


def test_select_targets_skips_recorded_failure_but_explicit_retry_overrides(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(tmp_path / "repair.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE web_posts(
            id INTEGER PRIMARY KEY,
            platform_key TEXT,
            platform_post_id TEXT,
            canonical_url TEXT,
            keyword TEXT,
            raw_sample_json TEXT,
            artifact_dir TEXT
        );
        CREATE TABLE xhs_runs(
            run_id TEXT PRIMARY KEY,
            target_key TEXT,
            status TEXT,
            started_at TEXT,
            report_json TEXT
        );
        """
    )
    for index in (1, 2):
        post_id = f"note-{index}"
        conn.execute(
            """
            INSERT INTO web_posts VALUES(
                ?, 'xhs', ?, ?, '青岛旅游', '{}', ?
            )
            """,
            (
                index,
                post_id,
                f"https://www.xiaohongshu.com/explore/{post_id}?"
                f"xsec_token=token-{index}&xsec_source=pc_search",
                f"artifact/{post_id}",
            ),
        )
    conn.execute(
        "INSERT INTO xhs_runs VALUES(?, ?, ?, ?, ?)",
        (
            "run-1",
            "xhs_repair:qingdao_travel",
            "completed",
            "2026-08-21T00:00:00+00:00",
            json.dumps(
                {
                    "repair_report": {
                        "candidate_failures": [
                            {
                                "platform_post_id": "note-1",
                                "failure_scope": "detail",
                                "error_code": "note_not_found",
                                "attempts": 1,
                                "retryable": False,
                            }
                        ]
                    }
                }
            ),
        ),
    )
    conn.commit()

    targets, rejected = repair.select_targets(conn, post_ids=[], max_items=20)
    assert [item["platform_post_id"] for item in targets] == ["note-2"]
    assert rejected[0]["platform_post_id"] == "note-1"
    assert rejected[0]["reason"] == "previous_repair_failure"
    assert rejected[0]["previous_failure"]["error_code"] == "note_not_found"

    targets, rejected = repair.select_targets(
        conn,
        post_ids=["note-1"],
        max_items=20,
    )
    assert [item["platform_post_id"] for item in targets] == ["note-1"]
    assert rejected == []
    conn.close()


def test_load_xhs_repair_fallbacks_includes_existing_metrics(tmp_path: Path) -> None:
    db_path = tmp_path / "repair.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE web_posts(
            platform_key TEXT,
            platform_post_id TEXT,
            keyword TEXT,
            raw_sample_json TEXT,
            post_likes_count INTEGER,
            post_favorites_count INTEGER,
            post_comments_count INTEGER,
            post_shares_count INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO web_posts VALUES(
            'xhs', 'note-1', '青岛旅游', '{}', 11, 12, 0, 13
        )
        """
    )
    conn.commit()
    conn.close()

    fallbacks = mediacrawler.load_post_repair_fallbacks(
        db_path,
        "xhs",
        {"note-1"},
    )

    assert fallbacks["note-1"] == {
        "keyword": "青岛旅游",
        "liked_count": 11,
        "collected_count": 12,
        "comment_count": 0,
        "share_count": 13,
    }


def test_xhs_repair_preserves_existing_metric_without_overwriting_fresh_zero() -> None:
    detail = {
        "note_id": "note-1",
        "desc": "完整正文",
        "content_detail_status": "detail_observed",
        "content_detail_source": "note_detail",
        "time": 1_786_000_000,
        "image_list": [{"url_default": "https://example.test/body.jpg"}],
        "user_id": "author-1",
        "nickname": "作者",
        "author_followers_count": 10,
        "followers_observed": True,
        "author_followers_source": "creator_profile",
        "liked_count": 0,
        "collected_count": 2,
        "comment_count": "",
        "share_count": 3,
    }

    merged = mediacrawler.merge_repair_fallback_metadata(
        "xhs",
        detail,
        {"liked_count": 99, "comment_count": 1},
    )

    validation = mediacrawler.validate_formal_record("xhs", merged, set())
    assert validation["valid"] is True
    assert merged["liked_count"] == 0
    assert merged["comment_count"] == 1
    assert merged["repair_fallback_evidence"] == {
        "metrics": {
            "comment_count": {
                "source": "existing_web_posts_metric",
                "value": 1,
            }
        }
    }


def test_xhs_repair_accepts_authoritative_title_and_image_without_desc(tmp_path: Path) -> None:
    contents_path = tmp_path / "xhs" / "detail_contents_test.jsonl"
    contents_path.parent.mkdir(parents=True)
    base = {
        "content_detail_status": "detail_observed",
        "content_detail_source": "note_detail",
        "time": 1_786_000_000,
        "image_list": [{"url_default": "https://example.test/body.jpg"}],
        "user_id": "author-1",
        "nickname": "作者",
        "author_followers_count": 10,
        "followers_observed": True,
        "author_followers_source": "creator_profile",
        "liked_count": 1,
        "collected_count": 2,
        "comment_count": "",
        "share_count": 3,
    }
    contents_path.write_text(
        "\n".join(
            json.dumps(value, ensure_ascii=False)
            for value in (
                {
                    **base,
                    "note_id": "missing-body",
                    "title": "只有标题的图文笔记",
                    "desc": "",
                },
                {
                    **base,
                    "note_id": "valid-note",
                    "title": "正文笔记",
                    "desc": "完整正文",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    summary = {
        "records": [
            {
                "platform": "xhs",
                "output": {
                    "jsonl_files": [str(contents_path)],
                    "image_manifest_paths": [],
                },
            }
        ]
    }

    validation, records = mediacrawler.collect_formal_records(
        summary,
        db_path=None,
        allowed_identities={"xhs:id:missing-body", "xhs:id:valid-note"},
        repair_metadata_by_identity={
            "xhs:id:missing-body": {"comment_count": 1},
            "xhs:id:valid-note": {"comment_count": 1},
        },
        repair_mode=True,
    )

    assert validation["valid_total_count"] == 2
    assert validation["invalid_reason_counts"] == {}
    assert [item["identity"] for item in records] == [
        "xhs:id:missing-body",
        "xhs:id:valid-note",
    ]


def test_xhs_repair_still_rejects_empty_title_and_desc(tmp_path: Path) -> None:
    contents_path = tmp_path / "xhs" / "detail_contents_test.jsonl"
    contents_path.parent.mkdir(parents=True)
    contents_path.write_text(
        json.dumps(
            {
                "note_id": "empty-note",
                "title": "",
                "desc": "",
                "content_detail_status": "detail_observed",
                "content_detail_source": "note_detail",
                "time": 1_786_000_000,
                "image_list": [{"url_default": "https://example.test/body.jpg"}],
                "user_id": "author-1",
                "nickname": "作者",
                "author_followers_count": 10,
                "followers_observed": True,
                "author_followers_source": "creator_profile",
                "liked_count": 1,
                "collected_count": 2,
                "comment_count": 3,
                "share_count": 4,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    summary = {
        "records": [
            {
                "platform": "xhs",
                "output": {
                    "jsonl_files": [str(contents_path)],
                    "image_manifest_paths": [],
                },
            }
        ]
    }

    validation, records = mediacrawler.collect_formal_records(
        summary,
        db_path=None,
        allowed_identities={"xhs:id:empty-note"},
        repair_mode=True,
    )

    assert validation["valid_total_count"] == 0
    assert validation["invalid_reason_counts"] == {"missing_content": 1}
    assert records == []


def test_repair_behavior_gate_does_not_require_search_pacing(monkeypatch) -> None:
    monkeypatch.setattr(mediacrawler, "behavior_evidence_valid", lambda value: True)
    result = mediacrawler.collect_behavior_validation(
        [
            {
                "platform": "xhs",
                "behavior_evidence": {
                    "status": "completed",
                    "profile": "xhs_guarded",
                    "url": "https://www.xiaohongshu.com/search_result?keyword=青岛旅游",
                    "events": [{}],
                    "request_pacing_events": [
                        {"stage": "note_detail"},
                        {"stage": "creator_profile"},
                    ],
                },
                "policy_events": [{"allowed": True, "disabled": True}],
            }
        ],
        ["xhs"],
        "青岛旅游",
        repair_mode=True,
    )

    assert result["ok"] is True
    assert result["platforms"]["xhs"]["request_pacing_ok"] is True


def test_partial_xhs_repair_can_commit_valid_records() -> None:
    assert mediacrawler.repair_partial_child_execution_allowed(
        repair_mode=True,
        child_execution_ok=False,
        validation={"valid_total_count": 2},
        image_materialization={"complete": True},
        behavior_validation={"ok": True},
    ) is True

    assert mediacrawler.repair_partial_child_execution_allowed(
        repair_mode=True,
        child_execution_ok=False,
        validation={"valid_total_count": 0},
        image_materialization={"complete": True},
        behavior_validation={"ok": True},
    ) is False


def test_xhs_repair_pagination_exposes_candidate_failures() -> None:
    evidence = mediacrawler.xhs_repair_pagination_evidence(
        [
            {
                "platform": "xhs",
                "repair_report": {
                    "batches": [{"batch": 1, "target_count": 2}],
                    "successful_ids": ["note-2"],
                    "candidate_failures": [
                        {
                            "platform_post_id": "note-1",
                            "failure_scope": "detail",
                            "error_code": "api_and_html_empty",
                            "attempts": 3,
                            "retryable": True,
                        }
                    ],
                },
            }
        ],
        target_count=2,
    )

    assert evidence["candidate_count"] == 2
    assert evidence["successful_candidate_count"] == 1
    assert evidence["skipped_candidate_count"] == 1
    assert evidence["skipped_candidate_failures"][0]["attempts"] == 3


def test_xhs_repair_report_preserves_run_level_blocker() -> None:
    reason = mediacrawler.repair_runtime_stop_reason(
        [
            {
                "platform": "xhs",
                "failure_classification": {"failure_type": "runtime_failed"},
                "repair_report": {
                    "runtime_blocker": {"error_code": "login_required"}
                },
            }
        ],
        ["xhs"],
    )

    assert reason == "login_required"


def test_candidate_only_child_failure_does_not_hide_runtime_or_media_blockers() -> None:
    summary = {
        "formal_validation": {
            "stop_reason": "repair_no_valid_detail",
            "valid_total_count": 0,
            "local_image_failure_count": 0,
            "pagination_runtime_blocked": False,
            "pagination_incomplete": False,
            "behavior_evidence_ok": True,
            "policy_evidence_ok": True,
        },
        "records": [
            {
                "platform": "xhs",
                "repair_report": {
                    "runtime_blocker": None,
                    "candidate_failures": [
                        {
                            "platform_post_id": "note-1",
                            "error_code": "note_not_found",
                        }
                    ],
                },
            }
        ],
    }

    assert repair.candidate_only_child_failure(summary) is True

    summary["formal_validation"]["local_image_failure_count"] = 1
    assert repair.candidate_only_child_failure(summary) is False
    summary["formal_validation"]["local_image_failure_count"] = 0
    summary["records"][0]["repair_report"]["runtime_blocker"] = {
        "error_code": "login_required"
    }
    assert repair.candidate_only_child_failure(summary) is False


@pytest.mark.asyncio
async def test_xhs_repair_continues_same_and_later_batches_after_candidate_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: list[str] = []
    requested: list[str] = []

    class FakeCrawler:
        async def get_note_detail_async_task(self, *, note_id, **_kwargs):
            requested.append(note_id)
            if note_id == "note-1":
                error = RuntimeError("detail exhausted")
                error.code = "api_and_html_empty"
                error.attempts = 3
                raise error
            return {
                "note_id": note_id,
                "xsec_token": f"token-{note_id}",
                "type": "normal",
            }

        async def enrich_note_creator(self, _note_detail):
            return None

        async def get_notice_media(self, _note_detail):
            return None

        async def batch_get_note_comments(self, _note_ids, _tokens):
            return None

        @staticmethod
        def is_video_note(_note_detail):
            return False

    async def update_xhs_note(note_detail):
        stored.append(note_detail["note_id"])

    fake_core = types.ModuleType("media_platform.xhs.core")
    fake_core.XiaoHongShuCrawler = FakeCrawler
    fake_core.config = SimpleNamespace(
        MAX_CONCURRENCY_NUM=1,
        XHS_SPECIFIED_NOTE_URL_LIST=["note-1", "note-2", "note-3"],
    )
    fake_core.parse_note_info_from_note_url = lambda value: SimpleNamespace(
        note_id=value,
        xsec_source="pc_search",
        xsec_token=f"token-{value}",
    )
    fake_core.utils = SimpleNamespace(
        logger=SimpleNamespace(info=lambda *_args: None, warning=lambda *_args: None)
    )
    fake_core.xhs_store = SimpleNamespace(update_xhs_note=update_xhs_note)
    fake_xhs = types.ModuleType("media_platform.xhs")
    fake_xhs.core = fake_core
    fake_platform = types.ModuleType("media_platform")
    fake_platform.xhs = fake_xhs
    monkeypatch.setitem(sys.modules, "media_platform", fake_platform)
    monkeypatch.setitem(sys.modules, "media_platform.xhs", fake_xhs)
    monkeypatch.setitem(sys.modules, "media_platform.xhs.core", fake_core)
    report_path = tmp_path / "repair_report.json"
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_REPAIR", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_REPAIR_BATCH_SIZE", "2")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_REPAIR_REPORT_PATH", str(report_path))

    entrypoint.install_xhs_repair_resilience()
    await FakeCrawler().get_specified_notes()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert requested == ["note-1", "note-2", "note-3"]
    assert stored == ["note-2", "note-3"]
    assert [batch["batch"] for batch in report["batches"]] == [1, 2]
    assert report["successful_ids"] == ["note-2", "note-3"]
    assert report["candidate_failures"] == [
        {
            "platform": "xhs",
            "identity": "xhs:id:note-1",
            "platform_post_id": "note-1",
            "batch": 1,
            "failure_scope": "detail",
            "detail": "RuntimeError",
            "error_type": "RuntimeError",
            "error_code": "api_and_html_empty",
            "attempts": 3,
            "retryable": True,
        }
    ]
