"""历史详情修复的暂停请求：不访问平台，只用临时库。"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import repair_post_details as repair  # noqa: E402
from test_post_detail_repair import (  # noqa: E402
    _mark_refreshed,
    _redirect_repair_dirs,
    _refresh_argv,
    _seed_refresh_posts,
    _summary_and_state,
    _target_post_id,
    _write_child_summary,
)


def _ok_verify(*_args: object, **_kwargs: object) -> dict:
    return {"ok": True}


def _install_success_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repair, "verify_image_artifacts", _ok_verify)
    monkeypatch.setattr(repair, "verify_image_persistence", _ok_verify)


def test_request_pause_writes_file_without_opening_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_PAUSE_ROOT", tmp_path / "pause")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pause request opened the database")

    monkeypatch.setattr(repair, "bootstrap_database", boom)
    monkeypatch.setattr(sys, "argv", ["repair_post_details.py", "--platform", "weibo", "--request-pause"])
    assert repair.main() == 0
    captured = capsys.readouterr().out
    path = tmp_path / "pause" / "weibo.json"
    assert str(path) in captured
    assert "当前批结束后停下" in captured
    assert "下一次启动会清除这个请求" in captured
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {"platform", "requested_at"}
    assert payload["platform"] == "weibo"
    monkeypatch.setattr(
        sys, "argv",
        ["repair_post_details.py", "--platform", "weibo", "--request-pause", "--refresh-all"],
    )
    with pytest.raises(SystemExit, match="cannot be combined"):
        repair.main()


def test_request_pause_accepts_equals_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_PAUSE_ROOT", tmp_path / "pause")
    monkeypatch.setattr(sys, "argv", ["repair_post_details.py", "--platform=weibo", "--request-pause"])
    assert repair.main() == 0
    assert repair.pause_request_path("weibo").is_file()


def test_pause_after_only_failed_batches_is_not_reported_as_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已派的批次一条都没成功时，暂停不得把“无进展”盖成正常暂停。"""

    db_path = tmp_path / "repair.sqlite"
    _seed_refresh_posts(db_path, ["row-a", "row-b"])
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _refresh_argv(db_path))

    def failed_child(command: list[str], **_kwargs: object) -> dict:
        repair.write_pause_request("zhihu")
        stdout = _write_child_summary(tmp_path / "child.json", {
            "import_completion_met": False,
            "formal_validation": {"valid_total_count": 0, "skipped_candidate_failures": []},
        })
        return {"returncode": 2, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(repair, "run_repair_child", failed_child)
    assert repair.main() == 2
    summary, state = _summary_and_state(tmp_path)
    assert summary["status"] == "failed"
    assert summary["not_attempted_count"] == 1
    assert state["status"] != "paused"


def test_signal_after_paused_outcome_keeps_the_paused_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_refresh_posts(db_path, ["row-a"])
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _refresh_argv(db_path))
    real_clear = repair.clear_pause_request
    real_finalize = repair.FrozenExecutionState.finalize
    calls = {"n": 0}

    def clear_then_request(platform: str):
        calls["n"] += 1
        payload = real_clear(platform)
        if calls["n"] == 1:
            repair.write_pause_request(platform)
        return payload

    def finalize_then_signal(self, *args: object, **kwargs: object):
        result = real_finalize(self, *args, **kwargs)
        os.kill(os.getpid(), signal.SIGINT)
        return result

    monkeypatch.setattr(repair, "clear_pause_request", clear_then_request)
    monkeypatch.setattr(repair.FrozenExecutionState, "finalize", finalize_then_signal)
    assert repair.main() == 128 + signal.SIGINT
    summary, state = _summary_and_state(tmp_path)
    assert state["status"] == "paused"
    assert summary["status"] == "paused"


