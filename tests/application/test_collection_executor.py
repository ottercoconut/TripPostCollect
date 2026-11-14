"""执行器全链路：五站完整收集与入库、内容事务与发现提交故障、完成门禁与运行阻断、终态信封与 Markdown。"""

from __future__ import annotations

import importlib
import json
import subprocess

import pytest

from support import executor_driver
from support.executor_driver import drive, forbid, strict_patch
from trippostcollect.application import collection, reporting
from trippostcollect.runtime import process

offline_guard = executor_driver.offline_guard


def run(tmp_path, monkeypatch, capsys, platform, scenario):
    with monkeypatch.context() as patch:
        return drive(tmp_path / "new", patch, capsys, platform, scenario)


@pytest.mark.parametrize("platform", ["bilibili", "weibo", "douyin", "zhihu", "xhs"])
def test_b_full_chain(tmp_path, monkeypatch, capsys, platform):
    value = run(tmp_path, monkeypatch, capsys, platform, "success")
    assert value["result"] == 0, {
        "validation": value["summary"]["formal_validation"]["invalid_reason_counts"],
        "stderr": value["summary"]["records"][-1]["run"].get("stderr_tail"),
        "images": value["summary"]["image_materialization"]["failures"],
    }
    assert value["summary"]["import_completion_met"] is True
    assert len(value["database"]["web_posts"]) == len(value["database"]["web_post_images"]) == len(value["media"]) == 2
    assert value["database"]["ctf_captures"] == []
    if platform != "xhs":
        assert len(value["database"]["crawl_discovery_checkpoints"]) == 1
        assert len(value["database"]["crawl_discovery_seen_candidates"]) == 2
    trace = value["trace"]
    assert ("worker.run" in trace) is (platform != "bilibili")
    assert ("bilibili.behavior" in trace) is (platform == "bilibili")
    assert trace.index("media.acquire") < trace.index("media.promote.1") < trace.index("BEGIN IMMEDIATE")
    assert trace.index("BEGIN IMMEDIATE") < trace.index("content.commit.enter") < trace.index("content.commit.exit")
    assert trace.index("content.commit.exit") < trace.index("media.release") < trace.index("discovery.enter")


@pytest.mark.parametrize("scenario", ["upsert_failure", "commit_before", "commit_after", "promotion_failure",
                                      "sqlite_busy", "supervision", "discovery_failure"])
def test_c_transaction_faults(tmp_path, monkeypatch, capsys, scenario):
    value = run(tmp_path, monkeypatch, capsys, "xhs" if scenario == "supervision" else "weibo", scenario)
    committed = scenario in {"commit_after", "discovery_failure"}
    assert len(value["database"]["web_posts"]) == (2 if committed else 0)
    assert len(value["database"]["web_post_images"]) == (2 if committed else 0)
    assert len(value["media"]) == (2 if committed or scenario == "supervision" else 0)
    assert value["database"]["crawl_discovery_checkpoints"] == [] or scenario == "promotion_failure"
    trace = value["trace"]
    if scenario in {"upsert_failure", "commit_before", "sqlite_busy"}:
        assert ["content.exception", "FormalImportBeforeCommitError", False] in trace
        assert value["summary"]["import_result"]["reason"] == "sqlite_import_failed"
        assert value["summary"]["image_materialization"]["rolled_back_images"] == 2
        assert "discovery.enter" not in trace
        assert value["result"] == 2
    if scenario in {"upsert_failure", "commit_before", "supervision"}:
        assert "ROLLBACK" in trace
    if scenario in {"commit_after", "supervision"}:
        assert value["result"]["original"] is True
        assert value["result"]["exception"] == ("RuntimeError" if committed else "XhsRuntimeSupervisionError")
        assert "discovery.enter" not in trace
    if scenario == "commit_after":
        assert "content.in_transaction.False" in trace and "ROLLBACK" not in trace
    if scenario == "commit_before":
        assert "content.in_transaction.True" in trace
    if scenario == "sqlite_busy":
        assert ["sqlite.busy_timeout", 10] in trace
        assert "other.begin_immediate" in trace and "BEGIN IMMEDIATE" in trace
        assert "content.upsert.1" not in trace
        assert "database is locked" in value["summary"]["import_result"]["error"]
    if scenario == "promotion_failure":
        assert value["summary"]["image_materialization"]["rolled_back_images"] == 1
        assert "content.commit.enter" not in trace
    if scenario == "discovery_failure":
        assert value["result"] == 2
        assert value["summary"]["failure_reason"] == "discovery_checkpoint_write_failed"
        assert trace.index("content.commit.exit") < trace.index("discovery.enter")


@pytest.mark.parametrize("scenario", ["missing_stop", "missing_required", "followers_unobserved",
                                      "invalid_behavior", "missing_image", "terminal_block", "pagination_block"])
