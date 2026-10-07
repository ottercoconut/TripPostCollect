"""入口契约单测：不修改防火墙，不启动浏览器，不调用真实进程枚举。"""

import errno
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

import run_lanes
from support.signal_driver import isolated_signal_test

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/ci"))


def test_fork_worker_paths_and_pytest_boundary(tmp_path):
    from importlib.machinery import PathFinder
    import run_matrix

    source = tmp_path / "source"
    support = tmp_path / "reports/ci_support"
    paths = run_matrix.fork_pythonpath(source, support).split(os.pathsep)
    assert paths == [str(source / "tools/MediaCrawler"), str(source / "src"),
                     str(source / "scripts"), str(support)]
    fork = source / "tools/MediaCrawler"
    command = run_matrix.fork_test_command(Path("worker-python"), fork, tmp_path)
    assert command[:3] == ["worker-python", "-m", "pytest"]
    assert command[command.index("-c") + 1] == str(fork / "pyproject.toml")
    assert command[command.index("--confcutdir") + 1] == str(fork)
    assert command[command.index("--rootdir") + 1] == str(fork)
    assert len(set(run_matrix.FORK_OFFLINE_TESTS)) == 19
    assert run_matrix.FORK_EXPECTED_TESTS == 284
    assert "support.execution_guard" not in command
    assert "ci_execution_guard" in command
    root = Path(__file__).resolve().parents[1]
    # 不依赖当前进程已安装的根包：新增路径必须能定位到真实 src 包。
    spec = PathFinder.find_spec("trippostcollect", run_matrix.fork_pythonpath(root, support).split(os.pathsep))
    assert Path(spec.origin) == root / "src/trippostcollect/__init__.py"
    assert all((root / "tools/MediaCrawler" / name).is_file()
               for name in run_matrix.FORK_OFFLINE_TESTS)


@pytest.mark.parametrize("kind, expected", [(AssertionError, "AssertionError"),
                                          (type("SecretHeader", (Exception,), {}), "OtherError")])
def test_failure_diagnostic_excludes_exception_text_and_parameter_values(tmp_path, kind, expected):
    item = SimpleNamespace(nodeid="tests/test_safe.py::test_safe[secret-header]",
                           config=SimpleNamespace(rootpath=tmp_path))
    call = SimpleNamespace(when="call", excinfo=SimpleNamespace(
        type=kind, value=kind("secret-body"), traceback=[
            SimpleNamespace(path=tmp_path / "tests/test_safe.py", lineno=41)]))
    result = run_lanes.failure_diagnostic(item, call)
    assert result == {"node": "tests/test_safe.py::test_safe", "phase": "call",
                      "error_type": expected, "file": "tests/test_safe.py", "line": 42}
    assert "secret" not in json.dumps(result).lower()


def test_failure_diagnostic_hook_records_only_failed_reports(tmp_path):
    item = SimpleNamespace(nodeid="tests/test_safe.py::test_safe",
                           config=SimpleNamespace(rootpath=tmp_path,
                                                  _lane_counts={"failure_diagnostics": []}))
    call = SimpleNamespace(when="call", excinfo=SimpleNamespace(
        type=AssertionError, traceback=[]))
    for failed in (False, True):
        hook = run_lanes.pytest_runtest_makereport(item, call)
        next(hook)
        with pytest.raises(StopIteration):
            hook.send(SimpleNamespace(get_result=lambda: SimpleNamespace(failed=failed)))
    assert item.config._lane_counts["failure_diagnostics"] == [
        {"node": item.nodeid, "phase": "call", "error_type": "AssertionError"}]


