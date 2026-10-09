"""Run every lane, keeping failures separate and uploading no raw test data."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

# 按本文件旁的路径装载，基线副本与当前工作树各用各自的实现。
_FROZEN_FLAGS = importlib.util.spec_from_file_location("run_matrix_frozen_flags", Path(__file__).with_name("frozen_flags.py"))
frozen_flags = importlib.util.module_from_spec(_FROZEN_FLAGS)
_FROZEN_FLAGS.loader.exec_module(frozen_flags)
_COVERAGE = importlib.util.spec_from_file_location("run_matrix_coverage_report", Path(__file__).with_name("coverage_report.py"))
coverage_report = importlib.util.module_from_spec(_COVERAGE)
_COVERAGE.loader.exec_module(coverage_report)


FORK = "tools/MediaCrawler"
# T14 过渡：fork gitlink 存在且离线清单非空时才运行 fork 离线 lane。T14-B2 起原 34 个离线用例已按台账
# target_file 原名原断言移植到 tests/artifacts/test_staging.py 与 tests/application/test_discovery.py
# （映射见 tests/fixtures/t14_fork_test_mapping.json），清单为空，fork lane 不再运行。
# T14-C 删除 fork 后，本节常量与 fork lane 代码一并删除。
FORK_OFFLINE_TESTS: tuple[str, ...] = ()
FORK_EXPECTED_TESTS = 0
# 根环境选站装配验收：B站正式 article 路线与四站 worker 选站，均不得装载 fork 顶层包。
ROOT_ASSEMBLY_MODULES = tuple(f"trippostcollect.platforms.bilibili.{name}" for name in (
    "core", "client", "parser", "login", "signer",
))
WORKER_PLATFORM_CODES = ("wb", "dy", "zhihu", "xhs")
ASSEMBLY_CHECK = (
    "import importlib, sys\n"
    "from pathlib import Path\n"
    "from ci_execution_guard import install, canary\n"
    "install(); canary()\n"
    "from ci_lane_report import denied_network_probe\n"
    "denied_network_probe(Path(sys.argv[1]), 'installation')\n"
    "for name in sys.argv[3].split(','):\n"
    "    importlib.import_module(name)\n"
    "from trippostcollect.platforms.entry import load_crawler\n"
    "for code in sys.argv[4].split(','):\n"
    "    load_crawler(code)\n"
    "fork = str(Path(sys.argv[2]).resolve())\n"
    "loaded = sorted({name.split('.')[0] for name, module in list(sys.modules.items())\n"
    "                 if (getattr(module, '__file__', None) or '').startswith(fork)})\n"
    "if loaded:\n"
    "    raise SystemExit('fork packages loaded: ' + ','.join(loaded))\n"
)


def fork_gitlink_present(repository):
    """以外层仓库索引中是否仍有 fork gitlink 判定过渡状态；源码副本没有 .git，不能自行判断。"""
    result = subprocess.run(["git", "-C", str(repository), "ls-files", "-s", "--", FORK],
                            capture_output=True, text=True, check=True)
    return any(line.split()[0] == "160000" and line.endswith("\t" + FORK)
               for line in result.stdout.splitlines())


def fork_pythonpath(source, support):
    # 正式 export entrypoint 注入 src；脚本入口自身也提供 scripts。
    # 不加入根 tests，避免覆盖 fork 的 support/conftest。
    return os.pathsep.join(str(path) for path in (
        source / "tools/MediaCrawler", source / "src", source / "scripts", support,
    ))


def fork_lane_enabled(with_fork):
    """fork lane 需要 fork 仍在且离线清单非空；清单为空时 pytest 会改收 fork 全部用例，不得运行。"""
    return bool(with_fork and FORK_OFFLINE_TESTS)


def fork_test_command(python, fork, output):
    return [str(python), "-m", "pytest", *FORK_OFFLINE_TESTS,
            "-c", str(fork / "pyproject.toml"), "--rootdir", str(fork),
            "--confcutdir", str(fork), "-p", "pytest_asyncio.plugin",
            "-p", "ci_lane_report", "-p", "ci_execution_guard",
            "-o", "xfail_strict=true", "--basetemp", str(output / "t/pytest"),
            "--junitxml", str(output / "pytest.xml"),
            "-o", f"cache_dir={output / 'cache'}", "-q"]


def fresh_source(pristine, destination):
    """复制源码白名单；不继承运行产物、环境或凭证。"""
    excluded = {".git", ".venv", "venv", "data", "outputs", "temp", "browser_data",
                "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules",
                ".env", ".ssh", ".aws", ".config", ".netrc", ".git-credentials"}

    def ignore(directory, names):
        return [name for name in names if name in excluded or name.startswith(".env.")
                or name.endswith((".egg-info", ".pem", ".key"))
                or (Path(directory) / name).is_symlink()]

    destination.mkdir(parents=True, exist_ok=False)
    for name in ("src", "scripts", "tests", "config", "db", "docs", "tools"):
        # tools/ 只承载 fork 子模块；T14-C 删除后不存在，跳过即可。
        if name == "tools" and not (pristine / name).exists():
            continue
        shutil.copytree(pristine / name, destination / name, ignore=ignore,
                        copy_function=shutil.copyfile)
    for name in ("pyproject.toml", "uv.lock", "AGENTS.md", "build_support.py", "MANIFEST.in"):
        shutil.copyfile(pristine / name, destination / name)
    # 工作流本身也是被测配置（路径过滤须覆盖构建输入），只复制这一个文件。
    workflow = ".github/workflows/macos-test-lanes.yml"
    (destination / workflow).parent.mkdir(parents=True)
    shutil.copyfile(pristine / workflow, destination / workflow)
    if (pristine / "README.md").exists():
        shutil.copyfile(pristine / "README.md", destination / "README.md")
    restore_frozen(destination)
    return destination


def restore_frozen(source):
    manifest = json.loads((source / "config/frozen_files.json").read_text())
    if manifest.get("schema_version") != 1 or not manifest.get("files"):
        raise RuntimeError("冻结登记缺失或版本错误")
    for item in manifest["files"]:
        path = (source / item["path"]).resolve()
        if not path.is_relative_to(source.resolve()):
            raise RuntimeError("冻结路径越界")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise RuntimeError(f"冻结副本哈希不匹配: {item['path']}")
        if item.get("require_immutable_flag"):
            # macOS 恢复 uchg，Linux 恢复 chattr +i（需 root 或免密 sudo）。
            frozen_flags.set_immutable(path)
            if not frozen_flags.is_immutable(path):
                raise RuntimeError("冻结副本不可变标志未恢复")


def lane_command(runner, source, lane, output):
    if runner.parent.parent.resolve() == source.resolve():
        raise RuntimeError("runner 必须来自外层真实仓库")
    return [sys.executable, str(runner), "--source", str(source),
            "--lane", lane, "-o", str(output)]


def redact_junit(path):
    """Keep case outcomes/counts, never upload tracebacks or captured content."""
    tree = ET.parse(path)
    for element in tree.iter():
        element.text = None
        element.tail = None
        if element.tag in {"failure", "error", "skipped"}:
            element.attrib.clear()
            element.set("message", "details retained only in temporary local log")
        if element.tag == "property":
            element.attrib.clear()
    tree.write(path.with_name("junit-summary.xml"), encoding="utf-8", xml_declaration=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--reports", required=True, type=Path)
    parser.add_argument("--runner", required=True, type=Path)
    parser.add_argument("--fork-python", type=Path, help="仅 fork gitlink 仍存在时提供（T14 过渡）")
    args = parser.parse_args()
    source, reports = args.source.resolve(), args.reports.resolve()
    runner = args.runner.resolve()
    repository = Path(__file__).resolve().parents[2]
    if runner != repository / "tests/run_lanes.py":
        raise RuntimeError("必须使用本控制脚本所在外层仓库的 runner")
    with_fork = fork_gitlink_present(repository)
    if with_fork != (source / FORK).is_dir() or with_fork != (args.fork_python is not None):
        raise RuntimeError("fork gitlink、源码副本中的 fork 与 --fork-python 必须同时存在或同时缺省")
    sys.path.insert(0, str(runner.parent))
    from run_lanes import sandbox_policy, runtime_path
    from native_macos import hosted_only

    hosted_only()
    results = {}
    for lane in ("component", "socket", "installation", "os"):
        lane_source = fresh_source(source, source.parent / f"lane-{lane}")
        result = subprocess.run(lane_command(runner, lane_source, lane, reports / lane),
                                check=False, cwd=runner.parent.parent)
        results[lane] = {"returncode": result.returncode}
        summary = reports / lane / "result.json"
        if summary.exists():
            results[lane]["result"] = json.loads(summary.read_text())
        else:
            results[lane]["status"] = "missing_result"
        junit = reports / lane / "pytest.xml"
        if junit.exists():
            redact_junit(junit)
    # 根环境选站装配：B站 article 与四站 worker 只用根包，不调用抓取入口、不访问平台。
    # fork 删除后传入的 fork 路径不存在，“未装载 fork 包”检查仍然成立。
    assembly = reports / "assembly"
    assembly_source = fresh_source(source, source.parent / "lane-assembly")
    assembly.mkdir()
    for name in ("t", "home", "ci_support"):
        (assembly / name).mkdir()
    shutil.copyfile(assembly_source / "tests/run_lanes.py", assembly / "ci_support" / "ci_lane_report.py")
    shutil.copyfile(assembly_source / "tests/support/execution_guard.py",
                    assembly / "ci_support" / "ci_execution_guard.py")
    assembly_policy = assembly / "sandbox.sb"
    assembly_policy.write_text(sandbox_policy(assembly_source, assembly, "installation"))
    assembly_environment = {
        "HOME": str(assembly / "home"), "TMPDIR": str(assembly / "t"), "PATH": runtime_path(),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": os.pathsep.join([str(assembly_source / "src"), str(assembly / "ci_support")]),
        "TPC_LANE_COUNTS": str(assembly / "counts.json"),
        "TPC_EXEC_GUARD_REPORT": str(assembly / "execution-guard.json"),
    }
    with (assembly / "imports.log").open("w") as log:
        result = subprocess.run([
            "/usr/bin/sandbox-exec", "-f", str(assembly_policy), sys.executable, "-P", "-c", ASSEMBLY_CHECK,
            str(assembly), str(assembly_source / "tools/MediaCrawler"),
            ",".join(ROOT_ASSEMBLY_MODULES), ",".join(WORKER_PLATFORM_CODES),
        ], cwd=assembly_source, env=assembly_environment, stdout=log, stderr=subprocess.STDOUT,
           check=False, timeout=120)
    results["assembly"] = {"returncode": result.returncode, "modules": list(ROOT_ASSEMBLY_MODULES),
                           "worker_platforms": list(WORKER_PLATFORM_CODES)}
    if fork_lane_enabled(with_fork):
        results["fork_offline"] = run_fork_offline(source, reports, args.fork_python)
    # 五站覆盖：按 tests/fixtures/t13_coverage.json 核对各 lane（fork lane 运行时含 fork）的 junit，任一声明 node 缺失或未通过即失败。
    coverage = coverage_report.write_report(
        source / coverage_report.SPEC_PATH, reports,
        (*coverage_report.ROOT_LANES, *(("fork",) if fork_lane_enabled(with_fork) else ())))
    results["coverage"] = {"returncode": int(not coverage["ok"]), "problems": len(coverage["problems"]),
                           "status_counts": coverage["status_counts"]}
    (reports / "matrix.json").write_text(json.dumps(results, indent=2))
    return int(any(value["returncode"] != 0 or value.get("status") == "missing_result"
                   for value in results.values()))


def run_fork_offline(source, reports, fork_python):
    """fork 离线用例仍用 fork 自身环境（共享辅助的旧桥出口，T14-C 随目录删除）。"""
    from run_lanes import sandbox_policy, runtime_path, validate_counts

    output = reports / "fork"
    fork_source = fresh_source(source, source.parent / "lane-fork")
    output.mkdir()
    (output / "t").mkdir()
    (output / "home").mkdir()
    policy = output / "sandbox.sb"
    policy.write_text(sandbox_policy(fork_source, output, "installation"))
    environment = {
        "HOME": str(output / "home"), "TMPDIR": str(output / "t"),
        "PATH": runtime_path(),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    fork = fork_source / "tools/MediaCrawler"
    support = output / "ci_support"
    support.mkdir()
    shutil.copyfile(fork_source / "tests/run_lanes.py", support / "ci_lane_report.py")
    shutil.copyfile(fork_source / "tests/support/execution_guard.py",
                    support / "ci_execution_guard.py")
    environment["PYTHONPATH"] = fork_pythonpath(fork_source, support)
    environment["TPC_LANE_COUNTS"] = str(output / "counts.json")
    environment["TPC_EXEC_GUARD_REPORT"] = str(output / "execution-guard.json")
    if not shutil.which("node", path=environment["PATH"]):
        raise RuntimeError("fork 离线用例需要 Node PATH")
    with (output / "pytest.log").open("w") as log:
        offline = subprocess.run([
            "/usr/bin/sandbox-exec", "-f", str(policy),
            *fork_test_command(fork_python.absolute(), fork, output),
        ], cwd=fork, env=environment, stdout=log, stderr=subprocess.STDOUT,
           check=False, timeout=600)
    result = {"returncode": offline.returncode,
              "files": len(FORK_OFFLINE_TESTS),
              "expected_tests": FORK_EXPECTED_TESTS}
    counts_path = output / "counts.json"
    if counts_path.exists():
        counts = json.loads(counts_path.read_text())
        result["counts"] = counts
        try:
            validate_counts(counts)
            if counts["selected"] != FORK_EXPECTED_TESTS:
                raise RuntimeError("fork selection count changed")
        except RuntimeError:
            result["returncode"] = 1
    else:
        result["status"] = "missing_result"
    if (output / "pytest.xml").exists():
        redact_junit(output / "pytest.xml")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
