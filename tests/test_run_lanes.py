"""入口契约单测：不修改防火墙，不启动浏览器，不调用真实进程枚举。"""

import errno
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

import run_lanes
from support.signal_driver import isolated_signal_test

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/ci"))


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
    uid = native_macos.TEST_UID
    loaded = {"on": False}
    monkeypatch.setattr(native_macos, "hosted_only", lambda: None)
    monkeypatch.setattr(native_macos.signal, "signal", lambda *args: None)
    monkeypatch.setenv("GITHUB_RUN_ID", "test")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "pw-browsers"))
    monkeypatch.setenv("TPC_BROWSER_REPLAY", "1")

    def fake_pf(*args):
        events.append(args)
        if args == ("-sr",):
            if temporary_dispatcher:
                return ""
            return 'anchor "com.apple/*" all'
        if args[0] == "-n":
            return ""
        if args[:2] == ("-s", "info"):
            return "Status: Enabled\n"
        if len(args) >= 3 and args[0] == "-a" and args[2] == "-f":
            loaded["on"] = True
            return ""
        if len(args) >= 3 and args[0] == "-a" and args[2] == "-F":
            loaded["on"] = False
            return ""
        if len(args) >= 3 and args[0] == "-a" and args[2] == "-sr":
            return native_macos.pf_policy(uid) if loaded["on"] else ""
        if "-vvsr" in args:
            return (
                f'block drop quick proto tcp all user = {uid} label "tpc_test_deny"\n'
                "  [ Packets: 4 Bytes: 240 ]\n"
            )
        return ""

    def fake_run(command, **kwargs):
        if "-E" in command:
            return SimpleNamespace(stdout="", stderr="Token : 123", returncode=0)
        if fail_execution:
            raise RuntimeError("child failure")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(native_macos, "pf", fake_pf)
    monkeypatch.setattr(native_macos, "create_test_user", lambda: {
        "name": native_macos.TEST_USER, "uid": uid, "gid": native_macos.TEST_GID,
        "shell": "/usr/bin/false", "deleted": False,
        "deletion": "not_attempted_vm_disposable",
    })
    monkeypatch.setattr(native_macos, "grant_test_user_access",
                        lambda *args: events.append(("grant", args[0])) or {"work": str(tmp_path)})
    monkeypatch.setattr(native_macos, "listeners", lambda stack: [])
    monkeypatch.setattr(native_macos, "verify_user_open",
                        lambda user, environment, servers: events.append(("baseline", user)) or [])
    monkeypatch.setattr(native_macos, "verify_user_drop",
                        lambda user, environment, servers: [{"user": user, "restricted": "timeout",
                                                            "family": "AF_INET", "address": ["127.0.0.1", 9]}])
    monkeypatch.setattr(native_macos, "verify_runner_open",
                        lambda servers: [{"user": "runner", "uid": 501, "connected": True}])
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
    def fake_test(command, source, environment, log, *, cleanup, user):
        events.append(("launched", user))
        events.append(("environment", {key: environment.get(key) for key in native_macos.BROWSER_REPLAY_KEYS}))
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
    submitted = (tmp_path / "native.pf").read_text()
    native_macos.assert_user_scoped(submitted, uid)
    native_macos.assert_echo_only_icmp(submitted)
    assert 'block drop quick all label "tpc_test_deny"' not in submitted
    assert evidence["test_user"]["name"] == native_macos.TEST_USER
    assert evidence["test_user"]["uid"] == uid
    assert evidence["test_user"]["deleted"] is False
    assert evidence["test_user_negative"][0]["user"] == native_macos.TEST_USER
    assert evidence["runner_positive"][0]["connected"] is True
    assert evidence["pf_policy"]
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
    # 本地 Chromium 回放只把 headless shell 位置与开关交给测试进程。
    assert ("environment", {"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "pw-browsers"),
                            "TPC_BROWSER_REPLAY": "1"}) in events
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
    monkeypatch.setattr(native_macos, "hosted_only", lambda: None)

    def wait(timeout):
        assert timeout == 1800
        events.append("wait")
        if scenario == "timeout":
            raise native_macos.subprocess.TimeoutExpired("fake", timeout)
        if scenario == "cancel":
            raise KeyboardInterrupt()
        return 0

    def popen(argv, **kwargs):
        assert kwargs["start_new_session"] is True
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        assert argv[:6] == ["/usr/bin/sudo", "-n", "-u", "_tpcci", "/usr/bin/env", "-i"]
        assert "HOME=/lane-home" in argv
        assert argv[-1] == "fake"
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

    def fake_admin(*arguments):
        assert arguments[:4] == ("/bin/kill", "-s", arguments[2], "--")
        assert arguments[4] == "-410"
        state["signal"] = {"TERM": native_macos.signal.SIGTERM,
                           "KILL": native_macos.signal.SIGKILL}[arguments[2]]
        events.append(state["signal"])
        return ""

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(native_macos.subprocess, "Popen", popen)
    monkeypatch.setattr(native_macos, "admin", fake_admin)
    monkeypatch.setattr(native_macos.os, "killpg",
                        lambda *args: pytest.fail("cross-user signals must use admin()"))
    monkeypatch.setattr(native_macos, "test_process_snapshot",
                        lambda *args: pytest.fail("real host observation forbidden"))
    errors = {"stuck": RuntimeError, "observe_error": OSError, "reused": RuntimeError,
              "wrong_group": RuntimeError, "timeout": native_macos.subprocess.TimeoutExpired,
              "cancel": KeyboardInterrupt}

    def run():
        return native_macos.run_test_command(
            ["fake"], None, {"HOME": "/lane-home"}, None, user="_tpcci", cleanup=cleanup,
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


def test_child_probe_times_out_without_pfctl(monkeypatch):
    import native_macos

    def forbidden(*args, **kwargs):
        pytest.fail("child probe must not call pfctl or sudo")

    monkeypatch.setattr(native_macos, "pf", forbidden)
    monkeypatch.setattr(native_macos, "admin", forbidden)
    monkeypatch.setattr(native_macos, "hosted_only", forbidden)

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def settimeout(self, value):
            pass

        def connect(self, address):
            self.address = address
            raise TimeoutError()

    monkeypatch.setattr(socket, "socket", lambda *args: Client())
    native_macos.verify_active({
        "test_user": {"uid": os.getuid()},
        "probes": [{"family": "AF_INET", "address": ["127.0.0.1", 9]}],
    })
    with pytest.raises(RuntimeError, match="不在测试用户中"):
        native_macos.verify_active({
            "test_user": {"uid": os.getuid() + 1},
            "probes": [{"family": "AF_INET", "address": ["127.0.0.1", 9]}],
        })

    class Open:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def settimeout(self, value):
            pass

        def connect(self, address):
            return None

    monkeypatch.setattr(socket, "socket", lambda *args: Open())
    with pytest.raises(RuntimeError, match="负例连接成功"):
        native_macos.verify_active({
            "test_user": {"uid": os.getuid()},
            "probes": [{"family": "AF_INET", "address": ["127.0.0.1", 9]}],
        })


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


def test_browser_replay_is_selected_only_by_the_os_lane():
    from _pytest.mark.expression import Expression

    def selected(lane, markers):
        return Expression.compile(run_lanes.EXPRESSIONS[lane]).evaluate(lambda name, **_: name in markers)

    assert [lane for lane in run_lanes.EXPRESSIONS if selected(lane, {"browser_replay"})] == ["os"]
    assert [lane for lane in run_lanes.EXPRESSIONS if selected(lane, {"macos_process"})] == ["os"]
    assert [lane for lane in run_lanes.EXPRESSIONS if selected(lane, set())] == ["component"]


def test_matrix_passes_browser_replay_only_to_the_os_lane(tmp_path):
    from run_matrix import lane_environment

    browsers = str(tmp_path / "pw-browsers")
    environ = {"PATH": "/usr/bin", "PLAYWRIGHT_BROWSERS_PATH": browsers, "TPC_BROWSER_REPLAY": "0"}
    native = lane_environment("os", environ)
    assert native["PLAYWRIGHT_BROWSERS_PATH"] == browsers and native["TPC_BROWSER_REPLAY"] == "1"
    for lane in ("component", "socket", "installation"):
        environment = lane_environment(lane, environ)
        assert environment == {"PATH": "/usr/bin"}
    # 没有安装位置时不打开回放开关：OS lane 的回放用例会跳过，再由 skip=0 校验判失败。
    assert lane_environment("os", {"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}


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
    # T12：根级构建输入 build_support.py、MANIFEST.in 同为必拷源码。
    (pristine / ".github/workflows").mkdir(parents=True)
    for file in ("pyproject.toml", "uv.lock", "AGENTS.md", "docs/README.md", "build_support.py", "MANIFEST.in",
                 ".github/workflows/macos-test-lanes.yml"):
        (pristine / file).write_text("source")
    if root_readme:
        (pristine / "README.md").write_text("root readme")
    for directory in ("data", "outputs", ".venv", "src/.venv", "tools/MediaCrawler/browser_data"):
        path = pristine / directory
        path.mkdir(parents=True)
        (path / "private").write_text("do not copy")
    (pristine / "src/.env").write_text("secret")
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
        for file in ("pyproject.toml", "uv.lock", "AGENTS.md", "docs/README.md", "build_support.py", "MANIFEST.in",
                     ".github/workflows/macos-test-lanes.yml"):
            assert (copied / file).read_text() == "source"
    assert not (second / "data").exists()
    assert not (second / ".venv").exists()
    assert not (second / "src/.venv").exists()
    assert not (second / "src/.env").exists()
    # tools/ 不在白名单：旧登录资料备份等残留目录不进入副本。
    assert not (second / "tools").exists()
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


def test_icmp_packet_counts_do_not_satisfy_the_user_rule(monkeypatch):
    import native_macos

    uid = native_macos.TEST_UID
    sample = (
        f'block drop quick proto tcp all user = {uid} label "tpc_test_deny"\n'
        "  [ Packets: 0 Bytes: 0 ]\n"
        'block drop quick out inet proto icmp all icmp-type echoreq label "tpc_icmp_echo_deny"\n'
        "  [ Packets: 9 Bytes: 500 ]\n"
    )
    monkeypatch.setattr(native_macos, "pf", lambda *args: sample)
    with pytest.raises(RuntimeError, match="实际阻断计数"):
        native_macos.checked_counters("test")
    counted = sample.replace("Packets: 0", "Packets: 2", 1)
    monkeypatch.setattr(native_macos, "pf", lambda *args: counted)
    assert "Packets: 2" in native_macos.checked_counters("test")


def test_pf_policy_scopes_blocks_to_the_test_user():
    import native_macos

    uid = native_macos.TEST_UID
    text = native_macos.pf_policy(uid)
    assert text == (
        f'block drop quick proto {{ tcp udp }} all user {uid} label "tpc_test_deny"\n'
        'block drop quick out inet proto icmp icmp-type echoreq label "tpc_icmp_echo_deny"\n'
        'block drop quick out inet6 proto icmp6 icmp6-type echoreq label "tpc_icmp_echo_deny"\n'
    )
    native_macos.assert_user_scoped(text, uid)
    native_macos.assert_echo_only_icmp(text)
    rewritten = (
        f'block drop quick proto tcp all user = {uid} label "tpc_test_deny"\n'
        f'block drop quick proto udp all user = {uid} label "tpc_test_deny"\n'
        'block drop quick out inet proto icmp all icmp-type 8 label "tpc_icmp_echo_deny"\n'
        'block drop quick out inet6 proto icmp6 all icmp6-type 128 label "tpc_icmp_echo_deny"\n'
    )
    native_macos.assert_user_scoped(rewritten, uid)
    native_macos.assert_echo_only_icmp(rewritten)
    with pytest.raises(RuntimeError, match="整机"):
        native_macos.assert_user_scoped('block drop quick all label "tpc_test_deny"\n', uid)


def test_negative_probe_uses_the_test_user_and_positive_uses_the_runner(monkeypatch):
    import native_macos

    launches = []

    def fake_run_as_user(user, argv, environment, *, timeout):
        launches.append(user)
        assert argv[1:3] == ["-c", native_macos._CONNECT_PROBE]
        assert environment["HOME"] == "/lane-home"
        return SimpleNamespace(returncode=10, stdout="", stderr="")

    monkeypatch.setattr(native_macos, "run_as_user", fake_run_as_user)
    server = SimpleNamespace(getsockname=lambda: ("127.0.0.1", 9))
    dropped = native_macos.verify_user_drop("_tpcci", {"HOME": "/lane-home"}, [(socket.AF_INET, server)])
    assert launches == ["_tpcci"]
    assert dropped[0]["restricted"] == "timeout"
    assert dropped[0]["user"] == "_tpcci"

    def connected_probe(user, argv, environment, *, timeout):
        launches.append(("open", user))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(native_macos, "run_as_user", connected_probe)
    accepted = []
    server.accept = lambda: accepted.append("accepted") or (SimpleNamespace(close=lambda: None), None)
    opened = native_macos.verify_user_open("_tpcci", {"HOME": "/lane-home"}, [(socket.AF_INET, server)])
    assert opened[0]["connected"] is True
    assert accepted == ["accepted"]

    monkeypatch.setattr(native_macos, "run_as_user",
                        lambda *args, **kwargs: pytest.fail("runner probe must not switch user"))

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def settimeout(self, value):
            pass

        def connect(self, address):
            self.address = address

    monkeypatch.setattr(socket, "socket", lambda *args: Client())
    positive = native_macos.verify_runner_open([(socket.AF_INET, server)])
    assert positive[0]["connected"] is True
    assert positive[0]["uid"] == os.getuid()
    assert positive[0]["uid"] != native_macos.TEST_UID


def test_user_probe_command_passes_the_lane_environment_explicitly():
    import native_macos

    command = native_macos.as_user_command(
        "_tpcci", ["/tmp/python", "-m", "pytest"], {"HOME": "/lane-home", "PATH": "/usr/bin"},
    )
    assert command == [
        "/usr/bin/sudo", "-n", "-u", "_tpcci", "/usr/bin/env", "-i",
        "HOME=/lane-home", "PATH=/usr/bin", "/tmp/python", "-m", "pytest",
    ]


def test_test_user_already_exists_is_rejected(monkeypatch):
    import native_macos

    calls = []

    def fake_admin(*args):
        calls.append(args)
        if args == ("/usr/bin/dscl", ".", "-list", "/Users"):
            return "runner\n_tpcci\n"
        raise AssertionError(args)

    monkeypatch.setattr(native_macos, "hosted_only", lambda: None)
    monkeypatch.setattr(native_macos, "admin", fake_admin)
    with pytest.raises(RuntimeError, match="拒绝复用"):
        native_macos.create_test_user()
    assert not any("-create" in args for args in calls)


def test_test_user_uid_collision_is_rejected(monkeypatch):
    import native_macos

    def fake_admin(*args):
        if args == ("/usr/bin/dscl", ".", "-list", "/Users"):
            return "runner\n"
        if args == ("/usr/bin/dscl", ".", "-list", "/Users", "UniqueID"):
            return f"runner {native_macos.TEST_UID}\n"
        raise AssertionError(args)

    monkeypatch.setattr(native_macos, "hosted_only", lambda: None)
    monkeypatch.setattr(native_macos, "admin", fake_admin)
    with pytest.raises(RuntimeError, match="已被 runner 占用"):
        native_macos.create_test_user()


def test_create_test_user_refuses_private_host(monkeypatch):
    import native_macos

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(native_macos, "admin", lambda *args: pytest.fail("must not create users"))
    with pytest.raises(RuntimeError, match="GitHub-hosted"):
        native_macos.create_test_user()


def test_create_test_user_is_unprivileged_and_not_deleted(monkeypatch):
    import native_macos

    calls = []
    user = native_macos.TEST_USER
    uid = str(native_macos.TEST_UID)

    def fake_admin(*args):
        calls.append(args)
        if args == ("/usr/bin/dscl", ".", "-list", "/Users"):
            return "runner\n"
        if args == ("/usr/bin/dscl", ".", "-list", "/Users", "UniqueID"):
            return "runner 501\n"
        if args[:3] == ("/usr/bin/dscl", ".", "-create"):
            return ""
        if args == ("/usr/bin/dscacheutil", "-flushcache"):
            return ""
        if args[:5] == ("/usr/bin/sudo", "-n", "-u", user, "/usr/bin/id") and args[-1] == "-u":
            return uid + "\n"
        if args[:5] == ("/usr/bin/sudo", "-n", "-u", user, "/usr/bin/id") and args[-1] == "-g":
            return "20\n"
        if args[:5] == ("/usr/bin/sudo", "-n", "-u", user, "/usr/bin/id") and args[-1] == "-Gn":
            return "staff\n"
        if args[:6] == ("/usr/bin/sudo", "-n", "-u", user, "/usr/bin/sudo", "-n"):
            raise subprocess.CalledProcessError(1, args, stderr="user is not in the sudoers file")
        raise AssertionError(args)

    monkeypatch.setattr(native_macos, "hosted_only", lambda: None)
    monkeypatch.setattr(native_macos, "admin", fake_admin)
    created = native_macos.create_test_user()
    assert created["name"] == user
    assert created["uid"] == native_macos.TEST_UID
    assert created["gid"] == 20
    assert created["deleted"] is False
    assert created["deletion"] == "not_attempted_vm_disposable"
    assert "admin" not in created["groups"]
    assert not any(arg == "-delete" for args in calls for arg in args)
    node = f"/Users/{user}"
    assert ("/usr/bin/dscl", ".", "-create", node, "UniqueID", uid) in calls
    assert ("/usr/bin/dscl", ".", "-create", node, "PrimaryGroupID", "20") in calls
    assert ("/usr/bin/dscl", ".", "-create", node, "UserShell", "/usr/bin/false") in calls
    assert ("/usr/bin/dscl", ".", "-create", node, "NFSHomeDirectory", "/var/empty") in calls
    assert not any("admin" in args for args in calls if args[:3] == ("/usr/bin/dscl", ".", "-create"))


def test_signal_group_uses_admin_and_ignores_a_missing_process(monkeypatch):
    import native_macos

    calls = []

    def fake_admin(*args):
        calls.append(args)
        if args[-1] == "-7":
            raise subprocess.CalledProcessError(1, args, stderr="kill: 7: No such process")
        raise subprocess.CalledProcessError(1, args, stderr="Operation not permitted")

    monkeypatch.setattr(native_macos, "admin", fake_admin)
    native_macos.signal_group(7, signal.SIGTERM)
    with pytest.raises(subprocess.CalledProcessError):
        native_macos.signal_group(8, signal.SIGKILL)
    assert calls[0] == ("/bin/kill", "-s", "TERM", "--", "-7")
    assert calls[1][2] == "KILL"


def test_access_grants_stay_inside_work(monkeypatch, tmp_path):
    import native_macos

    work = tmp_path / "tpc-work"
    source = work / "lane-os"
    output = work / "reports" / "os"
    venv = work / "root-venv"
    browsers = work / "pw-browsers"
    outside = tmp_path / "outside-python"
    for path in (source / "pkg", output / "home", output / "t", venv / "bin", browsers / "chromium"):
        path.mkdir(parents=True)
    (source / "pkg" / "module.py").write_text("x")
    frozen = source / "frozen.txt"
    frozen.write_text("frozen")
    frozen.chmod(0o644)
    outside.write_text("interpreter")
    (venv / "bin" / "python").symlink_to(outside)
    recorded = []

    def fake_acl(user, path, permissions):
        resolved = Path(path).resolve()
        assert resolved.is_relative_to(work.resolve())
        assert not Path(path).is_symlink()
        recorded.append((str(user), resolved, permissions))

    monkeypatch.setattr(native_macos, "change_acl", fake_acl)
    monkeypatch.setattr(native_macos, "acl_skipped", lambda path: Path(path) == frozen)
    environment = {
        "HOME": str(output / "home"),
        "TMPDIR": str(output / "t"),
        "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
    }
    grants = native_macos.grant_test_user_access(
        "_tpcci", [str(venv / "bin" / "python")], source, output, environment)
    assert grants["interpreter_outside_work"] is True
    assert all(path.is_relative_to(work.resolve()) for _, path, _ in recorded)
    assert outside.resolve() not in {path for _, path, _ in recorded}
    assert frozen.resolve() not in {path for _, path, _ in recorded}
    write_paths = {path for user, path, permissions in recorded
                   if user == "_tpcci" and permissions.startswith("read,write")}
    assert write_paths
    assert all(path.is_relative_to(output.resolve()) for path in write_paths)
    assert (work.resolve(),) and any(path == work.resolve() and permissions == "execute"
                                     for user, path, permissions in recorded if user == "_tpcci")
    runner = native_macos.supervisor_identity()["name"]
    assert any(user == runner and path.is_relative_to(output.resolve()) for user, path, _ in recorded)
    assert not any(user.isdigit() for user, _, _ in recorded)

    recorded.clear()
    environment["PLAYWRIGHT_BROWSERS_PATH"] = str(tmp_path / "outside-browsers")
    (tmp_path / "outside-browsers").mkdir()
    with pytest.raises(RuntimeError, match="TPC_WORK"):
        native_macos.grant_test_user_access(
            "_tpcci", [str(venv / "bin" / "python")], source, output, environment)
    assert recorded == []


def test_private_immutable_file_is_not_silently_skipped(monkeypatch, tmp_path):
    import native_macos

    target = tmp_path / "frozen"
    target.write_text("secret")
    target.chmod(0o600)

    class Private:
        st_flags = stat.UF_IMMUTABLE
        st_mode = stat.S_IFREG | 0o600

    class Readable:
        st_flags = stat.UF_IMMUTABLE
        st_mode = stat.S_IFREG | 0o644

    monkeypatch.setattr(Path, "lstat", lambda self: Private())
    with pytest.raises(RuntimeError, match="不可读"):
        native_macos.acl_skipped(target)
    monkeypatch.setattr(Path, "lstat", lambda self: Readable())
    assert native_macos.acl_skipped(target) is True


def test_fresh_source_without_tools_directory(monkeypatch, tmp_path):
    """白名单不含 tools/：pristine 没有该目录时副本照常生成。"""
    import run_matrix

    pristine = tmp_path / "pristine"
    for directory in ("src", "scripts", "tests", "config", "db", "docs", ".github/workflows"):
        (pristine / directory).mkdir(parents=True)
    for file in ("pyproject.toml", "uv.lock", "AGENTS.md", "build_support.py", "MANIFEST.in",
                 ".github/workflows/macos-test-lanes.yml"):
        (pristine / file).write_text("source")
    monkeypatch.setattr(run_matrix, "restore_frozen", lambda source: None)
    copied = run_matrix.fresh_source(pristine, tmp_path / "copy")
    assert not (copied / "tools").exists()
    assert (copied / "src").is_dir()


@pytest.mark.parametrize("extra_module, passed", [(None, True), ("ctf_scrapling_preflight", False)])
def test_assembly_check_rejects_modules_outside_source_src(tmp_path, extra_module, passed):
    """选站装配只允许从源码副本的 src 装载项目模块；scripts 等路径被插入 sys.path 时失败。"""
    import subprocess
    import run_matrix

    root = Path(__file__).resolve().parents[1]
    support = tmp_path / "ci_support"
    support.mkdir()
    # 本单测只核对装配后的模块来源判定；守卫与网络探针由 CI 沙箱内的真实副本承担。
    (support / "ci_execution_guard.py").write_text("def install():\n    pass\ndef canary():\n    pass\n")
    (support / "ci_lane_report.py").write_text("def denied_network_probe(*args):\n    pass\n")
    modules = list(run_matrix.ROOT_ASSEMBLY_MODULES) + ([extra_module] if extra_module else [])
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PYTHONNOUSERSITE": "1",
                   "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "scripts"), str(support)])}
    result = subprocess.run(
        [sys.executable, "-P", "-c", run_matrix.ASSEMBLY_CHECK, str(tmp_path), str(root),
         ",".join(modules), ",".join(run_matrix.WORKER_PLATFORM_CODES)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120, check=False)
    assert (result.returncode == 0) is passed, result.stdout[-2000:] + result.stderr[-2000:]
    if not passed:
        assert "modules loaded outside src: ctf_scrapling_preflight" in result.stderr


def test_policy_denies_project_login_profiles(tmp_path):
    source = (tmp_path / "source").resolve()
    source.mkdir()
    policy = run_lanes.sandbox_policy(source, tmp_path / "output", "component")
    relative = "data/runtime/platform_sessions"
    assert f'(deny file-read* file-write* (subpath {json.dumps(str(source / relative))}))' in policy
    assert "tools/MediaCrawler" not in policy