def test_startup_clears_stale_pause_and_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_refresh_posts(db_path, ["row-a"])
    _redirect_repair_dirs(monkeypatch, tmp_path)
    _install_success_stubs(monkeypatch)
    repair.write_pause_request("zhihu")
    monkeypatch.setattr(sys, "argv", _refresh_argv(db_path))

    def fake_child(command: list[str], **_kwargs: object) -> dict:
        post_id = _target_post_id(command)
        _mark_refreshed(db_path, post_id)
        stdout = _write_child_summary(tmp_path / "child.json", {
            "import_completion_met": True,
            "formal_validation": {"valid_total_count": 1, "skipped_candidate_failures": []},
            "import_result": {},
        })
        return {"returncode": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    assert repair.main() == 0
    summary, state = _summary_and_state(tmp_path)
    assert summary["status"] == "completed"
    assert "pause" not in summary
    assert not repair.pause_request_path("zhihu").exists()
    events = [item["type"] for item in state["events"]]
    assert "post_detail_repair_pause_cleared" in events


def test_pause_before_first_batch_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_refresh_posts(db_path, ["row-a", "row-b"])
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _refresh_argv(db_path))
    real_clear = repair.clear_pause_request
    calls = {"n": 0}

    def clear_then_request(platform: str):
        calls["n"] += 1
        payload = real_clear(platform)
        if calls["n"] == 1:
            repair.write_pause_request(platform)
        return payload

    monkeypatch.setattr(repair, "clear_pause_request", clear_then_request)

    def fail_if_called(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("paused before the first batch must not launch a child")

    monkeypatch.setattr(repair, "run_repair_child", fail_if_called)
    monkeypatch.setattr(repair, "verify_image_artifacts", fail_if_called)
    monkeypatch.setattr(repair, "verify_image_persistence", fail_if_called)
    assert repair.main() == 0
    summary, state = _summary_and_state(tmp_path)
    assert summary["status"] == "paused"
    assert summary["error"] == ""
    assert summary["pause"]["stopped_before_batch"] == 1
    assert summary["pause"]["remaining"] == 2
    assert summary["not_attempted_count"] == 2
    assert summary["candidate_failure_count"] == 0
    assert state["status"] == "paused"
    assert state["steps"]["artifacts_verified"]["status"] == "skipped"
    assert state["steps"]["artifacts_verified"]["evidence"]["verified"] is False
    assert state["steps"]["persistence_verified"]["evidence"]["verified"] is False
    assert not repair.pause_request_path("zhihu").exists()


def test_pause_after_current_batch_then_rerun_selects_only_remaining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_refresh_posts(db_path, ["row-a", "row-b", "row-c"])
    _redirect_repair_dirs(monkeypatch, tmp_path)
    _install_success_stubs(monkeypatch)
    monkeypatch.setattr(sys, "argv", _refresh_argv(db_path))
    launched: list[str] = []

    def fake_child(command: list[str], **_kwargs: object) -> dict:
        post_id = _target_post_id(command)
        launched.append(post_id)
        _mark_refreshed(db_path, post_id)
        if post_id == "row-a":
            repair.write_pause_request("zhihu")
        stdout = _write_child_summary(tmp_path / f"{post_id}.json", {
            "import_completion_met": True,
            "formal_validation": {"valid_total_count": 1, "skipped_candidate_failures": []},
            "import_result": {},
            "image_materialization": {"images_downloaded": 1, "images_reused": 0, "downloaded_images": 1},
        })
        return {"returncode": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    assert repair.main() == 0
    summary, _state = _summary_and_state(tmp_path)
    assert launched == ["row-a"]
    assert summary["status"] == "paused"
    assert summary["pause"]["stopped_before_batch"] == 2
    assert summary["pause"]["remaining"] == 2
    assert summary["not_attempted_count"] == 2
    assert summary["candidate_failure_count"] == 0
    by_id = {item["platform_post_id"]: item for item in summary["target_results"]}
    assert by_id["row-b"]["outcome"] == "not_attempted"
    assert by_id["row-c"]["outcome"] == "not_attempted"

    launched.clear()
    # 第二次启动会先清掉已经不存在的请求，然后只选未刷新的行。
    out_dir = tmp_path / "out"
    for path in out_dir.glob("*/run_summary.json"):
        path.unlink()
    assert repair.main() == 0
    assert launched == ["row-b", "row-c"]
    second, _state = _summary_and_state(tmp_path)
    assert second["status"] == "completed"
    assert "row-a" not in launched


def test_signal_outranks_pause_and_throttle_clears_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "repair.sqlite"
    _seed_refresh_posts(db_path, ["row-a", "row-b", "row-c"])
    _redirect_repair_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _refresh_argv(db_path))

    def signal_child(command: list[str], **_kwargs: object) -> dict:
        repair.write_pause_request("zhihu")
        os.kill(os.getpid(), signal.SIGINT)
        _write_child_summary(tmp_path / "child.json", {
            "import_completion_met": True,
            "formal_validation": {"valid_total_count": 1, "skipped_candidate_failures": []},
        })
        return {"returncode": 0, "stdout": json.dumps({"summary": str(tmp_path / "child.json")}), "stderr": ""}

    monkeypatch.setattr(repair, "run_repair_child", signal_child)
    assert repair.main() == 128 + signal.SIGINT
    summary, _state = _summary_and_state(tmp_path)
    assert summary["status"] == "failed"
    assert "pause" not in summary

    for path in (tmp_path / "out").glob("*/run_summary.json"):
        path.unlink()
    monkeypatch.setattr(sys, "argv", _refresh_argv(db_path))

    def throttle_child(command: list[str], **_kwargs: object) -> dict:
        post_id = _target_post_id(command)
        if post_id == "row-a":
            _mark_refreshed(db_path, post_id)
            stdout = _write_child_summary(tmp_path / "ok.json", {
                "import_completion_met": True,
                "formal_validation": {"valid_total_count": 1, "skipped_candidate_failures": []},
                "records": [{"policy_events": [{"allowed": True, "reason": ""}]}],
            })
            return {"returncode": 0, "stdout": stdout, "stderr": ""}
        repair.write_pause_request("zhihu")
        stdout = _write_child_summary(tmp_path / "stop.json", {
            "import_completion_met": False,
            "formal_validation": {"valid_total_count": 0, "skipped_candidate_failures": []},
            "records": [{
                "policy_events": [{
                    "allowed": False,
                    "reason": "max_requests_per_session",
                    "cooldown_until": "2026-11-16T20:00:00+00:00",
                    "wait_seconds": 10,
                }],
            }],
        })
        return {"returncode": 2, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(repair, "run_repair_child", throttle_child)
    assert repair.main() == 2
    summary, _state = _summary_and_state(tmp_path)
    assert summary["status"] == "throttle_wait"
    assert "pause" not in summary
    assert not repair.pause_request_path("zhihu").exists()