def test_policy_uses_allow_default_and_resolved_temporary_paths(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    policy = run_lanes.sandbox_policy(alias, tmp_path / "output", "component")
    assert "(allow default)" in policy
    assert "(deny default)" not in policy
    assert "(deny network*)" in policy
    assert str(alias) not in policy
    assert str(source.resolve()) in policy
    assert ".ssh" in policy and "Keychains" in policy
    assert "process-exec" in policy and "/usr/bin/open" in policy
    assert "network-bind" not in policy
    assert "network-bind" in run_lanes.sandbox_policy(source, tmp_path, "socket")


def test_socket_construction_failure_is_not_a_denial_pass(monkeypatch, tmp_path):
    def unavailable(*args):
        raise PermissionError(errno.EPERM, "socket creation denied")

    monkeypatch.setattr(socket, "socket", unavailable)
    with pytest.raises(PermissionError, match="socket creation"):
        run_lanes.denied_network_probe(tmp_path, "component")
    assert not (tmp_path / "network-probes.json").exists()


@pytest.mark.parametrize("error", [errno.ECONNREFUSED, errno.ETIMEDOUT, errno.ENETUNREACH])
def test_seatbelt_does_not_accept_other_layers_errors(monkeypatch, tmp_path, error):
    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def connect(self, address):
            raise OSError(error, "not seatbelt")

    monkeypatch.setattr(socket, "socket", lambda *args: Probe())
    with pytest.raises(RuntimeError, match="Seatbelt"):
        run_lanes.denied_network_probe(tmp_path, "component")


def test_native_probe_does_not_require_unix_socket_denial(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/ci"))
    import native_macos

    observed = []
    monkeypatch.setattr(native_macos, "verify_active", observed.append)
    (tmp_path / "os-policy.json").write_text(json.dumps({
        "mechanism": "pf+python-execution-guard", "verified_live_listeners": True,
    }))
    monkeypatch.setattr(socket, "socket", lambda *args: pytest.fail("not Seatbelt"))
    run_lanes.denied_network_probe(tmp_path, "os")
    assert len(observed) == 1


@pytest.mark.parametrize("key", ["failed", "errors", "skipped", "xfailed", "xpassed",
                                 "collection_errors", "exitstatus"])
def test_pipeline_rejects_nonpassing_outcomes(key):
    with pytest.raises(RuntimeError):
        run_lanes.validate_counts({"selected": 1, "passed": 1, key: 1})


@pytest.mark.parametrize("counts", [{"selected": 0, "passed": 0},
                                  {"selected": 2, "passed": 1}])
def test_pipeline_rejects_empty_or_incomplete_selection(counts):
    with pytest.raises(RuntimeError):
        run_lanes.validate_counts(counts)


def test_deselected_are_not_counted_as_passed():
    run_lanes.validate_counts({"selected": 2, "passed": 2, "deselected": 900})


def test_native_admin_guard_rejects_private_host(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/ci"))
    import native_macos

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(native_macos.subprocess, "run",
                        lambda *args, **kwargs: pytest.fail("must not execute sudo"))
    with pytest.raises(RuntimeError, match="GitHub-hosted"):
        native_macos.admin("/sbin/pfctl", "-e")


@pytest.mark.parametrize("fail_execution", [False, True, "cleanup"])
@pytest.mark.parametrize("temporary_dispatcher", [False, True])
def test_native_controller_restores_anchor_and_token(monkeypatch, tmp_path,
                                                     fail_execution, temporary_dispatcher):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/ci"))
    import native_macos

    events = []
    monkeypatch.setattr(native_macos, "hosted_only", lambda: None)
    monkeypatch.setattr(native_macos.signal, "signal", lambda *args: None)
    monkeypatch.setenv("GITHUB_RUN_ID", "test")

    def fake_pf(*args):
        events.append(args)
        if args == ("-sr",):
            if temporary_dispatcher:
                return ""
            return 'anchor "com.apple/*" all'
        if "-vvsr" in args:
            return "Packets: 4"
        return ""

    def fake_run(command, **kwargs):
        if "-E" in command:
            return SimpleNamespace(stdout="", stderr="Token : 123", returncode=0)
        if fail_execution:
            raise RuntimeError("child failure")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(native_macos, "pf", fake_pf)
    monkeypatch.setattr(native_macos, "listeners", lambda stack: [])
    monkeypatch.setattr(native_macos, "verify_drop", lambda servers: [])
    def connected(servers):
        events.append(("connected",))
        return []

    def prepare(stack, output, anchor):
        if temporary_dispatcher:
            stack.callback(fake_pf, "-f", "original-empty.pf")
        return temporary_dispatcher

    monkeypatch.setattr(native_macos, "prepare_anchor", prepare)
    monkeypatch.setattr(native_macos, "verify_connected", connected)
    monkeypatch.setattr(native_macos.subprocess, "run", fake_run)
    def fake_test(command, *args, cleanup):
        if fail_execution == "cleanup":
            cleanup.update(confirmed=False, status="cleanup_failed")
            events.append(("cleanup_failed",))
            raise RuntimeError("group remains")
        cleanup.update(confirmed=True, status="group_exited")
        events.append(("group_exited",))
        return fake_run(command).returncode

    monkeypatch.setattr(native_macos, "run_test_command", fake_test)
    (tmp_path / "execution-guard.json").write_text(json.dumps({
        "mechanism": "python-execution-guard", "observations": [{}] * 6,
    }))
    with (tmp_path / "log").open("w") as log:
        if fail_execution:
            message = "PF retained" if fail_execution == "cleanup" else "child failure"
            with pytest.raises(RuntimeError, match=message):
                native_macos.run_isolated(["test"], tmp_path, tmp_path, {}, log)
        else:
            assert native_macos.run_isolated(["test"], tmp_path, tmp_path, {}, log) == 0
    evidence = json.loads((tmp_path / "os-policy.json").read_text())
    if fail_execution == "cleanup":
        assert ("-a", "com.apple/trippostcollect-tests", "-F", "rules") not in events
        assert ("-X", "123") not in events
        assert ("-f", "original-empty.pf") not in events
        assert ("connected",) not in events
        assert evidence["restored"] is False
        assert evidence["test_cleanup"]["confirmed"] is False
        assert evidence["restoration_status"] == "blocked_until_vm_destruction"
        return
    assert events.index(("group_exited",)) < events.index(
        ("-a", "com.apple/trippostcollect-tests", "-F", "rules"))
    assert ("-X", "123") in events
    assert (("-f", "original-empty.pf") in events) == temporary_dispatcher
    assert json.loads((tmp_path / "os-policy.json").read_text())["restored"] is True


@pytest.mark.parametrize("scenario", ["empty", "term", "kill", "stuck", "observe_error",
                                     "reused", "wrong_group", "timeout", "cancel"])
def test_native_owned_group_cleanup(monkeypatch, scenario):
    import native_macos

    events = []
    now = [0.0]
    state = {"signal": None, "observations": 0}
    cleanup = {}
    owner = (410, "owned-start")
    unrelated = {999: (999, "other-start")}

    def wait(timeout):
        assert timeout == 1800
        events.append("wait")
        if scenario == "timeout":
            raise native_macos.subprocess.TimeoutExpired("fake", timeout)
        if scenario == "cancel":
            raise KeyboardInterrupt()
        return 0

    def popen(*args, **kwargs):
        assert kwargs["start_new_session"] is True
        return SimpleNamespace(pid=410, wait=wait, poll=lambda: 0)

    def observe(timeout):
        assert 0 < timeout <= 5
        state["observations"] += 1
        if state["observations"] == 1:
            return {**unrelated, 410: owner}
        if scenario == "observe_error":
            raise OSError("observation unavailable")
        if scenario == "reused":
            return {**unrelated, 410: (410, "different-start")}
        if scenario == "wrong_group":
            return {**unrelated, 410: (999, "owned-start")}
        alive = scenario != "empty"
        if scenario in ("term", "timeout", "cancel") and state["signal"] is not None:
            alive = False
        if scenario == "kill" and state["signal"] == native_macos.signal.SIGKILL:
            alive = False
        events.append("members" if alive else "empty")
        return {**unrelated, **({411: owner} if alive else {})}

    def killpg(pgid, signum):
        assert pgid == 410
        state["signal"] = signum
        events.append(signum)

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(native_macos.subprocess, "Popen", popen)
    monkeypatch.setattr(native_macos.os, "killpg", killpg)
    monkeypatch.setattr(native_macos, "test_process_snapshot",
                        lambda *args: pytest.fail("real host observation forbidden"))
    errors = {"stuck": RuntimeError, "observe_error": OSError, "reused": RuntimeError,
              "wrong_group": RuntimeError, "timeout": native_macos.subprocess.TimeoutExpired,
              "cancel": KeyboardInterrupt}

    def run():
        return native_macos.run_test_command(["fake"], None, {}, None, cleanup=cleanup,
                                             observe=observe, clock=lambda: now[0], sleep=sleep)

    if scenario in errors:
        with pytest.raises(errors[scenario]):
            run()
    else:
        assert run() == 0
    assert now[0] <= 10
    signals = [event for event in events if isinstance(event, int)]
    if scenario in ("kill", "stuck"):
        assert signals == [native_macos.signal.SIGTERM, native_macos.signal.SIGKILL]
    elif scenario in ("term", "timeout", "cancel"):
        assert signals == [native_macos.signal.SIGTERM]
    else:
        assert signals == []
    assert cleanup["confirmed"] == (scenario in ("empty", "term", "kill", "timeout", "cancel"))
    if cleanup["confirmed"]:
        assert events[-1] == "empty"
    else:
        assert cleanup["status"] == "cleanup_failed"


@pytest.mark.parametrize("table", ["", "410 invalid start", "410 410", "valid"])
def test_native_process_observation_is_strict_and_bounded(monkeypatch, table):
    import native_macos

    def fake_run(command, **kwargs):
        assert command == ["/bin/ps", "-axo", "pid=,pgid=,lstart="]
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 2.5
        return SimpleNamespace(stdout=("410 410 Mon Sep 28 00:00:00 2026\n"
                                       if table == "valid" else table))

    monkeypatch.setattr(native_macos.subprocess, "run", fake_run)
    if table == "valid":
        assert native_macos.test_process_snapshot(2.5) == {
            410: (410, "Mon Sep 28 00:00:00 2026")}
    else:
        with pytest.raises((ValueError, RuntimeError)):
            native_macos.test_process_snapshot(2.5)


def test_native_rejects_receipt_when_kernel_rule_is_missing(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/ci"))
    import native_macos

    monkeypatch.setattr(native_macos, "hosted_only", lambda: None)
    monkeypatch.setattr(native_macos, "pf", lambda *args: "")
    with pytest.raises(RuntimeError, match="PF 规则不存在"):
        native_macos.verify_active({"enforced": True})


def test_ci_upload_junit_removes_source_and_captured_content(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/ci"))
    from run_matrix import redact_junit

    report = tmp_path / "pytest.xml"
    report.write_text('<testsuites><testsuite tests="1" failures="1"><testcase name="case">'
                      '<failure message="private">source excerpt</failure>'
                      '<system-out>database row</system-out>'
                      '</testcase></testsuite></testsuites>')
    redact_junit(report)
    safe = (tmp_path / "junit-summary.xml").read_text()
    assert 'tests="1"' in safe and 'failures="1"' in safe
    assert "private" not in safe and "source excerpt" not in safe and "database row" not in safe
    assert "source excerpt" in report.read_text()


@isolated_signal_test
def test_signal_driver_loads_asyncio_with_autoload_disabled(tmp_path, request):
    # This executes in a real nested pytest, without sending any signal.
    assert request.config.pluginmanager.hasplugin("pytest_asyncio.plugin")
    assert request.config.pluginmanager.hasplugin("support.execution_guard")
    assert Path(__import__("os").environ["TPC_LANE_COUNTS"]).name == "driver-counts.json"
    guard = json.loads(Path(__import__("os").environ["TPC_EXEC_GUARD_REPORT"]).read_text())
    assert len(guard["observations"]) == 6
    assert {item["boundary"] for item in guard["observations"]} == {"subprocess", "exec"}
    assert tmp_path.is_dir()


def test_matrix_uses_outer_runner_and_rejects_inner_runner(tmp_path):
    from run_matrix import lane_command

    source = tmp_path / "lane"
    runner = tmp_path / "checkout/tests/run_lanes.py"
    command = lane_command(runner, source, "component", tmp_path / "report")
    assert command[1] == str(runner)
    assert command[3] == str(source)
    with pytest.raises(RuntimeError, match="外层"):
        lane_command(source / "tests/run_lanes.py", source, "component", tmp_path)


def test_main_launches_execute_from_source(monkeypatch, tmp_path):
    source = (tmp_path / "source").resolve()
    source.mkdir()
    (source / "pyproject.toml").touch()
    output = tmp_path / "report"
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "argv", ["run_lanes.py", "--source", str(source),
                                     "-o", str(output), "--lane", "component"])

    def fake_run(command, **kwargs):
        assert command == [
            "/usr/bin/sandbox-exec", "-f", str(output / "sandbox.sb"),
            sys.executable, str(source / "tests/run_lanes.py"),
            "--source", str(source), "-o", str(output), "--lane", "component", "--execute",
        ]
        assert kwargs["cwd"] == source
        assert kwargs["env"]["PYTHONPATH"].split(os.pathsep) == [
            str(source / name) for name in ("src", "tests", "scripts")
        ]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(run_lanes.subprocess, "run", fake_run)
    assert run_lanes.main() == 0
    monkeypatch.setattr(run_lanes, "__file__", str(source / "tests/run_lanes.py"))
    with pytest.raises(RuntimeError, match="外层"):
        run_lanes.main()

    monkeypatch.setattr(sys, "argv", [*sys.argv, "--execute"])

    def fake_execute(actual_source, actual_output, lane):
        assert (actual_source, actual_output, lane) == (source, output, "component")
        return 0

    monkeypatch.setattr(run_lanes, "execute", fake_execute)
    assert run_lanes.main() == 0


@pytest.mark.parametrize("root_readme", [False, True])
def test_fresh_sources_never_reuse_data_or_environment(monkeypatch, tmp_path, root_readme):
    import run_matrix

    pristine = tmp_path / "pristine"
    pristine.mkdir()
    for directory in ("src", "scripts", "tests", "config", "db", "docs", "tools"):
        (pristine / directory).mkdir()
    for file in ("pyproject.toml", "uv.lock", "AGENTS.md", "docs/README.md"):
        (pristine / file).write_text("source")
    if root_readme:
        (pristine / "README.md").write_text("root readme")
    for directory in ("data", "outputs", ".venv", "tools/MediaCrawler/.venv",
                      "tools/MediaCrawler/browser_data"):
        path = pristine / directory
        path.mkdir(parents=True)
        (path / "private").write_text("do not copy")
    (pristine / "tools/.env").write_text("secret")
    restored = []
    monkeypatch.setattr(run_matrix, "restore_frozen", restored.append)
    first = run_matrix.fresh_source(pristine, tmp_path / "one")
    (first / "data").mkdir()
    (first / "data/result").touch()
    second = run_matrix.fresh_source(pristine, tmp_path / "two")
    for copied in (first, second):
        assert (copied / "README.md").exists() is root_readme
        if root_readme:
            assert (copied / "README.md").read_text() == "root readme"
        for file in ("pyproject.toml", "uv.lock", "AGENTS.md", "docs/README.md"):
            assert (copied / file).read_text() == "source"
    assert not (second / "data").exists()
    assert not (second / ".venv").exists()
    assert not (second / "tools/MediaCrawler/.venv").exists()
    assert not (second / "tools/MediaCrawler/browser_data").exists()
    assert not (second / "tools/.env").exists()
    assert restored == [first, second]
    assert (pristine / "data/private").read_text() == "do not copy"
    with pytest.raises(FileExistsError):
        run_matrix.fresh_source(pristine, second)


@pytest.mark.parametrize("executable,args", [
    ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", []),
    ("/usr/bin/open", ["open", "https://example.org"]),
    ("/usr/bin/osascript", []),
    ("/bin/sh", ["sh", "-c", "open https://example.org"]),
])
def test_guard_rejects_browser_boundaries(executable, args):
    from support.execution_guard import audit

    for event in ("subprocess.Popen", "os.exec", "os.posix_spawn"):
        with pytest.raises(PermissionError):
            audit(event, (executable, args, {}))


@pytest.mark.parametrize("executable, args", [
    ("/usr/bin/xdg-open", ["xdg-open", "https://example.org"]),
    ("/usr/bin/sensible-browser", []),
    ("/usr/bin/x-www-browser", []),
    ("/bin/sh", ["sh", "-c", "xdg-open https://example.org"]),
])
def test_guard_rejects_linux_desktop_openers(executable, args):
    from support.execution_guard import audit, check_command

    for event in ("subprocess.Popen", "os.exec", "os.posix_spawn"):
        with pytest.raises(PermissionError):
            audit(event, (executable, args, {}))
    # 词边界匹配：名称中仅包含 open 的普通命令不受影响。
    check_command("/bin/sh", ["sh", "-c", "openssl version"])


def test_guard_allows_real_signal_and_gate_python_commands():
    from support.execution_guard import check_command

    check_command(sys.executable, [sys.executable, "-c", "import os; os.kill(123, 15)"])
    check_command("/bin/ps", ["ps", "-axo", "pid,pgid,command"])


@pytest.mark.parametrize("occupied", [True, False])
def test_pf_dispatcher_only_bootstraps_empty_vm(monkeypatch, tmp_path, occupied):
    from contextlib import ExitStack
    import native_macos

    events = []
    root = "pass all" if occupied else ""

    def fake_pf(*args):
        nonlocal root
        events.append(args)
        if args == ("-sr",):
            return root
        if args[0] == "-f":
            root = Path(args[1]).read_text()
        return ""

    monkeypatch.setattr(native_macos, "pf", fake_pf)
    with ExitStack() as stack:
        if occupied:
            with pytest.raises(RuntimeError, match="未知策略"):
                native_macos.prepare_anchor(stack, tmp_path, "test")
            assert not any(args[0] == "-f" for args in events)
        else:
            assert native_macos.prepare_anchor(stack, tmp_path, "test")
            assert 'anchor "test"' in root
    assert root == ("pass all" if occupied else "")


def test_frozen_copy_hash_is_checked_before_flags(monkeypatch, tmp_path):
    import run_matrix

    (tmp_path / "config").mkdir()
    (tmp_path / "frozen.md").write_text("changed")
    (tmp_path / "config/frozen_files.json").write_text(json.dumps({
        "schema_version": 1, "files": [{"path": "frozen.md", "sha256": "wrong",
                                         "require_immutable_flag": True}],
    }))
    monkeypatch.setattr(run_matrix.frozen_flags, "set_immutable",
                        lambda *args: pytest.fail("哈希错误不得设置标志"))
    with pytest.raises(RuntimeError, match="哈希"):
        run_matrix.restore_frozen(tmp_path)


def test_frozen_copy_restores_only_registered_copy(monkeypatch, tmp_path):
    import hashlib
    import run_matrix

    (tmp_path / "config").mkdir()
    target = tmp_path / "frozen.md"
    target.write_text("content")
    (tmp_path / "config/frozen_files.json").write_text(json.dumps({
        "schema_version": 1, "files": [{"path": "frozen.md",
            "sha256": hashlib.sha256(b"content").hexdigest(), "require_immutable_flag": True}],
    }))
    immutable = set()

    def fake_set(path):
        assert path == target
        immutable.add(path)

    monkeypatch.setattr(run_matrix.frozen_flags, "set_immutable", fake_set)
    monkeypatch.setattr(run_matrix.frozen_flags, "is_immutable", lambda path: path in immutable)
    run_matrix.restore_frozen(tmp_path)
    assert immutable == {target}


def test_signal_driver_preserves_parent_counts(monkeypatch, tmp_path):
    from support import signal_driver

    parent_counts = tmp_path / "parent-counts.json"
    parent_counts.write_text("parent")
    monkeypatch.setenv("TPC_LANE_COUNTS", str(parent_counts))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_case.py::test_case (call)")
    monkeypatch.delenv("TPC_SIGNAL_DRIVER", raising=False)

    def fake_run(command, **kwargs):
        assert "pytest_asyncio.plugin" in command
        assert "support.execution_guard" in command
        assert kwargs["env"]["TPC_LANE_COUNTS"] != str(parent_counts)
        Path(kwargs["env"]["TPC_LANE_COUNTS"]).write_text("driver")
        Path(command[command.index("--junitxml") + 1]).write_text(
            '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"/></testsuites>')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(signal_driver.subprocess, "run", fake_run)
    isolated_signal_test(lambda **kwargs: pytest.fail("must run in child"))(tmp_path=tmp_path)
    assert parent_counts.read_text() == "parent"


def test_pf_without_real_packet_counts_fails(monkeypatch):
    import native_macos

    monkeypatch.setattr(native_macos, "pf", lambda *args: "Packets: 0")
    with pytest.raises(RuntimeError, match="实际阻断计数"):
        native_macos.checked_counters("test")
