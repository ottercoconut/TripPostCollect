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
import stat
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET

import adapter_ledger as ledger


ROOT = Path(__file__).resolve().parents[2]
FORK = "tools/MediaCrawler"


def sandbox_policy(temporary, checkouts, home):
    """纯函数：调用者传入规范路径，只允许临时根与设备目录写入。"""
    def quote(path):
        return json.dumps(str(path), ensure_ascii=False)

    rules = ["(version 1)", "(allow default)", "(deny network*)",
             "(allow network* (remote unix-socket))",
             '(deny process-exec (literal "/usr/bin/open") (literal "/usr/bin/osascript")',
             '  (regex #"(?i).*(chrome|chromium|safari|firefox|webkit|msedge|MiniBrowser).*"))']
    for relative in ("Google/Chrome", "Google/Chrome for Testing", "Google/ChromeForTesting", "Chromium"):
        path = Path(home) / "Library/Application Support" / relative
        rules.append(f"(deny file-read* file-write* (subpath {quote(path)}))")
    for checkout in sorted(set(map(Path, checkouts))):
        for relative in ("data", "outputs", f"{FORK}/browser_data"):
            rules.append(f"(deny file-write* (subpath {quote(checkout / relative)}))")
    rules.extend([
        "(deny file-write* (require-all",
        f"  (require-not (subpath {quote(temporary)}))",
        '  (require-not (subpath "/dev"))))',
    ])
    return "\n".join(rules) + "\n"


CANARY = '''
import errno, json, os, socket, subprocess, sys
from pathlib import Path
results = {}
def denied(name, operation):
    try:
        operation()
    except OSError as exc:
        results[name] = {"denied": exc.errno in (errno.EPERM, errno.EACCES), "errno": exc.errno}
    else:
        results[name] = {"denied": False}
for host, port in (("127.0.0.1", 9), ("192.0.2.1", 443)):
    def connect():
        with socket.socket() as connection:
            connection.settimeout(2)
            connection.connect((host, port))
    denied(host, connect)
denied("open", lambda: subprocess.run(["/usr/bin/open"], check=False, capture_output=True))
probe = Path(sys.argv[1]) / sys.argv[2]
def write_probe():
    with probe.open("x") as stream:
        stream.write("canary")
    probe.unlink()
denied("write", write_probe)
if sys.argv[4] == "1":
    denied("profile", lambda: os.listdir(sys.argv[3]))
else:
    results["profile"] = {"denied": True, "absent": True}
print(json.dumps(results))
sys.exit(0 if all(item["denied"] for item in results.values()) else 2)
'''


