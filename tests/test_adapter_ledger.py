"""T00 迁移台账工具的验收测试：规则完整性、可复现与产物 schema。"""

from __future__ import annotations

import hashlib
import json
import sys
from importlib import import_module
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "scripts" / "dev"
if str(DEV) not in sys.path:
    sys.path.insert(0, str(DEV))

ledger = import_module("adapter_ledger")
LEDGER_DIR = ROOT / "docs" / "adapter-ledger"
DISPOSITIONS = {"迁", "拆", "并", "薄", "退"}
CARDS = {f"T{n:02d}" for n in range(15)}


# ---------- 符号账：规则与枚举 ----------

def test_current_baseline_has_no_unmapped_definitions() -> None:
    result = ledger.build_symbols(ROOT)
    assert result["unmapped"] == []
    assert result["stat"]["total"] == sum(result["stat"][d] for d in DISPOSITIONS)
    assert result["stat"]["total"] >= 1234


def test_unmapped_definition_is_reported(tmp_path: Path) -> None:
    # 在临时源码树中增加一个没有规则的定义，生成必须报告而不是静默归类
    source = tmp_path / "src"
    target = source / "tools" / "MediaCrawler" / "tools"
    target.mkdir(parents=True)
    (target / "brand_new_helper.py").write_text("def brand_new_helper():\n    return 1\n")
    result = ledger.build_symbols(source, files=["tools/MediaCrawler/tools/brand_new_helper.py"])
    assert result["unmapped"] == [
        {"file": "tools/MediaCrawler/tools/brand_new_helper.py", "line": 1, "qualname": "brand_new_helper"}
    ]


def test_every_symbol_row_has_complete_fields() -> None:
    result = ledger.build_symbols(ROOT)
    for row in result["rows"]:
        assert set(row) >= {"file", "line", "qualname", "target", "disposition", "card", "tests", "note"}
        assert row["disposition"] in DISPOSITIONS
        assert row["card"] in CARDS
        if row["disposition"] == "退":
            assert row["target"] == "—"
        else:
            assert row["target"] and row["target"] != "—"


def test_exit_references_are_listed() -> None:
    # 退出符号被保留代码引用的位置必须全部列出，供 T12 切断
    result = ledger.build_symbols(ROOT)
    names = {item["symbol"] for item in result["exit_references"]}
    assert {"ProxyRefreshMixin", "AbstractCrawler", "create_ip_pool"} <= names
    for item in result["exit_references"]:
        assert item["referenced_by"], item


def test_committed_artifacts_are_reproducible() -> None:
    # 已提交的 JSON 与 C8 附录必须与重新生成的结果逐字节一致
    assert ledger.main(["symbols", "--check"]) == 0


def test_check_mode_detects_stale_artifact(tmp_path: Path) -> None:
    stale = tmp_path / "symbols.json"
    stale.write_text("{}\n")
    assert ledger.check_file(stale, "{\"rows\": []}\n") is False
    assert ledger.check_file(stale, "{}\n") is True


# ---------- 基线与资源散列 ----------

def test_baseline_records_three_heads_and_resource_hashes() -> None:
    baseline = json.loads((LEDGER_DIR / "baseline.json").read_text())
    for key in ("root_head", "fork_head", "upstream_head", "fork_upstream_merge_base"):
        assert len(baseline[key]) == 40
    resources = baseline["resources"]
    assert set(resources) == {"libs/douyin.js", "libs/zhihu.js", "libs/stealth.min.js", "LICENSE"}
    fork = ROOT / "tools" / "MediaCrawler"
    for relative, digest in resources.items():
        assert hashlib.sha256((fork / relative).read_bytes()).hexdigest() == digest


def test_resource_hashes_match_spec_c0() -> None:
    spec = (ROOT / "docs" / "platform-adapter-specification.md").read_text()
    baseline = json.loads((LEDGER_DIR / "baseline.json").read_text())
    for digest in baseline["resources"].values():
        assert digest in spec


# ---------- 输入展开 ----------

ENTRYPOINTS = {
    "scripts/crawl_runner.py", "scripts/mediacrawler_crawl.py", "scripts/xhs_runner.py",
    "scripts/repair_xhs_posts.py", "scripts/repair_post_details.py", "scripts/login_warmup.py",
    "scripts/mediacrawler_login_warmup.py",
}


def test_inputs_cover_all_entrypoints_and_match_source() -> None:
    inputs = json.loads((LEDGER_DIR / "inputs.json").read_text())
    assert ENTRYPOINTS <= set(inputs["cli"])
    fresh = ledger.build_inputs(ROOT)
    assert inputs == fresh


def test_cli_defaults_are_preserved_verbatim() -> None:
    cli = json.loads((LEDGER_DIR / "inputs.json").read_text())["cli"]
    crawl = {arg["flags"][0]: arg for arg in cli["scripts/mediacrawler_crawl.py"]}
    assert crawl["--keyword"]["default"] == "'青岛旅游'"
    assert crawl["--timeout-per-platform"]["default"] == "180"
    assert crawl["--xhs-post-interaction"]["choices"] == "('none', 'comment-scroll', 'like-one', 'random')"
    runner = {arg["flags"][0]: arg for arg in cli["scripts/crawl_runner.py"]}
    assert runner["--max-jobs"]["default"] == "3"


def test_env_names_have_producers_or_consumers() -> None:
    env = json.loads((LEDGER_DIR / "inputs.json").read_text())["env"]
    assert "TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS" in env
    for name, item in env.items():
        assert name.startswith("TRIPPOSTCOLLECT_") or name == "TRIPPOST_PROJECT_ROOT"
        assert item["files"], name


# ---------- 测试节点台账 ----------

def test_tests_ledger_schema() -> None:
    data = json.loads((LEDGER_DIR / "tests.json").read_text())
    assert set(data) >= {"root", "fork", "collected_at_root_head"}
    for side in ("root", "fork"):
        assert data[side]["nodes"], side
        for node in data[side]["nodes"]:
            assert set(node) >= {"node_id", "lane", "markers", "source_file", "target_file", "protects", "card"}
            assert node["card"] in CARDS
    lanes = {node["lane"] for node in data["root"]["nodes"]}
    assert lanes <= {"component", "socket", "installation", "os"}
    assert {node["lane"] for node in data["fork"]["nodes"]} == {"fork"}


def test_fork_node_count_matches_ci_expectation() -> None:
    data = json.loads((LEDGER_DIR / "tests.json").read_text())
    run_matrix = (ROOT / "scripts" / "ci" / "run_matrix.py").read_text()
    expected = int(run_matrix.split("FORK_EXPECTED_TESTS = ", 1)[1].split("\n", 1)[0])
    assert len(data["fork"]["nodes"]) == expected


def test_root_lane_assignment_follows_markers() -> None:
    data = json.loads((LEDGER_DIR / "tests.json").read_text())
    for node in data["root"]["nodes"]:
        markers = set(node["markers"])
        if "macos_process" in markers:
            assert node["lane"] == "os"
        elif "local_socket" in markers:
            assert node["lane"] == "socket"
        elif "installation" in markers:
            assert node["lane"] == "installation"
        else:
            assert node["lane"] == "component"


def test_tests_collector_refuses_production_checkout() -> None:
    with pytest.raises(SystemExit):
        ledger.main(["tests", "--source", str(ROOT)])
