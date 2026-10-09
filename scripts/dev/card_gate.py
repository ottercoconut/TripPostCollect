"""在本机沙箱中复核迁移卡片；终端只输出摘要，明细保存在临时报告。"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
import difflib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET

import adapter_ledger as ledger
from adapter_ledger_rules import FORK_BRIDGE_FILES


ROOT = Path(__file__).resolve().parents[2]
FORK = "tools/MediaCrawler"
ROOT_LANES = ("component", "installation", "os")


def sandbox_backend(platform=None):
    """按平台选择沙箱后端：macOS 为 Seatbelt（sandbox_macos），Linux 为 bubblewrap（sandbox_linux）。"""
    platform = platform or sys.platform
    if platform == "darwin":
        import sandbox_macos as backend
    elif platform.startswith("linux"):
        import sandbox_linux as backend
    else:
        raise RuntimeError(f"卡片闸门不支持的平台：{platform}")
    return backend


CANARY = '''
import errno, json, os, socket, subprocess, sys
from pathlib import Path
results = {}
spec = json.loads(sys.argv[3])
allowed = set(spec["denied_errnos"])
def denied(name, operation):
    try:
        operation()
    except OSError as exc:
        results[name] = {"denied": exc.errno in allowed, "errno": exc.errno}
    else:
        results[name] = {"denied": False}
for host, port in (("127.0.0.1", 9), ("192.0.2.1", 443)):
    def connect():
        with socket.socket() as connection:
            connection.settimeout(2)
            connection.connect((host, port))
    denied(host, connect)
for index, executable in enumerate(spec["exec"]):
    denied("exec" if index == 0 else f"exec{index}",
           lambda executable=executable: subprocess.run([executable], check=False, capture_output=True))
if not spec["exec"]:
    results["exec"] = {"denied": True, "absent": True}
probe = Path(sys.argv[1]) / sys.argv[2]
def write_probe():
    with probe.open("x") as stream:
        stream.write("canary")
    probe.unlink()
denied("write", write_probe)
if spec["profile_exists"]:
    denied("profile", lambda: os.listdir(spec["profile"]))
else:
    results["profile"] = {"denied": True, "absent": True}
# 项目平台登录资料探针：临时根内按真实布局构造，目录缺失时 listdir 得 ENOENT，同样判未通过。
for index, path in enumerate(spec.get("project_profiles", [])):
    denied(f"project_profile{index}", lambda path=path: os.listdir(path))
print(json.dumps(results))
sys.exit(0 if all(item["denied"] for item in results.values()) else 2)
'''


def load_module(path, name):
    """只装载开发工具或测试运行器的定义，不调用它们的入口。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# macOS uchg 与 Linux chattr +i 的不可变标志实现；只依赖标准库。
FROZEN_FLAGS = load_module(ROOT / "scripts/ci/frozen_flags.py", "card_gate_frozen_flags")