def test_c_gates_and_blockers(tmp_path, monkeypatch, capsys, scenario):
    value = run(tmp_path, monkeypatch, capsys, "xhs" if scenario == "terminal_block" else "weibo", scenario)
    summary = value["summary"]
    assert value["database"]["web_posts"] == [] and value["media"] == {}
    validation = summary["formal_validation"]
    if scenario in {"missing_required", "followers_unobserved"}:
        reason = "missing_author_id" if scenario == "missing_required" else "followers_not_observed"
        assert validation["invalid_reason_counts"][reason] == 2
    else:
        assert value["result"] == 2 and summary["import_completion_met"] is False
    if scenario in {"terminal_block", "pagination_block"}:
        assert validation["stop_reason"] == "runtime_failed"
        assert summary["runtime_blocker"]["reason"] == ("安全限制 300011" if scenario == "terminal_block" else "blocked_or_forbidden")
        assert validation["stop_detail"] == summary["runtime_blocker"]["reason"]


@pytest.mark.parametrize("scenario", ["success", "missing_stop", "count_0", "count_1", "count_1000000",
                                      "many_pages", "stagnant", "empty_page", "unverified_empty_first_page"])
def test_d_completion_requires_stop_evidence(tmp_path, monkeypatch, capsys, scenario):
    platform = "douyin" if scenario == "unverified_empty_first_page" else "weibo"
    value = run(tmp_path, monkeypatch, capsys, platform, scenario)
    summary = value["summary"]
    completed = scenario == "success"
    events = value["state"]["events"]
    has_stop = any(e["type"] == "adaptive_search_stopped" and e["details"].get("stop_reason") == "source_exhausted" for e in events)
    assert has_stop is (scenario in {"success", "unverified_empty_first_page"})
    assert summary["formal_validation"]["source_exhausted_met"] is completed
    assert summary["formal_validation"]["completion_met"] is completed
    assert summary["import_completion_met"] is completed
    assert value["result"] == (0 if completed else 2)
    assert value["envelope"][-1]["status"] == ("completed" if completed else "failed")


@pytest.mark.parametrize("scenario", ["success", "items_unresolved"])
def test_e_incomplete_campaign_records_still_write_markdown_and_keep_the_stop_reason(
    tmp_path, monkeypatch, capsys, scenario,
):
    """#91：累计摘要带中断轮次的批次检查点记录（只有产物路径、没有计数）时，完成轮次与扫描停止的失败轮次都写出
    summary.md 并按门禁退出；根读子摘要的同一组函数拿到子摘要里的停止原因。"""
    xhs_runner = importlib.import_module("xhs_runner")
    campaign = tmp_path / "campaign_summary.json"
    incomplete = {"platform": "xhs", "status": "incomplete", "ok": False,
                  "output": {"jsonl_files": [], "image_manifest_paths": [], "content_visit_paths": []}}
    campaign.write_text(json.dumps({"keyword": "青岛旅游", "records": [incomplete, incomplete]}))
    root = tmp_path / "run"

    value = drive(root, monkeypatch, capsys, "xhs", scenario, resume_summary=campaign)

    completed = scenario == "success"
    assert value["result"] == (0 if completed else 2)
    summary = value["summary"]
    assert [record["status"] for record in summary["records"]] == ["incomplete", "incomplete", "completed"]
    rows = [line for line in value["markdown"].decode().splitlines() if line.startswith("| 小红书 |")]
    assert rows[:2] == ["| 小红书 | incomplete | — | — | — | — | — | — |"] * 2
    assert rows[2].startswith("| 小红书 | completed | 2 | 2 | 0 | 2 | 0 |")
    assert "- `incomplete`：中断轮次由批次检查点写入的累计记录" in value["markdown"].decode()
    (envelope,) = value["envelope"]
    assert envelope["status"] == ("completed" if completed else "failed")
    assert envelope["summary"] == "<run>/batches/fixed/summary.json"
    # 根：从 stdout 的终态信封取子摘要路径，再按子摘要归类失败（scripts/xhs_runner.py 同一组函数）。
    stdout = json.dumps({**envelope, "summary": str(root / "batches/fixed/summary.json")})
    child = xhs_runner.load_child_summary(xhs_runner.extract_stdout_json(stdout)["summary"])
    fields = xhs_runner._terminal_failure_fields(stdout, child, exit_code=value["result"])
    if completed:
        assert fields == {"failure_type": "", "stop_reason": "source_exhausted", "stop_detail": "has_more_false",
                          "reason": "has_more_false"}
    else:
        assert fields == {"failure_type": "runtime_failed", "stop_reason": "runtime_failed",
                          "stop_detail": "xhs_search_items_unresolved", "reason": "xhs_search_items_unresolved"}


