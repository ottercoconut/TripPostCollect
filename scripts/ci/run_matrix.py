"""Run every lane, keeping failures separate and uploading no raw test data."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET


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
            os.chflags(path, path.stat().st_flags | stat.UF_IMMUTABLE)
            if not path.stat().st_flags & stat.UF_IMMUTABLE:
                raise RuntimeError("冻结副本 uchg 未恢复")


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
    environment["PYTHONPATH"] = os.pathsep.join((str(fork), str(fork_source / "tests")))
    if not shutil.which("node", path=environment["PATH"]):
        raise RuntimeError("完整 fork 导入需要 Node PATH")
    with (output / "imports.log").open("w") as log:
        result = subprocess.run([
            "/usr/bin/sandbox-exec", "-f", str(policy), str(args.fork_python.absolute()),
            "-c", "from support.execution_guard import install, canary; install(); canary(); import importlib; "
            f"[importlib.import_module(name) for name in {modules!r}]",
        ], cwd=fork, env=environment, stdout=log, stderr=subprocess.STDOUT, check=False,
           timeout=120)
    results["fork"] = {"returncode": result.returncode, "static_modules": modules,
                       "count": len(modules) if result.returncode == 0 else 0}
    (reports / "matrix.json").write_text(json.dumps(results, indent=2))
    return int(any(value["returncode"] != 0 or value.get("status") == "missing_result"
                   for value in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