def clean_environment(output, runtime_path):
    """从空环境建立隔离的 HOME、临时目录与测试插件设置。"""
    for name in ("home", "t"):
        (output / name).mkdir(parents=True, exist_ok=True)
    return {
        "PATH": runtime_path, "HOME": str(output / "home"), "TMPDIR": str(output / "t"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "LC_ALL": "en_US.UTF-8",
    }


class Sandbox:
    """所有非 Git 子进程共用一个策略；完整输出落盘，避免终端日志洪泛。"""

    def __init__(self, backend, policy, environment):
        self.backend = backend
        self.policy = policy
        self.environment = environment
        self.commands = []

    def run(self, command, cwd, log, *, environment=None, timeout=900):
        command = list(map(str, command))
        wrapped, descriptors = self.backend.wrap(self.policy, command)
        record = {"command": command, "cwd": str(cwd), "log": str(log)}
        self.commands.append(record)
        try:
            with log.open("w", encoding="utf-8") as stream:
                try:
                    result = subprocess.run(wrapped, cwd=cwd, env=environment or self.environment,
                                            stdout=stream, stderr=subprocess.STDOUT,
                                            check=False, timeout=timeout, pass_fds=descriptors)
                    record["returncode"] = result.returncode
                except subprocess.TimeoutExpired:
                    record["returncode"] = 124
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
        return record["returncode"]


def changed_files(root, root_names, fork_names):
    """解析 NUL 分隔文件名；删除文件计入清单，只检查现存 Python 文件。"""
    def names(value):
        return sorted({p for p in value.split("\0") if p and p != "temp" and not p.startswith("temp/")})
    root_files = [p for p in names(root_names) if p != FORK and not p.startswith(FORK + "/")]
    fork_files = names(fork_names)
    return {
        "root": root_files, "fork": fork_files,
        "compile": [p for p in root_files + [f"{FORK}/{p}" for p in fork_files]
                    if p.endswith(".py") and (root / p).is_file()],
        "ruff": [p for p in root_files if p.endswith(".py") and (root / p).is_file()],
    }


def ast_text(node):
    return ast.dump(node, include_attributes=False) if node is not None else None


def retired_source(root, path):
    """fork 删除批中随 fork/私有桥整体删除的原文件；T14-C 合并后基线不再含这些文件，可删除本函数。"""
    return (path.startswith(FORK + "/") or path in FORK_BRIDGE_FILES) and not (Path(root) / path).exists()


def ast_review(root, rows, reader, cards=(), pairs=(), retiring=False):
    """两侧共用按文件缓存的定位器，覆盖本卡迁移和前序已迁实现的变化。

    retiring 表示基线有 fork gitlink 而本次没有（T14-C）：基线仍在 fork/私有桥原位（pending）的定义
    随文件整体删除，若本次已由承接定义或收口规则定位（moved/exited），记为预期退出而不逐字对照；
    本次未定位的仍按“工作树未定位”报错。
    """
    inspect = ledger.definition_inspector(root)
    base_inspect = ledger.definition_inspector(root, reader)

    result = {"equal": [], "different": [], "missing": [], "pending": [], "exited": [], "retired": [],
              "cards": [], "previously_moved": 0}
    selected_cards = set(cards)

    def position(located):
        return {"state": located.state, "file": located.file, "qualname": located.qualname}

    def compare(item, old, new):
        if old is None or new is None:
            item["reason"] = "基线无定义" if old is None else "工作树未定位"
            result["missing"].append(item)
        elif ast_text(old) == ast_text(new):
            result["equal"].append(item)
        else:
            diff = list(difflib.unified_diff(
                ast.unparse(old).splitlines(), ast.unparse(new).splitlines(),
                fromfile="基线", tofile="工作树", lineterm="",
            ))
            item["diff"] = "\n".join(diff[:120])[:16000]
            item["truncated"] = len(diff) > 120 or len("\n".join(diff[:120])) > 16000
            result["different"].append(item)

    for row in rows:
        if cards and row["card"] not in cards:
            continue
        previous = ledger.locate_definition(root, row, base_inspect, rows=rows)
        located = ledger.locate_definition(root, row, inspect, rows=rows)
        if not cards and previous.state == located.state and ast_text(previous.node) == ast_text(located.node):
            continue
        selected_cards.add(row["card"])
        result["previously_moved"] += previous.state == "moved"
        item = {"definition": f"{row['file']}:{row['qualname']}",
                "base_location": position(previous), "location": position(located)}
        if (retiring and previous.state == "pending" and located.state in ("moved", "exited")
                and retired_source(root, previous.file)):
            result["retired"].append(item)
            continue
        if located.state in ("pending", "exited"):
            result[located.state].append(item.copy())
            # 原位 pending 仍单列；已迁实现退回原位且内容改变时不能掩盖差异。
            if (located.state == "exited" or previous.state != "moved"
                    or ast_text(previous.node) == ast_text(located.node)):
                continue
        compare(item, previous.node, located.node)
    for original, target in pairs:
        new = inspect(target)[0]
        for name, old in base_inspect(original)[0].items():
            compare({"definition": f"{original}:{name}",
                     "base_location": {"file": original, "qualname": name},
                     "location": {"file": target, "qualname": name}}, old, new.get(name))
    result["cards"] = sorted(selected_cards)
    return result


def junit_result(xml_path, counts, returncode=0):
    """合并计数与失败节点；JUnit 收集错误保留，预期失败的 skipped 不计入。"""
    tree = ET.parse(xml_path)
    failures = set()
    for case in tree.iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        classname, name = case.get("classname", ""), case.get("name", "")
        # pytest 的收集错误把相对文件名放在 name，且没有 classname。
        if case.get("file") and classname:
            file = case.get("file")
            module = file.removesuffix(".py").replace("/", ".")
            classes = classname.removeprefix(module).strip(".")
            nodeid = file + ("::" + classes.replace(".", "::") if classes else "") + "::" + name
        elif not classname:
            nodeid = name
        else:
            parts = classname.split(".")
            split = next((i for i, part in enumerate(parts) if part.startswith("Test")), len(parts))
            nodeid = "/".join(parts[:split]) + ".py"
            nodeid += "".join("::" + part for part in parts[split:]) + "::" + name
        failures.add(nodeid)
    for suite in tree.iter("testsuite"):
        for error in suite.findall("error"):
            failures.add("收集错误::" + error.get("name", suite.get("name", "未知")))
    return {"failures": sorted(failures), "counts": counts, "returncode": returncode}


def compare_lane(current, baseline, expected=None):
    """按失败集合判定；失败消失、通过数下降为显式变化提示。"""
    before, after = set(baseline["failures"]), set(current["failures"])
    added, removed = sorted(after - before), sorted(before - after)
    notes = []
    if removed:
        notes.append("变化：基线失败消失 " + "、".join(removed))
    if current["counts"].get("passed", 0) < baseline["counts"].get("passed", 0):
        notes.append("提示：通过数少于基线")
    errors = []
    if added:
        errors.append("新增失败 " + "、".join(added))
    if expected is not None and current["counts"].get("selected") != expected:
        errors.append(f"fork 选中数不符：应为 {expected}")
    for label, result in (("本次", current), ("基线", baseline)):
        counts = result["counts"]
        if (result["returncode"] not in (0, 1, 2)
                or (result["returncode"] != 0 and not result["failures"])
                or counts.get("selected", 0) == 0
                or (counts.get("collection_errors", 0) and not result["failures"])):
            errors.append(label + "测试未完整运行")
        if result.get("expected") is not None and counts.get("selected") != result["expected"]:
            errors.append(label + " fork 选中数不符")
    return {"passed": not errors, "added": added, "removed": removed,
            "notes": notes, "errors": errors}


def cache_identity(root_commit, fork_commit, script_hash, fork_python):
    return {"root_commit": root_commit, "fork_commit": fork_commit,
            "script_hash": script_hash, "fork_python": str(fork_python) if fork_python else None}


def cache_valid(cached, identity, lanes):
    return cached.get("identity") == identity and set(cached.get("lanes", {})) == set(lanes)


def canary_profile_probes(output, stores):
    """在临时根内按真实相对布局构造探针 checkout，交给后端与真实 checkout 同样遮蔽；不读写真实登录资料。"""
    checkout = output / "canary-checkout"
    paths = []
    for relative in stores:
        path = checkout / relative
        path.mkdir(parents=True)
        (path / "probe").write_text("canary", encoding="utf-8")
        paths.append(str(path))
    return checkout, paths


def removal_problems(root, progress):
    """T14-C 删除批的额外门禁：fork 目录须已从磁盘删除（只 git rm --cached 不算），台账不得残留 pending。"""
    problems = []
    if (Path(root) / FORK).exists():
        problems.append(f"fork 删除批要求 {FORK} 目录已从磁盘删除（仅 git rm --cached 不算删除）")
    pending = progress["counts"].get("pending", 0)
    if pending:
        problems.append(f"fork 删除批要求台账 progress 的 pending 为 0，实际 {pending}")
    return problems


def matrix_offline_tests():
    return tuple(load_module(ROOT / "scripts/ci/run_matrix.py", "card_gate_lane_matrix").FORK_OFFLINE_TESTS)


def selected_lanes(head_fork, offline_tests=None):
    """fork lane 只在本次仍有 fork gitlink 且 run_matrix 离线清单非空时运行（与 run_matrix.fork_lane_enabled
    一致）；基线与本次使用同一组 lane 才可比较。offline_tests 缺省取本次 checkout 的 run_matrix。"""
    if offline_tests is None:
        offline_tests = matrix_offline_tests()
    return (*ROOT_LANES, "fork") if head_fork and offline_tests else ROOT_LANES


def fork_plan(root, base, checkout, common, fork_python=None):
    """T14 过渡：按基线与本次（索引）的 fork gitlink 决定 lane、基线对象库与 fork 解释器。

    present：两侧都有 gitlink，fork lane 另需 run_matrix 离线清单非空（T14-B2 起为空，不运行）；removing：基线有、本次无（T14-C 本身），跳过 fork lane，
    基线 fork 源码从仍含该提交的对象库导出；absent：两侧都无。T14-C 合并后只剩 absent，可删除本函数。
    """
    fork_base, head_fork = ledger.fork_gitlink(root, base), ledger.fork_gitlink(root)
    if head_fork and not fork_base:
        raise RuntimeError("基线没有 fork gitlink，本次不得重新引入 fork")
    repository = None
    if fork_base:
        git_dir = (root / ledger.git_read(root, "rev-parse", "--git-dir")).resolve()
        repository = fork_repository(
            [root / FORK] if head_fork else [root / FORK, git_dir / "modules" / FORK,
                                             checkout / FORK, common / "modules" / FORK],
            fork_base)
    lanes = selected_lanes(head_fork)
    # fork 解释器只供 fork lane；清单为空不运行 fork lane 时不要求 fork 环境。
    return {"base": fork_base, "head": head_fork,
            "transition": "present" if head_fork else "removing" if fork_base else "absent",
            "repository": repository, "lanes": lanes,
            "python": (fork_python or checkout / FORK / ".venv/bin/python").absolute() if "fork" in lanes else None}


def fork_repository(candidates, commit):
    """在候选对象库中找到含基线 fork 提交者；本次已删除 fork 时可退到主 checkout 或子模块 gitdir。"""
    for candidate in candidates:
        if candidate.exists() and subprocess.run(
                ["git", "-C", str(candidate), "cat-file", "-e", f"{commit}^{{commit}}"],
                capture_output=True, check=False).returncode == 0:
            return candidate
    raise RuntimeError(f"基线含 fork gitlink {commit}，但以下位置均无该提交，无法导出基线："
                       + "、".join(map(str, candidates)))


def remove_source(path):
    """只解除本次临时源码副本的不可变标志，再删除副本。"""
    if not path.exists():
        return
    for directory, _, files in os.walk(path):
        for name in files:
            item = Path(directory) / name
            if not item.is_symlink() and FROZEN_FLAGS.is_immutable(item):
                FROZEN_FLAGS.clear_immutable(item)
    shutil.rmtree(path)


def run_tests(pristine, output, sandbox, python, fork_python, sources, selected):
    """每组生成独立白名单副本，避免前一组测试留下的修改影响下一组。"""
    matrix = load_module(pristine / "scripts/ci/run_matrix.py", "card_gate_matrix")
    lanes = load_module(pristine / "tests/run_lanes.py", "card_gate_lanes")
    results = {}
    for lane in selected:
        destination = output / f"source-{lane}"
        sources.append(destination)
        source = matrix.fresh_source(pristine, destination)
        report = output / lane
        report.mkdir(parents=True)
        environment = clean_environment(report, lanes.runtime_path())
        environment.update(TPC_LANE_COUNTS=str(report / "counts.json"),
                           TPC_EXEC_GUARD_REPORT=str(report / "execution-guard.json"))
        if lane == "fork":
            support = report / "support"
            support.mkdir()
            shutil.copyfile(source / "tests/run_lanes.py", support / "ci_lane_report.py")
            shutil.copyfile(source / "tests/support/execution_guard.py", support / "ci_execution_guard.py")
            environment["PYTHONPATH"] = matrix.fork_pythonpath(source, support)
            cwd = source / FORK
            command = matrix.fork_test_command(fork_python, cwd, report)
        else:
            environment["PYTHONPATH"] = os.pathsep.join(str(source / p) for p in ("src", "tests", "scripts"))
            cwd = source
            command = [python, "-m", "pytest", "tests", "-m", lanes.EXPRESSIONS[lane],
                       "--strict-markers", "-p", "pytest_asyncio.plugin", "-p", "run_lanes",
                       "-p", "support.execution_guard", "-o", "xfail_strict=true",
                       "--basetemp", report / "t/pytest", "--junitxml", report / "pytest.xml",
                       "-o", f"cache_dir={report / 'cache'}", "-q"]
        # 保留文件字段，准确恢复任意测试类名的 nodeid，不依赖 Test 前缀猜测。
        command.extend(["-o", "junit_family=xunit1"])
        code = sandbox.run(command, cwd, report / "pytest.log", environment=environment)
        counts_path, xml_path = report / "counts.json", report / "pytest.xml"
        if not counts_path.is_file() or not xml_path.is_file():
            raise RuntimeError(f"{lane} 未生成完整计数/JUnit，见 {report}")
        results[lane] = junit_result(xml_path, json.loads(counts_path.read_text()), code)
        if lane == "fork":
            results[lane]["expected"] = matrix.FORK_EXPECTED_TESTS
    return results


def git_bytes(root, *arguments):
    """唯一不套沙箱的子进程类别：只读 Git 查询。"""
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("只读 Git 查询失败：" + " ".join(arguments))
    return result.stdout


def archive_source(root, base, fork_base, destination, fork_repo=None):
    """从对象库导出基线，不 checkout、不修改索引或引用；基线无 fork gitlink 时只导出根。"""
    destination.mkdir(parents=True)
    archives = [(root, base, destination)]
    if fork_base:
        archives.append((fork_repo or root / FORK, fork_base, destination / FORK))
    for repository, revision, target in archives:
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(git_bytes(repository, "archive", revision))) as archive:
            archive.extractall(target, filter="data")


