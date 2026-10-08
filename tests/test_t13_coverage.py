"""T13 五站覆盖守护：`tests/fixtures/t13_coverage.json` 声明的每个 node 必须在声明的 lane 被收集且不被跳过。

按 `run_lanes.EXPRESSIONS` 的四个表达式各做一次 `--collect-only`（与 CI 选择完全相同），
由 `support.coverage_probe` 记录标记。运行期 `pytest.skip()` 由 lane 计数校验（skipped=0）兜底，
CI 侧 `scripts/ci/coverage_report.py` 再核对 junit 通过。单站或总通过数不能替代逐格声明。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from run_lanes import EXPRESSIONS


ROOT = Path(__file__).resolve().parents[1]
FORK = ROOT / "tools" / "MediaCrawler"
# 依赖冻结旧实现 fixture 的对照测试文件（T05–T10），T14 删除时守护强制重新覆盖。
LEGACY_FILES = frozenset(f"tests/test_adapter_{name}.py" for name in (
    "t05", "t05_bridge", "t06", "t06_bridge", "t07", "t08", "t09", "t09_bridge", "t10"))
# 单站卡的对照文件只驱动该站，文件本身即本站证据。
CARD_SITES = {"t05": "weibo", "t05_bridge": "weibo", "t06": "douyin", "t06_bridge": "douyin",
              "t07": "zhihu", "t08": "bilibili", "t09": "xhs", "t09_bridge": "xhs", "t09_deps": "xhs"}
SITE_TOKENS = {
    "bilibili": ("bilibili", "bili"),
    "weibo": ("weibo", "wb"),
    "douyin": ("douyin", "dy"),
    "zhihu": ("zhihu",),
    "xhs": ("xhs", "xiaohongshu"),
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = _load("t13_coverage_report", ROOT / "scripts" / "ci" / "coverage_report.py")
run_matrix = _load("t13_run_matrix", ROOT / "scripts" / "ci" / "run_matrix.py")
SPEC = report.load_spec(ROOT / report.SPEC_PATH)


@pytest.fixture(scope="module")
def collected(tmp_path_factory) -> dict[str, dict[str, dict]]:
    """lane → nodeid → 探针记录；四个表达式各收集一次。"""
    base = tmp_path_factory.mktemp("t13-collect")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("TRIPPOSTCOLLECT_", "TPC_")) and key != "PYTHONPATH"}
    environment.update({
        "PYTHONPATH": os.pathsep.join(str(ROOT / name) for name in ("src", "tests", "scripts")),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1",
    })
    result = {}
    for lane, expression in EXPRESSIONS.items():
        output = base / f"{lane}.json"
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "-m", expression,
             "--strict-markers", "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin",
             "-p", "support.coverage_probe"],
            cwd=ROOT, env={**environment, "TPC_COVERAGE_PROBE": str(output)},
            capture_output=True, text=True, timeout=600, check=False,
        )
        assert completed.returncode == 0, completed.stdout[-3000:] + completed.stderr[-3000:]
        result[lane] = {item["nodeid"]: item for item in json.loads(output.read_text(encoding="utf-8"))}
    return result


def _root_entries():
    return [(responsibility, owner, entry) for responsibility, owner, entry in report.iter_entries(SPEC)
            if entry["lane"] != "fork"]


def test_matrix_declares_every_responsibility_for_five_sites() -> None:
    assert report.spec_problems(SPEC) == []
    for responsibility in ("F11", "F12", "F13", "F14", "F15"):
        covered = [site for site, cell in SPEC["responsibilities"][responsibility]["cells"].items()
                   if cell["status"] != "na"]
        assert len(covered) == 1, responsibility
    # 每格只能是 covered/shared/na，不得残留 partial、gap 或其他临时状态。
    statuses = {cell["status"] for block in SPEC["responsibilities"].values() for cell in block["cells"].values()}
    assert statuses <= report.STATUSES == {"covered", "shared", "na"}


def test_legacy_equivalence_flag_marks_exactly_frozen_comparison_tests() -> None:
    # T14 删除冻结对照测试时，守护会因缺失这些 node 而强制重新覆盖，这是预期行为。
    for responsibility, owner, entry in report.iter_entries(SPEC):
        reference = entry.get("function") or entry.get("node") or entry["file"]
        assert bool(entry.get("legacy_equivalence")) == (reference.split("::")[0] in LEGACY_FILES), (
            responsibility, owner, reference)


def test_declared_nodes_run_in_declared_lane_without_skip(collected) -> None:
    problems = []
    for responsibility, owner, entry in _root_entries():
        matched = report.match_entry(entry, collected[entry["lane"]])
        problems += [f"{responsibility}/{owner}: {item}" for item in report.entry_problems(entry, matched)]
        for nodeid in matched:
            item = collected[entry["lane"]][nodeid]
            if item["skip"] or item["xfail"] or any(condition is not False for condition in item["skipif"]):
                problems.append(f"{responsibility}/{owner}: {nodeid} 带 skip/xfail 或非假 skipif")
            other = [lane for lane in collected if lane != entry["lane"] and nodeid in collected[lane]]
            if other:
                problems.append(f"{responsibility}/{owner}: {nodeid} 同时落在 {other}")
    assert problems == []


def _function_source(path: str, function: str) -> str:
    source = (ROOT / path).read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            return "\n".join(ast.get_source_segment(source, part) or "" for part in (*node.decorator_list, node))
    raise AssertionError(f"{path} 中没有 {function}")


def _site_evidence(site: str, nodeid: str) -> bool:
    path, function, params = report.split_nodeid(nodeid)
    pattern = re.compile(r"(?<![0-9A-Za-z])(" + "|".join(SITE_TOKENS[site]) + r")(?![0-9A-Za-z])", re.I)
    if f"/platforms/{site}/" in path or CARD_SITES.get(Path(path).stem.removeprefix("test_adapter_")) == site:
        return True
    for text in (Path(path).name.replace("_", " "), function.replace("_", " "), params or ""):
        if pattern.search(text):
            return True
    return bool(pattern.search(_function_source(path, function)))


def test_site_cells_reference_their_site(collected) -> None:
    """covered 格的每个条目必须有本站证据，防止拿他站或无关用例充数；例外须写 site_basis。"""
    problems = []
    for responsibility, owner, entry in _root_entries():
        cell = SPEC["responsibilities"].get(responsibility, {}).get("cells", {}).get(owner)
        if cell is None or cell["status"] != "covered" or entry.get("site_basis"):
            continue
        matched = report.match_entry(entry, collected[entry["lane"]])
        if not any(_site_evidence(owner, nodeid) for nodeid in matched):
            problems.append(f"{responsibility}/{owner}: {entry.get('function') or entry.get('node') or entry['file']}")
    assert problems == []


def test_issue_1_marker_sets_are_exact(collected) -> None:
    block = SPEC["issues"]["1"]
    for marker, expected in block["markers"].items():
        lane = expected["lane"]
        marked = {nodeid for nodeid, item in collected[lane].items() if marker in item["markers"]}
        declared = {entry["node"] for entry in block["nodes"] if entry.get("marker") == marker}
        assert marked == declared and len(marked) == expected["count"], marker
        assert not any(marker in item["markers"] for other, nodes in collected.items() if other != lane
                       for item in nodes.values()), marker


def test_issue_2_lease_exit_nodes_are_complete(collected) -> None:
    (entry,) = SPEC["issues"]["2"]["files"]
    matched = report.match_entry(entry, collected["component"])
    assert len(matched) == entry["count"] == 68
    assert all(not report.match_entry(entry, collected[lane]) for lane in ("socket", "installation", "os"))


def test_fork_nodes_remain_in_offline_list() -> None:
    if not FORK.is_dir():
        # T14-C 删除 fork 后不再有 fork lane：声明中残留的 fork 条目必须一并删除。
        assert [entry for _, _, entry in report.iter_entries(SPEC) if entry["lane"] == "fork"] == []
        return
    offline = set(run_matrix.FORK_OFFLINE_TESTS)
    total = 0
    for responsibility, owner, entry in report.iter_entries(SPEC):
        if entry["lane"] != "fork":
            continue
        assert entry["file"] in offline, (responsibility, entry["file"])
        tree = ast.parse((FORK / entry["file"]).read_text(encoding="utf-8"))
        functions = [node.name for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")]
        assert len(functions) >= entry["min_functions"], entry["file"]
        total += 1
    assert total == len(offline)
