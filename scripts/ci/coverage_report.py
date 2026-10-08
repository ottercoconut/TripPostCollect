"""五站覆盖声明的匹配与 CI 报告（T13）。

声明位于 `tests/fixtures/t13_coverage.json`。同一套匹配规则供两处使用：
- `tests/test_t13_coverage.py` 在收集阶段核对声明 node 落在声明的 lane 且不带 skip/xfail；
- `scripts/ci/run_matrix.py` 在各 lane 执行后读取 junit，按 F×站、按站×lane 汇总并要求全部通过。
本模块只读 JSON/XML，不导入被测代码、不启动进程；产物不含 traceback 或捕获输出。
"""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
from typing import Any, Iterable, Iterator, Mapping
import xml.etree.ElementTree as ET


SITES = ("bilibili", "weibo", "douyin", "zhihu", "xhs")
RESPONSIBILITIES = tuple(f"F{number:02d}" for number in range(1, 16))
ROOT_LANES = ("component", "socket", "installation", "os")
STATUSES = {"covered", "shared", "na"}
SPEC_PATH = Path("tests/fixtures/t13_coverage.json")


def load_spec(path: Path) -> dict[str, Any]:
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    if spec.get("schema_version") != 1:
        raise ValueError("t13 覆盖声明版本错误")
    return spec


def split_nodeid(nodeid: str) -> tuple[str, str, str | None]:
    """返回 (文件, 函数, 参数 ID)；参数 ID 内可含任意字符。"""
    path, _, rest = nodeid.partition("::")
    function, bracket, params = rest.partition("[")
    return path, function, params[:-1] if bracket else None


def param_matches(token: str, params: str | None) -> bool:
    if params is None:
        return False
    return re.search(rf"(?<![0-9A-Za-z]){re.escape(token)}(?![0-9A-Za-z])", params) is not None


