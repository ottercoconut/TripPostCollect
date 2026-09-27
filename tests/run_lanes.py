"""macOS 测试入口；仅使用专用临时源码副本，报告写到 -o。"""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import platform
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import pytest


EXPRESSIONS = {
    "component": "not macos_process and not local_socket and not installation",
    "socket": "local_socket",
    "installation": "installation",
    "os": "macos_process",
}


def runtime_path():
    node = shutil.which("node")
    prefixes = [str(Path(node).parent)] if node else []
    return os.pathsep.join([*prefixes, "/usr/local/bin", "/opt/homebrew/bin",
                            "/usr/bin", "/bin", "/usr/sbin", "/sbin"])


def pytest_configure(config):
    config._lane_counts = dict(collected=0, selected=0, deselected=0, passed=0,
                               failed=0, errors=0, skipped=0, xfailed=0, xpassed=0,
                               collection_errors=0, failure_diagnostics=[])


def failure_diagnostic(item, call):
    """只导出源码位置和白名单错误类型，不读取异常文本或 locals。"""
    allowed = {"AssertionError", "TimeoutError", "RuntimeError", "ValueError",
               "TypeError", "KeyError", "OSError", "PermissionError"}
    kind = call.excinfo.type.__name__
    diagnostic = {"node": item.nodeid.split("[", 1)[0],
                  "error_type": kind if kind in allowed else "OtherError",
                  "phase": call.when}
    root = Path(item.config.rootpath).resolve()
    for entry in reversed(list(call.excinfo.traceback)):
        path = Path(str(entry.path)).resolve()
        if path.is_relative_to(root):
            diagnostic.update(file=str(path.relative_to(root)), line=entry.lineno + 1)
            break
    return diagnostic


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if outcome.get_result().failed and call.excinfo is not None:
        item.config._lane_counts["failure_diagnostics"].append(failure_diagnostic(item, call))


def pytest_deselected(items):
    if items:
        items[0].config._lane_counts["deselected"] += len(items)


def pytest_collection_finish(session):
    counts = session.config._lane_counts
    counts["selected"] = len(session.items)
    counts["collected"] = counts["selected"] + counts["deselected"]


def pytest_runtest_logreport(report):
    counts = _active_counts
    if hasattr(report, "wasxfail"):
        counts["xfailed" if report.skipped else "xpassed"] += 1
    elif report.failed:
        counts["failed" if report.when == "call" else "errors"] += 1
    elif report.skipped:
        counts["skipped"] += 1
    elif report.when == "call" and report.passed:
        counts["passed"] += 1


def pytest_collectreport(report):
    if report.failed:
        _active_counts["collection_errors"] += 1


_active_counts = {}


def pytest_sessionstart(session):
    global _active_counts
    _active_counts = session.config._lane_counts


def pytest_sessionfinish(session, exitstatus):
    counts = session.config._lane_counts
    counts["exitstatus"] = int(exitstatus)
    Path(os.environ["TPC_LANE_COUNTS"]).write_text(json.dumps(counts, indent=2))


def validate_counts(counts: dict) -> None:
    require(counts["selected"] > 0 and counts["passed"] == counts["selected"],
            "必跑 lane 必须全部实际执行且通过")
    require(all(counts.get(key, 0) == 0 for key in
                ("failed", "errors", "skipped", "xfailed", "xpassed", "exitstatus",
                 "collection_errors")), "必跑 lane 不接受失败、skip、xfail 或 xpass")


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def sandbox_policy(source: Path, output: Path, lane: str) -> str:
    # Allow the loader, Python distributions and macOS runtime services. Do not
    # reconstruct their changing dependency graph with a deny-default policy.
    source, output = source.resolve(), output.resolve()
    home = Path.home().resolve()
    checkout = Path(__file__).resolve().parents[1]
    forbidden = {
        home / name for name in (
            ".ssh", ".aws", ".azure", ".config", ".codex", ".agents",
            ".netrc", ".git-credentials", "Library/Keychains",
            "Library/Application Support", "Library/Cookies", "Library/Safari",
            "Documents", "Desktop", "Downloads",
        )
    }
    if checkout != source:
        forbidden.update(checkout / name for name in
                         ("data", "outputs", "tools/MediaCrawler/browser_data", ".git", ".env"))
    forbidden.update(source / name for name in
                     ("tools/MediaCrawler/browser_data", ".git", ".env"))
    rules = ["(version 1)", "(allow default)", "(deny network*)"]
    rules += [f"(deny file-read* file-write* (subpath {json.dumps(str(p.resolve()))}))"
              for p in sorted(forbidden)]
    if checkout != source:
        rules += [f"(deny file-write* (subpath {json.dumps(str(checkout))}))"]
    rules += [
        '(deny file-write* (require-all',
        f'  (require-not (subpath {json.dumps(str(source))}))',
        f'  (require-not (subpath {json.dumps(str(output))}))',
        '  (require-not (literal "/dev/null"))))',
        '(deny process-exec (literal "/usr/bin/open") (literal "/usr/bin/osascript")',
        '  (regex #"(?i).*(chrome|chromium|safari|firefox|webkit|msedge|MiniBrowser).*"))',
    ]
    if lane == "socket":
        rules += [
            f'(allow network-bind (local unix-socket (subpath {json.dumps(str(output / "t"))})))'
        ]
    return "\n".join(rules) + "\n"