def render_summary(report):
    """三态结论始终在最后一行；跳过测试不能成为通过。"""
    lines = ["沙箱 canary：" + ("通过" if report.get("canary", {}).get("passed") else "未通过")]
    if "fork" in report:
        lines.append("fork：" + {"present": "基线与本次均有 gitlink（离线清单非空时含 fork lane）",
                                 "removing": "本次删除 gitlink（T14-C 过渡，跳过 fork lane，原位定义按预期退出核对）",
                                 "absent": "基线与本次均无 gitlink（无 fork lane）"}[report["fork"]["transition"]])
    if "ledger" in report:
        item = report["ledger"]
        lines.append(f"台账：总计 {item['progress']['total']}，{item['progress']['counts']}")
        for card, counts in item["cards"].items():
            lines.append(f"  {card}：{counts}")
        lines.append("台账四项：" + ("通过" if item["passed"] else "未通过"))
        lines.extend("  缺失：" + f"{r['file']}:{r['qualname']}" for r in item["progress"]["missing"][:20])
        if any(item["drift"].values()):
            lines.extend(ledger.json_text(item["drift"]).splitlines()[:20])
    if "frozen" in report:
        lines.append("冻结文件：" + ("通过" if report["frozen"] else "未通过"))
    if "files" in report:
        item = report["files"]
        lines.append(f"改动文件：根 {len(item['root'])}，fork {len(item['fork'])}；"
                     f"编译 {len(item['compile'])}，ruff {len(item['ruff'])}；失败 {item['failures']}")
    if "ast" in report:
        item = report["ast"]
        def names(key):
            return [r["definition"] for r in item[key]]

        lines.append(f"AST：逐字 {len(item['equal'])}，差异 {names('different')}，未定位 {names('missing')}；"
                     f"原位 pending {len(item['pending'])}，退出 {len(item['exited'])}，"
                     f"随 fork 删除预期退出 {len(item.get('retired', []))}；"
                     f"其中前序已迁 {item['previously_moved']}")
    if report.get("skip_tests"):
        lines.append("测试：未运行（--skip-tests）")
    for lane, item in report.get("tests", {}).items():
        counts = item["current"]["counts"]
        comparison = item["comparison"]
        lines.append(f"测试 {lane}：选中 {counts.get('selected', 0)}，通过 {counts.get('passed', 0)}，"
                     f"失败 {len(item['current']['failures'])}；"
                     + ("通过（失败集合与基线一致）" if comparison["passed"] and not comparison["removed"]
                        else "变化" if comparison["passed"] else "未通过"))
        lines.extend("  " + note for note in comparison["notes"] + comparison["errors"])
    if report.get("cache"):
        lines.append("测试基线：" + report["cache"])
    lines.append("JSON 报告：" + report["report_path"])
    errors = report.get("errors", [])
    if errors:
        lines.append("结论：未通过（" + "；".join(errors) + "）")
    elif report.get("skip_tests"):
        lines.append("结论：部分（测试未运行）")
    else:
        lines.append("结论：通过")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--card", action="append", default=[])
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--fork-python", type=Path)
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--refresh-baseline", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    # 报告根总是新建，且限定临时区域，避免编排进程写入真实数据路径。
    try:
        parent = args.output.resolve() if args.output else None
        temporary_roots = (Path(tempfile.gettempdir()).resolve(), Path("/private/tmp"), ROOT / "temp")
        if parent:
            if not any(parent.is_relative_to(path) for path in temporary_roots):
                raise ValueError("--output 必须位于系统临时目录或工作树 temp/ 下")
            parent.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix="tpc-card-gate-", dir=parent)).resolve()
    except (OSError, ValueError) as exc:
        print("JSON 报告：未生成（无法建立临时报告目录）")
        print(f"结论：未通过（闸门预检失败：{exc}）")
        return 2
    report = {"report_path": str(output / "report.json"), "skip_tests": args.skip_tests, "errors": []}
    sources = []
    code = 2
    try:
        common = Path(ledger.git_read(ROOT, "rev-parse", "--git-common-dir"))
        common = (ROOT / common).resolve()
        checkout = common.parent
        base = ledger.git_read(ROOT, "rev-parse", "--verify", (args.base or ledger.git_read(
            ROOT, "merge-base", "HEAD", "main")) + "^{commit}")
        plan = fork_plan(ROOT, base, checkout, common, args.fork_python)
        fork_base, head_fork, transition = plan["base"], plan["head"], plan["transition"]
        fork_repo, fork_python, lanes_selected = plan["repository"], plan["python"], plan["lanes"]
        report["fork"] = {"base": fork_base, "head": head_fork, "transition": transition,
                          "repository": str(fork_repo) if fork_repo else None, "lanes": list(lanes_selected)}
        python = ROOT / ".venv/bin/python"
        backend = sandbox_backend()
        backend.preflight()
        if not python.is_file():
            raise RuntimeError("缺少工作树解释器")
        if not args.skip_tests and fork_python and not fork_python.is_file():
            raise RuntimeError(f"fork 解释器不存在：{fork_python}")
        rows = ledger.load_symbols(ROOT)["rows"]
        if set(args.card) - {r["card"] for r in rows}:
            raise ValueError("未知卡号")
        pairs = []
        for pair in args.pair:
            original, separator, target = pair.partition(":")
            if not separator or not original or not target:
                raise ValueError("--pair 必须是原文件:新文件")
            for path in (original, target):
                if Path(path).is_absolute() or ".." in Path(path).parts:
                    raise ValueError("--pair 必须使用仓库内相对路径")
            pairs.append((original, target))
        lanes = load_module(ROOT / "tests/run_lanes.py", "card_gate_runtime")
        home = Path.home().resolve()
        probe_checkout, profile_probes = canary_profile_probes(output, backend.PROFILE_STORES)
        policy = backend.prepare(output, (ROOT, checkout, probe_checkout), home, lanes.runtime_path())
        sandbox = Sandbox(backend, policy, clean_environment(output / "control", lanes.runtime_path()))
        report.update(base=base, fork_base=fork_base, sandbox=backend.NAME, commands=sandbox.commands)
        canary_log = output / "canary.json"
        probe_name = f".card-gate-probe-{output.name}"
        canary_spec = {**backend.canary_spec(home, policy), "project_profiles": profile_probes}
        canary_spec = json.dumps(canary_spec, ensure_ascii=False)
        canary_code = sandbox.run([python, "-c", CANARY, ROOT, probe_name, canary_spec], ROOT, canary_log)
        # 仅在沙箱意外允许创建但拒绝删除时，由编排进程立即清理自己的探针。
        probe = ROOT / probe_name
        if probe.exists():
            probe.unlink()
            canary_code = 2
        report["canary"] = {"passed": canary_code == 0, "log": str(canary_log)}
        if canary_code:
            raise RuntimeError("沙箱 canary 未通过，已终止")
        report["canary"]["observations"] = json.loads(canary_log.read_text())

        def check(name, command):
            log = output / (name + ".log")
            return sandbox.run(command, ROOT, log), log

        checks = {}
        for name, arguments in (("symbols", ["symbols", "--check"]), ("inputs", ["inputs", "--check"]),
                                ("drift", ["drift"]), ("progress", ["progress", "--json"])):
            rc, log = check(name, [python, "scripts/dev/adapter_ledger.py", *arguments])
            checks[name] = {"returncode": rc, "log": str(log)}
        drift = json.loads((output / "drift.log").read_text())
        progress = json.loads((output / "progress.log").read_text())
        report["ledger"] = {"checks": checks, "drift": drift, "progress": progress, "cards": {},
                            "passed": not any(c["returncode"] for c in checks.values())
                            and not any(drift.values()) and progress["counts"]["missing"] == 0}
        if not report["ledger"]["passed"]:
            report["errors"].append("台账检查失败")
        if transition == "removing":
            report["fork"]["problems"] = removal_problems(ROOT, progress)
            report["errors"].extend(report["fork"]["problems"])
        report["frozen"] = check("frozen", [python, "scripts/verify_frozen_files.py"])[0] == 0
        if not report["frozen"]:
            report["errors"].append("冻结文件校验失败")
        root_names = git_bytes(ROOT, "diff", "--name-only", "-z", base).decode() + git_bytes(
            ROOT, "ls-files", "--others", "--exclude-standard", "-z").decode()
        fork_names = (git_bytes(ROOT / FORK, "diff", "--name-only", "-z", fork_base).decode() + git_bytes(
            ROOT / FORK, "ls-files", "--others", "--exclude-standard", "-z").decode()) if head_fork else ""
        files = changed_files(ROOT, root_names, fork_names)
        files["failures"] = []
        for index, path in enumerate(files["compile"]):
            if check(f"compile-{index}", [python, "-c", "import py_compile,sys; "
                     "py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)",
                     path, output / f"compile-{index}.pyc"])[0]:
                files["failures"].append("编译：" + path)
        if files["ruff"] and check("ruff", [python, "-m", "ruff", "check", "--no-cache", *files["ruff"]])[0]:
            files["failures"].append("ruff：" + "、".join(files["ruff"]))
        report["files"] = files
        if files["failures"]:
            report["errors"].append("改动文件检查失败")

        def reader(path):
            repository, revision, relative = ROOT, base, path
            if path.startswith(FORK + "/"):
                if not fork_base:
                    return ""
                repository, revision, relative = fork_repo, fork_base, path[len(FORK) + 1:]
            names = git_bytes(repository, "ls-tree", "--name-only", revision, "--", relative).decode().splitlines()
            return git_bytes(repository, "show", f"{revision}:{relative}").decode() if relative in names else ""

        report["ast"] = ast_review(ROOT, rows, reader, args.card, pairs, retiring=transition == "removing")
        counts = defaultdict(Counter)
        for row in progress["rows"]:
            if row["card"] in report["ast"]["cards"]:
                counts[row["card"]][row["state"]] += 1
        report["ledger"]["cards"] = dict(sorted(counts.items()))
        if report["ast"]["different"] or report["ast"]["missing"]:
            report["errors"].append("AST 存在差异或未定位定义")
        if not args.skip_tests:
            identity = cache_identity(base, fork_base, hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), fork_python)
            cache = common / "card-gate" / f"baseline-{base}-{fork_base or 'nofork'}.json"
            cached = json.loads(cache.read_text()) if cache.is_file() else {}
            if args.refresh_baseline or not cache_valid(cached, identity, lanes_selected):
                pristine = output / "baseline-archive"
                sources.append(pristine)
                archive_source(ROOT, base, fork_base, pristine, fork_repo)
                baseline = run_tests(pristine, output / "baseline", sandbox, python, fork_python, sources,
                                     lanes_selected)
                cached = {"identity": identity, "lanes": baseline}
                # 缓存是编排进程唯一写入临时根以外的报告，不经子进程执行。
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(ledger.json_text(cached), encoding="utf-8")
                report["cache"] = "重建 " + str(cache)
            else:
                report["cache"] = "命中 " + str(cache)
            current = run_tests(ROOT, output / "current", sandbox, python, fork_python, sources, lanes_selected)
            report["tests"] = {}
            for lane, result in current.items():
                baseline = cached["lanes"][lane]
                comparison = compare_lane(result, baseline, result.get("expected"))
                report["tests"][lane] = {"current": result, "baseline": baseline, "comparison": comparison}
                if not comparison["passed"]:
                    report["errors"].append(lane + "测试失败集合或计数不符合要求")
        code = 1 if report["errors"] or args.skip_tests else 0
    except (OSError, RuntimeError, ValueError, ET.ParseError, tarfile.TarError) as exc:
        report["errors"].append("闸门自身错误：" + str(exc))
        code = 2
    finally:
        if not args.keep:
            for source in sources:
                try:
                    remove_source(source)
                except OSError as exc:
                    report["errors"].append(f"临时副本清理失败：{source}：{exc}")
                    code = 2
        report["exitcode"] = code
        summary = render_summary(report)
        report["summary"] = summary
        Path(report["report_path"]).write_text(ledger.json_text(report), encoding="utf-8")
        (output / "summary.txt").write_text(summary + "\n", encoding="utf-8")
        print(summary)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