@pytest.mark.parametrize(
    ("platform", "scenario"),
    [("xhs", "success"), ("xhs", "items_unresolved"), ("weibo", "success"), ("weibo", "pagination_block")],
)
def test_e_markdown_failure_after_the_envelope_fails_the_child_and_keeps_the_child_summary(
    tmp_path, monkeypatch, capsys, platform, scenario,
):
    """#91 后续：终版 summary.json 写出后立即打印终态信封。之后 Markdown 抛异常照常从中间层入口传出（进程以 1 退出），
    根仍从 stdout 唯一的信封取到子摘要与其中的停止原因（小红书经 xhs_runner、通用平台经 crawl_runner 的同一组函数）。"""

    def broken_markdown(summary, path):
        raise KeyError("non_video_content_records")

    strict_patch(monkeypatch, collection, "write_markdown", broken_markdown)
    root = tmp_path / "run"

    with pytest.raises(KeyError):
        process.run_main_with_operator_interrupt(lambda: drive(root, monkeypatch, capsys, platform, scenario))

    stdout = capsys.readouterr().out
    batch_dir = root / "batches/fixed"
    summary_path = batch_dir / "summary.json"
    child = json.loads(summary_path.read_text())
    (envelope,) = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    assert envelope == reporting.terminal_summary_envelope(
        summary_path=summary_path, report_path=batch_dir / "summary.md", batch_dir=batch_dir, summary=child,
    )
    assert envelope["status"] == ("completed" if scenario == "success" else "failed")
    assert not (batch_dir / "summary.md").exists()
    exit_code = 1  # 未捕获异常：解释器以 1 退出
    if platform == "xhs":
        xhs_runner = importlib.import_module("xhs_runner")
        loaded = xhs_runner.load_child_summary(xhs_runner.extract_stdout_json(stdout)["summary"])
        assert loaded == child
        fields = xhs_runner._terminal_failure_fields(stdout, loaded, exit_code=exit_code)
        if scenario == "success":
            assert loaded["formal_validation"]["stop_reason"] == "source_exhausted"
            assert fields == {"failure_type": "runtime_failed", "stop_reason": "runtime_failed",
                              "stop_detail": "xhs_child_exit_1", "reason": "xhs_child_exit_1"}
        else:
            assert fields == {"failure_type": "runtime_failed", "stop_reason": "runtime_failed",
                              "stop_detail": "xhs_search_items_unresolved", "reason": "xhs_search_items_unresolved"}
    else:
        crawl_runner = importlib.import_module("crawl_runner")
        assert crawl_runner.find_artifact_paths(stdout, None)[2] == str(summary_path)
        assert crawl_runner.classify_attempt(exit_code=exit_code, stdout=stdout)["status"] != "completed"
        validation = child["formal_validation"]
        if scenario == "success":
            assert validation["stop_reason"] == "source_exhausted"
        else:
            assert (validation["stop_reason"], validation["stop_detail"]) == ("runtime_failed", "blocked_or_forbidden")


def test_guard_and_strict_patch(monkeypatch):
    with pytest.raises(AssertionError, match="patch 目标不存在"):
        strict_patch(monkeypatch, process, "不存在的入口", forbid)
    for operation in (lambda: subprocess.Popen(["never"]), lambda: process.run_command([])):
        with pytest.raises(pytest.fail.Exception, match="离线驱动漏拦"):
            operation()


# ---------- 五站内容事务与发现提交故障 ----------
# commit 前失败整批回滚、commit 后失败保留合法提交、发现提交失败不回滚已提交内容。
# 小红书的发现提交走批次 ACK，不在此重复。
SITES = ["bilibili", "weibo", "douyin", "zhihu", "xhs"]
CASES = [pytest.param(site, scenario, id=f"{site}-{scenario}")
         for site in SITES for scenario in ("commit_before", "commit_after", "discovery_failure")
         if not (site == "xhs" and scenario == "discovery_failure")]


@pytest.mark.parametrize(("platform", "scenario"), CASES)
def test_content_and_discovery_commit_faults_per_site(tmp_path, monkeypatch, capsys, platform, scenario):
    with monkeypatch.context() as patch:
        value = drive(tmp_path / "new", patch, capsys, platform, scenario)

    committed = scenario in {"commit_after", "discovery_failure"}
    trace = value["trace"]
    database = value["database"]
    assert len(database["web_posts"]) == len(database["web_post_images"]) == (2 if committed else 0)
    assert len(value["media"]) == (2 if committed else 0)
    if platform != "xhs":
        assert database["crawl_discovery_checkpoints"] == []
    assert "discovery.exit" not in trace
    if scenario == "commit_before":
        assert ["content.exception", "FormalImportBeforeCommitError", False] in trace
        assert "content.in_transaction.True" in trace and "ROLLBACK" in trace
        assert value["summary"]["import_result"]["reason"] == "sqlite_import_failed"
        assert value["summary"]["image_materialization"]["rolled_back_images"] == 2
        assert "discovery.enter" not in trace
        assert value["result"] == 2
    if scenario == "commit_after":
        assert "content.in_transaction.False" in trace and "ROLLBACK" not in trace
        assert value["result"]["exception"] == "RuntimeError"
        assert value["result"]["original"] is True
        assert "discovery.enter" not in trace
    if scenario == "discovery_failure":
        assert value["result"] == 2
        assert value["summary"]["failure_reason"] == "discovery_checkpoint_write_failed"
        assert trace.index("content.commit.exit") < trace.index("discovery.enter")