def denied_network_probe(output: Path, lane: str) -> None:
    # A refused connection is not proof of policy: require EPERM/EACCES.
    import errno

    observations = []
    if lane == "os":
        # PF is verified by the CI supervisor against live listeners and rule
        # counters. Its drop/time-out semantics are not Seatbelt EPERM semantics.
        evidence = json.loads((output / "os-policy.json").read_text())
        require(evidence.get("mechanism") == "pf+python-execution-guard"
                and evidence.get("verified_live_listeners") is True,
                "缺少 PF 实测证据")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/ci"))
        from native_macos import verify_active
        verify_active(evidence)
        return
    for address in (("127.0.0.1", 9), ("192.0.2.1", 443)):
        # A failure to construct a socket is a missing capability, not a passed
        # connection-denial probe.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.settimeout(1)
                sock.connect(address)
            except OSError as exc:
                require(exc.errno in {errno.EPERM, errno.EACCES},
                        f"Seatbelt 未证明拒绝 {address}: {exc}")
                observations.append({"operation": "connect", "address": address,
                                     "errno": exc.errno})
            else:
                raise RuntimeError(f"网络策略允许了 {address}")
    if lane == "socket":
        # The parent exists and is writable; require a network authorization
        # failure, not ENOENT or a filesystem restriction. Only this lane needs
        # AF_UNIX capability, never the native PF lane.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(str(output / "outside.sock"))
            except OSError as exc:
                require(exc.errno in {errno.EPERM, errno.EACCES}, str(exc))
                observations.append({"operation": "unix_bind_outside", "errno": exc.errno})
            else:
                raise RuntimeError("临时白名单外 AF_UNIX bind 未被拒绝")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            path = output / "t" / "probe.sock"
            sock.bind(str(path))
        path.unlink()
        observations.append({"operation": "unix_bind_inside", "allowed": True})
    (output / "network-probes.json").write_text(json.dumps(
        {"mechanism": "seatbelt", "observations": observations}, indent=2))


def os_preflight(output: Path) -> None:
    from trippostcollect.xhs.leases import SystemProcessInspector

    inspector = SystemProcessInspector()
    owner = inspector.current_identity()
    require(owner.pid == os.getpid() and owner.pgid == os.getpgid(0),
            "当前进程身份预检失败")
    profile = output / "probe-profile"
    profile.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()",
         f"--user-data-dir={profile}"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        require(bool(select.select([child.stdout], [], [], 5)[0]), "测试 child 启动超时")
        require(child.stdout.readline().strip() == "ready", "测试 child 未就绪")
        identity = inspector.identity(child.pid)
        require(identity is not None and identity.pgid == child.pid
                and bool(identity.process_start_token), "child PID/PGID/启动身份不可用")
        require(any(p.identity == identity for p in inspector.group_members(child.pid)),
                "真实进程组枚举不可用")
        require(any(p.identity == identity for p in inspector.profile_processes(profile)),
                "真实 profile 精确匹配不可用")
        require(all(p.identity != identity for p in
                    inspector.profile_processes(output / "other-profile")),
                "profile 匹配误纳入其他目录")
    finally:
        # EOF stops only the Popen child, without signaling an unverified PID.
        child.communicate(timeout=10)
    require(inspector.process_presence(child.pid) is False, "预检 child 未退出")


