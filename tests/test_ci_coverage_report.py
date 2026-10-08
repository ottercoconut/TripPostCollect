"""CI 五站覆盖报告（scripts/ci/coverage_report.py）的离线单测：用合成 junit 驱动真实声明。"""

from __future__ import annotations

from collections import defaultdict
import importlib.util
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("t13_ci_coverage_report", ROOT / "scripts/ci/coverage_report.py")
report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report)
SPEC = report.load_spec(ROOT / report.SPEC_PATH)
LANES = (*report.ROOT_LANES, "fork")


def synthetic_nodes() -> dict[str, set[str]]:
    """按声明为每个条目生成恰好满足要求的通过 node。"""
    nodes: dict[str, set[str]] = defaultdict(set)
    for _, _, entry in report.iter_entries(SPEC):
        lane = entry["lane"]
        if "node" in entry:
            nodes[lane].add(entry["node"])
        elif "function" in entry:
            params = entry.get("params") or [None]
            nodes[lane].update(entry["function"] + (f"[case-{token}]" if token else "") for token in params)
        else:
            count = entry.get("count") or entry.get("min_count") or entry.get("min_functions")
            nodes[lane].update(f"{entry['file']}::test_synthetic_{index}" for index in range(count))
    return nodes


def write_junit(path: Path, outcomes: dict[str, str]) -> None:
    suite = ET.Element("testsuite")
    for nodeid, outcome in sorted(outcomes.items()):
        module, _, name = nodeid.partition("::")
        case = ET.SubElement(suite, "testcase", classname=module.removesuffix(".py").replace("/", "."), name=name)
        if outcome != "passed":
            ET.SubElement(case, "failure" if outcome == "failed" else "skipped", message="redacted")
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8")


def reports_with(tmp_path: Path, change=None) -> Path:
    nodes = synthetic_nodes()
    for lane in LANES:
        outcomes = {nodeid: "passed" for nodeid in nodes.get(lane, ())}
        outcomes[f"tests/test_unrelated.py::test_extra_{lane}"] = "passed"
        if change:
            change(lane, outcomes)
        write_junit(tmp_path / lane / "junit-summary.xml", outcomes)
    return tmp_path


def test_all_declared_nodes_passing_produces_ok_report_with_site_and_lane_counts(tmp_path):
    reports = reports_with(tmp_path)

    result = report.write_report(ROOT / report.SPEC_PATH, reports, LANES)

    assert result["ok"] is True, result["problems"][:5]
    assert json.loads((reports / "coverage.json").read_text(encoding="utf-8")) == result
    for site in report.SITES:
        counts = result["status_counts"][site]
        assert sum(counts.values()) == 15
        assert result["sites"][site]["component"]["declared"] == result["sites"][site]["component"]["passed"] > 0
    assert result["status_counts"]["xhs"] == {"covered": 11, "na": 4, "shared": 0}
    assert result["cells"]["F08"]["weibo"]["status"] == "covered"
    assert result["lanes"]["component"]["failed"] == result["lanes"]["component"]["skipped"] == 0


@pytest.mark.parametrize("outcome", ["missing", "failed", "skipped"])
def test_any_declared_node_not_passing_fails_the_report(tmp_path, outcome):
    target = "tests/test_xhs_runtime_status.py::test_socket_is_rejected_without_blocking"

    def change(lane, outcomes):
        if target in outcomes:
            if outcome == "missing":
                del outcomes[target]
            else:
                outcomes[target] = outcome

    result = report.write_report(ROOT / report.SPEC_PATH, reports_with(tmp_path, change), LANES)

    assert result["ok"] is False
    assert any(target in problem or "lane socket 中缺失" in problem for problem in result["problems"])


def test_lost_parametrized_site_instance_is_reported(tmp_path):
    def change(lane, outcomes):
        for nodeid in [n for n in outcomes if n.startswith("tests/test_adapter_t11.py::test_b_full_chain[")
                       and n.endswith("-douyin]")]:
            del outcomes[nodeid]

    result = report.write_report(ROOT / report.SPEC_PATH, reports_with(tmp_path, change), LANES)

    assert result["ok"] is False
    assert any("test_b_full_chain: 缺少参数 douyin" in problem for problem in result["problems"])


def test_missing_lane_junit_is_a_problem_not_a_pass(tmp_path):
    reports = reports_with(tmp_path)
    (reports / "os" / "junit-summary.xml").unlink()

    result = report.write_report(ROOT / report.SPEC_PATH, reports, LANES)

    assert result["ok"] is False
    assert result["lanes"]["os"] is None
    assert any("lane os 无 junit 结果" in problem for problem in result["problems"])


def test_junit_classnames_and_parameters_round_trip_to_nodeids(tmp_path):
    path = tmp_path / "pytest.xml"
    write_junit(path, {
        "tests/platforms/xhs/test_xhs_login_contract.py::test_case[a.b-c]": "passed",
        "tests/test_root.py::test_failed": "failed",
        "tests/test_root.py::test_skipped": "skipped",
    })

    assert report.junit_outcomes(path) == {
        "tests/platforms/xhs/test_xhs_login_contract.py::test_case[a.b-c]": "passed",
        "tests/test_root.py::test_failed": "failed",
        "tests/test_root.py::test_skipped": "skipped",
    }
    assert report.split_nodeid("tests/x.py::test_y[worker-wb-weibo]") == ("tests/x.py", "test_y", "worker-wb-weibo")
    assert report.param_matches("wb", "worker-wb-weibo") and not report.param_matches("dy", "ready")


def test_spec_problems_reject_unknown_status_and_bare_na() -> None:
    broken = json.loads(json.dumps(SPEC))
    broken["responsibilities"]["F01"]["cells"]["weibo"] = {"status": "partial", "entries": [{}]}
    broken["responsibilities"]["F02"]["cells"]["weibo"] = {"status": "na"}

    problems = report.spec_problems(broken)

    assert any("F01/weibo: 非法状态 partial" in problem for problem in problems)
    assert any("F02/weibo: 不适用必须写依据" in problem for problem in problems)
