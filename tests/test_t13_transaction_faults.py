"""T13：内容事务与发现提交故障按五站逐一驱动新实现（W10）。

复用 T11 冻结对照的离线驱动（只读导入，不修改该锁定测试）；这里只跑新实现，
断言与 T11 微博分支相同：commit 前失败整批回滚、commit 后失败保留合法提交、
发现提交失败不回滚已提交内容。小红书的发现提交走批次 ACK（F02），不在此重复。
"""

from __future__ import annotations

import pytest

import test_adapter_t11 as t11

# T11 模块内的自动离线守卫只作用于定义模块；显式引入，使本模块同样禁止真实进程与网络。
offline_guard = t11.offline_guard

SITES = ["bilibili", "weibo", "douyin", "zhihu", "xhs"]
CASES = [pytest.param(site, scenario, id=f"{site}-{scenario}")
         for site in SITES for scenario in ("commit_before", "commit_after", "discovery_failure")
         if not (site == "xhs" and scenario == "discovery_failure")]


@pytest.mark.parametrize(("platform", "scenario"), CASES)
def test_content_and_discovery_commit_faults_per_site(tmp_path, monkeypatch, capsys, platform, scenario):
    with monkeypatch.context() as patch:
        value = t11.drive(t11.executor, tmp_path / "new", patch, capsys, platform, scenario)

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