def load_module(path, name):
    """只装载开发工具或测试运行器的定义，不调用它们的入口。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    def __init__(self, policy, environment):
        self.policy = policy
        self.environment = environment
        self.commands = []

    def run(self, command, cwd, log, *, environment=None, timeout=900):
        command = list(map(str, command))
        wrapped = ["/usr/bin/sandbox-exec", "-f", str(self.policy), *command]
        record = {"command": command, "cwd": str(cwd), "log": str(log)}
        self.commands.append(record)
        with log.open("w", encoding="utf-8") as stream:
            try:
                result = subprocess.run(wrapped, cwd=cwd, env=environment or self.environment,
                                        stdout=stream, stderr=subprocess.STDOUT,
                                        check=False, timeout=timeout)
                record["returncode"] = result.returncode
            except subprocess.TimeoutExpired:
                record["returncode"] = 124
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


def ast_review(root, rows, reader, cards=(), pairs=()):
    """两侧共用按文件缓存的定位器，覆盖本卡迁移和前序已迁实现的变化。"""
    inspect = ledger.definition_inspector(root)
    base_inspect = ledger.definition_inspector(root, reader)

    result = {"equal": [], "different": [], "missing": [], "pending": [], "exited": [],
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
            "script_hash": script_hash, "fork_python": str(fork_python)}


def cache_valid(cached, identity):
    return cached.get("identity") == identity and set(cached.get("lanes", {})) == {
        "component", "installation", "os", "fork",
    }


def remove_source(path):
    """只解除本次临时源码副本的不可变标志，再删除副本。"""
    if not path.exists():
        return
    for directory, _, files in os.walk(path):
        for name in files:
            item = Path(directory) / name
            flags = item.lstat().st_flags
            if flags & stat.UF_IMMUTABLE:
                os.chflags(item, flags & ~stat.UF_IMMUTABLE, follow_symlinks=False)
    shutil.rmtree(path)


def run_tests(pristine, output, sandbox, python, fork_python, sources):
    """每组生成独立白名单副本，避免前一组测试留下的修改影响下一组。"""
    matrix = load_module(pristine / "scripts/ci/run_matrix.py", "card_gate_matrix")
    lanes = load_module(pristine / "tests/run_lanes.py", "card_gate_lanes")
    results = {}
    for lane in ("component", "installation", "os", "fork"):
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


def archive_source(root, base, fork_base, destination):
    """从两个对象库导出基线，不 checkout、不修改索引或引用。"""
    destination.mkdir(parents=True)
    for repository, revision, target in ((root, base, destination),
                                         (root / FORK, fork_base, destination / FORK)):
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(git_bytes(repository, "archive", revision))) as archive:
            archive.extractall(target, filter="data")


def render_summary(report):
    """三态结论始终在最后一行；跳过测试不能成为通过。"""
    lines = ["沙箱 canary：" + ("通过" if report.get("canary", {}).get("passed") else "未通过")]
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
                     f"原位 pending {len(item['pending'])}，退出 {len(item['exited'])}；"
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
        fork_base = ledger.git_read(ROOT, "ls-tree", base, FORK).split()[2]
        python = ROOT / ".venv/bin/python"
        fork_python = (args.fork_python or checkout / FORK / ".venv/bin/python").absolute()
        if not Path("/usr/bin/sandbox-exec").is_file() or not python.is_file():
            raise RuntimeError("缺少 Seatbelt 或工作树解释器")
        if not args.skip_tests and not fork_python.is_file():
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
        policy = output / "sandbox.sb"
        policy.write_text(sandbox_policy(output, (ROOT, checkout), Path.home().resolve()), encoding="utf-8")
        sandbox = Sandbox(policy, clean_environment(output / "control", lanes.runtime_path()))
        report.update(base=base, fork_base=fork_base, commands=sandbox.commands)
        profile = Path.home() / "Library/Application Support/Google/Chrome"
        canary_log = output / "canary.json"
        probe_name = f".card-gate-probe-{output.name}"
        canary_code = sandbox.run([python, "-c", CANARY, ROOT, probe_name,
                                   profile, "1" if profile.exists() else "0"], ROOT, canary_log)
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
        report["frozen"] = check("frozen", [python, "scripts/verify_frozen_files.py"])[0] == 0
        if not report["frozen"]:
            report["errors"].append("冻结文件校验失败")
        root_names = git_bytes(ROOT, "diff", "--name-only", "-z", base).decode() + git_bytes(
            ROOT, "ls-files", "--others", "--exclude-standard", "-z").decode()
        fork_names = git_bytes(ROOT / FORK, "diff", "--name-only", "-z", fork_base).decode() + git_bytes(
            ROOT / FORK, "ls-files", "--others", "--exclude-standard", "-z").decode()
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
                repository, revision, relative = ROOT / FORK, fork_base, path[len(FORK) + 1:]
            names = git_bytes(repository, "ls-tree", "--name-only", revision, "--", relative).decode().splitlines()
            return git_bytes(repository, "show", f"{revision}:{relative}").decode() if relative in names else ""

        report["ast"] = ast_review(ROOT, rows, reader, args.card, pairs)
        counts = defaultdict(Counter)
        for row in progress["rows"]:
            if row["card"] in report["ast"]["cards"]:
                counts[row["card"]][row["state"]] += 1
        report["ledger"]["cards"] = dict(sorted(counts.items()))
        if report["ast"]["different"] or report["ast"]["missing"]:
            report["errors"].append("AST 存在差异或未定位定义")
        if not args.skip_tests:
            identity = cache_identity(base, fork_base, hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), fork_python)
            cache = common / "card-gate" / f"baseline-{base}-{fork_base}.json"
            cached = json.loads(cache.read_text()) if cache.is_file() else {}
            if args.refresh_baseline or not cache_valid(cached, identity):
                pristine = output / "baseline-archive"
                sources.append(pristine)
                archive_source(ROOT, base, fork_base, pristine)
                baseline = run_tests(pristine, output / "baseline", sandbox, python, fork_python, sources)
                cached = {"identity": identity, "lanes": baseline}
                # 缓存是编排进程唯一写入临时根以外的报告，不经子进程执行。
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(ledger.json_text(cached), encoding="utf-8")
                report["cache"] = "重建 " + str(cache)
            else:
                report["cache"] = "命中 " + str(cache)
            current = run_tests(ROOT, output / "current", sandbox, python, fork_python, sources)
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