def execute(source: Path, output: Path, lane: str) -> int:
    sys.path[:0] = [str(source / "src"), str(source / "tests")]
    from support.execution_guard import install, canary
    install()
    canary()
    denied_network_probe(output, lane)
    if lane == "os":
        os_preflight(output)
    if lane == "installation":
        for name in ("scrapling.fetchers", "patchright", "playwright", "PIL", "pydantic"):
            importlib.import_module(name)
        for script in ("crawl_runner.py", "xhs_runner.py", "mediacrawler_crawl.py"):
            subprocess.run([sys.executable, str(source / "scripts" / script), "--help"],
                           cwd=source, check=True, timeout=60)
    report = output / "pytest.xml"
    command = [
        sys.executable, "-m", "pytest", "tests", "-m", EXPRESSIONS[lane],
        "--strict-markers", "-p", "pytest_asyncio.plugin",
        "-p", "run_lanes", "-p", "support.execution_guard", "-o", "xfail_strict=true",
        "--basetemp", str(output / "t" / "pytest"),
        "--junitxml", str(report), "-o", f"cache_dir={output / 'cache'}", "-q",
    ]
    result = subprocess.run(command, cwd=source, check=False)
    if result.returncode:
        return result.returncode
    validate_counts(json.loads((output / "counts.json").read_text()))
    suites = list(ET.parse(report).getroot().iter("testsuite"))
    require(sum(int(s.attrib["tests"]) for s in suites) > 0, "必跑 lane 空收集")
    require(all(int(s.attrib.get(key, 0)) == 0 for s in suites
                for key in ("skipped", "failures", "errors")), "必跑 lane 不接受 skip/xfail")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="专用临时源码副本")
    parser.add_argument("-o", "--output", type=Path, required=True, help="新的仓库外报告目录")
    parser.add_argument("--lane", choices=EXPRESSIONS, default="component")
    parser.add_argument("--execute", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if not args.execute:
        require(source != Path(__file__).resolve().parents[1], "runner 必须来自外层真实仓库")
    require(sys.platform == "darwin", "需要 macOS；Linux 不能替代验收")
    temporary_roots = {Path("/private/tmp"), Path(tempfile.gettempdir()).resolve()}
    require(any(inside(source, p) for p in temporary_roots), "源码必须位于专用临时目录")
    require(any(inside(output, p) for p in temporary_roots), "输出必须位于专用临时目录")
    require(not inside(output, source) and not inside(source, output), "源码与输出必须分离")
    require((source / "pyproject.toml").is_file(), "源码副本不完整")
    if args.execute:
        if args.lane == "os":
            require(os.environ.get("GITHUB_ACTIONS") == "true"
                    and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
                    and (output / "os-policy.json").is_file(),
                    "原生 OS 执行缺少独立托管 VM 与控制层探针记录")
        return execute(source, output, args.lane)
    output.mkdir(parents=True, exist_ok=False)
    (output / "t").mkdir()
    # The caller supplies a source-only copy, never a copy of production data.
    for name in ("data", "outputs"):
        require(not (source / name).exists()
                or not any((source / name).iterdir()), f"源码副本含已有 {name}，拒绝执行")
    environment = {
        "PATH": runtime_path(),
        "HOME": str(output / "home"), "TMPDIR": str(output / "t"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": os.pathsep.join(str(source / p) for p in ("src", "tests", "scripts")),
        "LC_ALL": "en_US.UTF-8",
        "TPC_LANE_COUNTS": str(output / "counts.json"),
        "TPC_EXEC_GUARD_REPORT": str(output / "execution-guard.json"),
    }
    (output / "home").mkdir()
    command = [
        sys.executable, str(source / "tests" / "run_lanes.py"),
        "--source", str(source), "-o", str(output), "--lane", args.lane, "--execute",
    ]
    if args.lane == "os":
        require(os.environ.get("GITHUB_ACTIONS") == "true"
                and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted",
                "OS lane 仅允许一次性 GitHub 托管 macOS VM；禁止当前私人账号宿主运行")
        environment.update({key: os.environ[key] for key in
                            ("GITHUB_ACTIONS", "RUNNER_ENVIRONMENT", "GITHUB_RUN_ID", "ImageOS")})
    else:
        policy = output / "sandbox.sb"
        policy.write_text(sandbox_policy(source, output, args.lane))
        command = ["/usr/bin/sandbox-exec", "-f", str(policy), *command]
    started = time.time()
    returncode = 2
    with (output / "pytest.log").open("w") as log:
        try:
            if args.lane == "os":
                sys.path.insert(0, str(source / "scripts" / "ci"))
                from native_macos import run_isolated
                returncode = run_isolated(command, source, output, environment, log)
            else:
                returncode = subprocess.run(command, cwd=source, env=environment,
                                            stdout=log, stderr=subprocess.STDOUT,
                                            check=False).returncode
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            log.write(f"隔离/执行失败: {exc}\n")
    summary = {
        "lane": args.lane, "status": "passed" if returncode == 0 else "failed",
        "returncode": returncode, "source": str(source), "output": str(output),
        "python": sys.version, "macos": platform.mac_ver()[0],
        "seconds": round(time.time() - started, 3),
        "other_lanes": {key: "未运行" for key in EXPRESSIONS if key != args.lane},
        "counts": json.loads((output / "counts.json").read_text())
        if (output / "counts.json").exists() else None,
    }
    (output / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False))
    return returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"测试入口预检失败，不可验收: {exc}", file=sys.stderr)
        raise SystemExit(2)
