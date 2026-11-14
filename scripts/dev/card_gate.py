"""在本机沙箱中对当前工作树做改动检查与分 lane 测试，并与基线提交的失败集合比较；终端只输出摘要，明细保存在临时报告。"""

from __future__ import annotations

import argparse
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


ROOT = Path(__file__).resolve().parents[2]
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


def json_text(value):
    """报告统一编码、缩进、键顺序及末尾换行。"""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def changed_files(root, names):
    """解析 NUL 分隔文件名；删除文件计入清单，只检查现存 Python 文件。"""
    files = sorted({p for p in names.split("\0") if p and p != "temp" and not p.startswith("temp/")})
    python = [p for p in files if p.endswith(".py") and (root / p).is_file()]
    return {"root": files, "compile": python, "ruff": python}


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


def compare_lane(current, baseline):
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
    for label, result in (("本次", current), ("基线", baseline)):
        counts = result["counts"]
        if (result["returncode"] not in (0, 1, 2)
                or (result["returncode"] != 0 and not result["failures"])
                or counts.get("selected", 0) == 0
                or (counts.get("collection_errors", 0) and not result["failures"])):
            errors.append(label + "测试未完整运行")
    return {"passed": not errors, "added": added, "removed": removed,
            "notes": notes, "errors": errors}


def cache_identity(root_commit, script_hash):
    return {"root_commit": root_commit, "script_hash": script_hash}


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


def run_tests(pristine, output, sandbox, python, sources):
    """每组生成独立白名单副本，避免前一组测试留下的修改影响下一组。"""
    matrix = load_module(pristine / "scripts/ci/run_matrix.py", "card_gate_matrix")
    lanes = load_module(pristine / "tests/run_lanes.py", "card_gate_lanes")
    results = {}
    for lane in ROOT_LANES:
        destination = output / f"source-{lane}"
        sources.append(destination)
        source = matrix.fresh_source(pristine, destination)
        report = output / lane
        report.mkdir(parents=True)
        environment = clean_environment(report, lanes.runtime_path())
        environment.update(TPC_LANE_COUNTS=str(report / "counts.json"),
                           TPC_EXEC_GUARD_REPORT=str(report / "execution-guard.json"))
        environment["PYTHONPATH"] = os.pathsep.join(str(source / p) for p in ("src", "tests", "scripts"))
        command = [python, "-m", "pytest", "tests", "-m", lanes.EXPRESSIONS[lane],
                   "--strict-markers", "-p", "pytest_asyncio.plugin", "-p", "run_lanes",
                   "-p", "support.execution_guard", "-o", "xfail_strict=true",
                   "--basetemp", report / "t/pytest", "--junitxml", report / "pytest.xml",
                   "-o", f"cache_dir={report / 'cache'}", "-q"]
        # 保留文件字段，准确恢复任意测试类名的 nodeid，不依赖 Test 前缀猜测。
        command.extend(["-o", "junit_family=xunit1"])
        code = sandbox.run(command, source, report / "pytest.log", environment=environment)
        counts_path, xml_path = report / "counts.json", report / "pytest.xml"
        if not counts_path.is_file() or not xml_path.is_file():
            raise RuntimeError(f"{lane} 未生成完整计数/JUnit，见 {report}")
        results[lane] = junit_result(xml_path, json.loads(counts_path.read_text()), code)
    return results


def git_bytes(root, *arguments):
    """唯一不套沙箱的子进程类别：只读 Git 查询。"""
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("只读 Git 查询失败：" + " ".join(arguments))
    return result.stdout


def git_text(root, *arguments):
    return git_bytes(root, *arguments).decode().strip()


def archive_source(root, base, destination):
    """从对象库导出基线，不 checkout、不修改索引或引用。"""
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(git_bytes(root, "archive", base))) as archive:
        archive.extractall(destination, filter="data")


def render_summary(report):
    """三态结论始终在最后一行；跳过测试不能成为通过。"""
    lines = ["沙箱 canary：" + ("通过" if report.get("canary", {}).get("passed") else "未通过")]
    if "frozen" in report:
        lines.append("冻结文件：" + ("通过" if report["frozen"] else "未通过"))
    if "files" in report:
        item = report["files"]
        lines.append(f"改动文件：{len(item['root'])}；"
                     f"编译 {len(item['compile'])}，ruff {len(item['ruff'])}；失败 {item['failures']}")
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
        common = Path(git_text(ROOT, "rev-parse", "--git-common-dir"))
        common = (ROOT / common).resolve()
        checkout = common.parent
        base = git_text(ROOT, "rev-parse", "--verify", (args.base or git_text(
            ROOT, "merge-base", "HEAD", "main")) + "^{commit}")
        python = ROOT / ".venv/bin/python"
        backend = sandbox_backend()
        backend.preflight()
        if not python.is_file():
            raise RuntimeError("缺少工作树解释器")
        lanes = load_module(ROOT / "tests/run_lanes.py", "card_gate_runtime")
        home = Path.home().resolve()
        probe_checkout, profile_probes = canary_profile_probes(output, backend.PROFILE_STORES)
        policy = backend.prepare(output, (ROOT, checkout, probe_checkout), home, lanes.runtime_path())
        sandbox = Sandbox(backend, policy, clean_environment(output / "control", lanes.runtime_path()))
        report.update(base=base, sandbox=backend.NAME, commands=sandbox.commands)
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

        report["frozen"] = check("frozen", [python, "scripts/verify_frozen_files.py"])[0] == 0
        if not report["frozen"]:
            report["errors"].append("冻结文件校验失败")
        names = git_bytes(ROOT, "diff", "--name-only", "-z", base).decode() + git_bytes(
            ROOT, "ls-files", "--others", "--exclude-standard", "-z").decode()
        files = changed_files(ROOT, names)
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

        if not args.skip_tests:
            identity = cache_identity(base, hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
            cache = common / "card-gate" / f"baseline-{base}.json"
            cached = json.loads(cache.read_text()) if cache.is_file() else {}
            if args.refresh_baseline or not cache_valid(cached, identity, ROOT_LANES):
                pristine = output / "baseline-archive"
                sources.append(pristine)
                archive_source(ROOT, base, pristine)
                baseline = run_tests(pristine, output / "baseline", sandbox, python, sources)
                cached = {"identity": identity, "lanes": baseline}
                # 缓存是编排进程唯一写入临时根以外的报告，不经子进程执行。
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json_text(cached), encoding="utf-8")
                report["cache"] = "重建 " + str(cache)
            else:
                report["cache"] = "命中 " + str(cache)
            current = run_tests(ROOT, output / "current", sandbox, python, sources)
            report["tests"] = {}
            for lane, result in current.items():
                baseline = cached["lanes"][lane]
                comparison = compare_lane(result, baseline)
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
        Path(report["report_path"]).write_text(json_text(report), encoding="utf-8")
        (output / "summary.txt").write_text(summary + "\n", encoding="utf-8")
        print(summary)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
