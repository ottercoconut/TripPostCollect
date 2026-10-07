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


FORK_OFFLINE_TESTS = tuple(f"tests/test_{name}.py" for name in (
    "image_client_http_classification",
    "image_download_retry", "image_staging_errors", "trippostcollect_adaptive",
))
FORK_EXPECTED_TESTS = 34


def fork_pythonpath(source, support):
    # 正式 export entrypoint 注入 src；脚本入口自身也提供 scripts。
    # 不加入根 tests，避免覆盖 fork 的 support/conftest。
    return os.pathsep.join(str(path) for path in (
        source / "tools/MediaCrawler", source / "src", source / "scripts", support,
    ))


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
        shutil.copytree(pristine / name, destination / name, ignore=ignore,
                        copy_function=shutil.copyfile)
    for name in ("pyproject.toml", "uv.lock", "AGENTS.md"):
        shutil.copyfile(pristine / name, destination / name)
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
    parser.add_argument("--fork-python", required=True, type=Path)
    args = parser.parse_args()
    source, reports = args.source.resolve(), args.reports.resolve()
    runner = args.runner.resolve()
    if runner != Path(__file__).resolve().parents[2] / "tests/run_lanes.py":
        raise RuntimeError("必须使用本控制脚本所在外层仓库的 runner")
    sys.path.insert(0, str(runner.parent))
    from run_lanes import sandbox_policy, runtime_path, validate_counts
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
    # Worker imports use the fork's own environment; no root environment reuse,
    # platform requests, browser installs, login or collection commands.
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
    modules = ["media_platform.bilibili", "media_platform.weibo", "media_platform.douyin",
               "media_platform.zhihu", "media_platform.xhs"]
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
        raise RuntimeError("完整 fork 导入需要 Node PATH")
    with (output / "imports.log").open("w") as log:
        result = subprocess.run([
            "/usr/bin/sandbox-exec", "-f", str(policy), str(args.fork_python.absolute()),
            "-c", "from ci_execution_guard import install, canary; install(); canary(); "
            "from ci_lane_report import denied_network_probe; from pathlib import Path; "
            f"denied_network_probe(Path({str(output)!r}), 'installation'); import importlib; "
            f"[importlib.import_module(name) for name in {modules!r}]",
        ], cwd=fork, env=environment, stdout=log, stderr=subprocess.STDOUT, check=False,
           timeout=120)
    results["fork"] = {"returncode": result.returncode, "static_modules": modules,
                       "count": len(modules) if result.returncode == 0 else 0}
    with (output / "pytest.log").open("w") as log:
        offline = subprocess.run([
            "/usr/bin/sandbox-exec", "-f", str(policy),
            *fork_test_command(args.fork_python.absolute(), fork, output),
        ], cwd=fork, env=environment, stdout=log, stderr=subprocess.STDOUT,
           check=False, timeout=600)
    results["fork_offline"] = {"returncode": offline.returncode,
                               "files": len(FORK_OFFLINE_TESTS),
                               "expected_tests": FORK_EXPECTED_TESTS}
    counts_path = output / "counts.json"
    if counts_path.exists():
        counts = json.loads(counts_path.read_text())
        results["fork_offline"]["counts"] = counts
        try:
            validate_counts(counts)
            if counts["selected"] != FORK_EXPECTED_TESTS:
                raise RuntimeError("fork selection count changed")
        except RuntimeError:
            results["fork_offline"]["returncode"] = 1
    else:
        results["fork_offline"]["status"] = "missing_result"
    if (output / "pytest.xml").exists():
        redact_junit(output / "pytest.xml")
    (reports / "matrix.json").write_text(json.dumps(results, indent=2))
    return int(any(value["returncode"] != 0 or value.get("status") == "missing_result"
                   for value in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