def iter_entries(spec: Mapping[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """逐条给出 (责任, 归属, 条目)；归属为站名、shared 或 issue。"""
    for responsibility, block in spec["responsibilities"].items():
        for entry in block.get("shared", []):
            yield responsibility, "shared", entry
        for site, cell in block["cells"].items():
            for entry in cell.get("entries", []):
                yield responsibility, site, entry
    for number, block in spec["issues"].items():
        for key in ("nodes", "functions", "files"):
            for entry in block.get(key, []):
                yield f"issue#{number}", "issue", entry


def match_entry(entry: Mapping[str, Any], lane_nodes: Iterable[str]) -> list[str]:
    """在给定 lane 的 nodeid 中找出条目覆盖的实例。"""
    if "node" in entry:
        return [nodeid for nodeid in lane_nodes if nodeid == entry["node"]]
    if "function" in entry:
        path, function = entry["function"].split("::")
        return [nodeid for nodeid in lane_nodes if split_nodeid(nodeid)[:2] == (path, function)]
    return [nodeid for nodeid in lane_nodes if split_nodeid(nodeid)[0] == entry["file"]]


def entry_problems(entry: Mapping[str, Any], matched: list[str]) -> list[str]:
    label = entry.get("node") or entry.get("function") or entry["file"]
    if not matched:
        return [f"{label}: lane {entry['lane']} 中缺失"]
    problems = []
    for token in entry.get("params", []):
        if not any(param_matches(token, split_nodeid(nodeid)[2]) for nodeid in matched):
            problems.append(f"{label}: 缺少参数 {token}")
    if "count" in entry and len(matched) != entry["count"]:
        problems.append(f"{label}: 期望 {entry['count']} 项，实际 {len(matched)}")
    if "min_count" in entry and len(matched) < entry["min_count"]:
        problems.append(f"{label}: 至少 {entry['min_count']} 项，实际 {len(matched)}")
    if "min_functions" in entry and len({split_nodeid(n)[1] for n in matched}) < entry["min_functions"]:
        problems.append(f"{label}: 至少 {entry['min_functions']} 个函数")
    return problems


def spec_problems(spec: Mapping[str, Any]) -> list[str]:
    """结构校验：五站 × F01–F15 齐全，状态与依据合法。"""
    problems = []
    if tuple(spec.get("sites", ())) != SITES:
        problems.append("站点集合必须是五站")
    if tuple(spec["responsibilities"]) != RESPONSIBILITIES:
        problems.append("责任必须恰为 F01–F15")
    for responsibility, block in spec["responsibilities"].items():
        if set(block["cells"]) != set(SITES):
            problems.append(f"{responsibility}: 必须逐站声明")
        for site, cell in block["cells"].items():
            status = cell.get("status")
            where = f"{responsibility}/{site}"
            if status not in STATUSES:
                problems.append(f"{where}: 非法状态 {status}")
            elif status == "na":
                if not cell.get("basis") or cell.get("entries"):
                    problems.append(f"{where}: 不适用必须写依据且不列 node")
            elif not cell.get("entries"):
                problems.append(f"{where}: 必须列出 node")
            if status == "shared" and not cell.get("basis"):
                problems.append(f"{where}: shared 必须写依据")
    return problems


def junit_outcomes(path: Path, *, prefix: str = "") -> dict[str, str]:
    """把 junit testcase 还原为 nodeid → passed/failed/skipped。"""
    outcomes = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        classname = case.attrib.get("classname", "")
        nodeid = f"{prefix}{classname.replace('.', '/')}.py::{case.attrib.get('name', '')}"
        tags = {child.tag for child in case}
        outcomes[nodeid] = ("failed" if tags & {"failure", "error"}
                            else "skipped" if "skipped" in tags else "passed")
    return outcomes


def build_report(spec: Mapping[str, Any], outcomes_by_lane: Mapping[str, Mapping[str, str] | None]) -> dict[str, Any]:
    """按声明汇总各 lane 的 junit 结果；任何声明 node 缺失或未通过都使 ok=False。"""
    problems: list[str] = list(spec_problems(spec))
    cells: dict[str, dict[str, Any]] = defaultdict(dict)
    sites: dict[str, dict[str, dict[str, int]]] = {site: defaultdict(lambda: {"declared": 0, "passed": 0})
                                                   for site in (*SITES, "shared", "issue")}
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for responsibility, owner, entry in iter_entries(spec):
        lane = entry["lane"]
        outcomes = outcomes_by_lane.get(lane)
        if outcomes is None:
            # fork 删除后不再运行 fork lane；残留的 fork 条目必须从声明中删除，不能静默跳过。
            problems.append(f"{responsibility}/{owner}: " + (
                "fork lane 未运行（fork 已删除），覆盖声明须移除 fork 条目"
                if lane == "fork" and lane not in outcomes_by_lane else f"lane {lane} 无 junit 结果"))
            continue
        matched = match_entry(entry, outcomes)
        entry_issue = entry_problems(entry, matched)
        failed = [nodeid for nodeid in matched if outcomes[nodeid] != "passed"]
        entry_issue += [f"{nodeid}: {outcomes[nodeid]}" for nodeid in failed]
        problems.extend(f"{responsibility}/{owner}: {item}" for item in entry_issue)
        cell = cells[responsibility].setdefault(owner, {"matched": 0, "passed": 0, "problems": 0})
        cell["matched"] += len(matched)
        cell["passed"] += len(matched) - len(failed)
        cell["problems"] += len(entry_issue)
        for nodeid in matched:
            if nodeid not in seen[(owner, lane)]:
                seen[(owner, lane)].add(nodeid)
                sites[owner][lane]["declared"] += 1
                sites[owner][lane]["passed"] += outcomes[nodeid] == "passed"
    for responsibility, block in spec["responsibilities"].items():
        for site, cell in block["cells"].items():
            summary = cells[responsibility].setdefault(site, {"matched": 0, "passed": 0, "problems": 0})
            summary["status"] = cell["status"]
    lanes = {lane: ({"total": len(outcomes),
                     **{key: sum(value == key for value in outcomes.values()) for key in ("passed", "failed", "skipped")}}
                    if outcomes is not None else None)
             for lane, outcomes in outcomes_by_lane.items()}
    status_counts = {site: {status: sum(spec["responsibilities"][f]["cells"][site]["status"] == status
                                        for f in RESPONSIBILITIES) for status in sorted(STATUSES)}
                     for site in SITES}
    return {
        "schema_version": 1,
        "ok": not problems,
        "problems": problems,
        "status_counts": status_counts,
        "cells": {key: dict(value) for key, value in cells.items()},
        "sites": {site: dict(value) for site, value in sites.items()},
        "lanes": lanes,
    }


def write_report(spec_path: Path, reports: Path, lanes: Iterable[str]) -> dict[str, Any]:
    """run_matrix 调用：读取各 lane 的 junit（优先脱敏摘要），写 reports/coverage.json。"""
    outcomes: dict[str, Mapping[str, str] | None] = {}
    for lane in lanes:
        directory = reports / lane
        junit = next((path for path in (directory / "junit-summary.xml", directory / "pytest.xml")
                      if path.exists()), None)
        outcomes[lane] = junit_outcomes(junit) if junit else None
    report = build_report(load_spec(spec_path), outcomes)
    (reports / "coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
