"""卡片闸门的离线组件测试；只注入数据，不启动沙箱或测试子进程。"""

import ast
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/dev"))
import card_gate as gate
import sandbox_linux
import sandbox_macos


def test_policy_covers_all_boundaries():
    policy = sandbox_macos.sandbox_policy(Path('/tmp/带 空格/"本轮"'),
                                 [Path("/work/main"), Path("/work/卡 G")], Path("/Users/测试"))
    assert "(deny network*)" in policy
    assert "(allow network* (remote unix-socket))" in policy
    assert '(literal "/usr/bin/open")' in policy
    assert '(literal "/usr/bin/osascript")' in policy
    assert "(?i).*(chrome|chromium|safari|firefox|webkit|msedge|MiniBrowser).*" in policy
    for profile in ("Google/Chrome", "Google/Chrome for Testing", "Google/ChromeForTesting", "Chromium"):
        path = "/Users/测试/Library/Application Support/" + profile
        assert f"(deny file-read* file-write* (subpath {json.dumps(path, ensure_ascii=False)}))" in policy
    for root in ("/work/main", "/work/卡 G"):
        for name in ("data", "outputs"):
            assert f'(deny file-write* (subpath "{root}/{name}"))' in policy
        # 真实平台登录资料禁读写：T14-A 的 platform_sessions 与删除前的 fork browser_data。
        for name in ("data/runtime/platform_sessions", "tools/MediaCrawler/browser_data"):
            assert f'(deny file-read* file-write* (subpath "{root}/{name}"))' in policy
    assert "(require-all" in policy
    assert '(require-not (subpath "/dev"))' in policy
    assert '(require-not (subpath "/tmp/带 空格/\\\"本轮\\\""))' in policy


def test_junit_failure_error_collection_and_xfail(tmp_path):
    xml = tmp_path / "pytest.xml"
    xml.write_text('''<testsuites><testsuite name="pytest">
      <testcase classname="tests.test_demo" name="test_failed[a.b]"><failure/></testcase>
      <testcase classname="tests.test_demo.TestGroup" name="test_setup"><error/></testcase>
      <testcase name="tests/test_broken.py"><error message="collection failure"/></testcase>
      <testcase classname="tests.test_demo" name="test_xfail"><skipped type="pytest.xfail"/></testcase>
      <testcase classname="tests.test_demo" name="test_passed"/>
    </testsuite></testsuites>''')
    counts = {"selected": 4, "passed": 1, "failed": 1, "errors": 1, "collection_errors": 1}
    result = gate.junit_result(xml, counts, 2)
    assert result == {"failures": ["tests/test_broken.py", "tests/test_demo.py::TestGroup::test_setup",
                                   "tests/test_demo.py::test_failed[a.b]"],
                      "counts": counts, "returncode": 2}


def test_junit_file_preserves_arbitrary_class_name(tmp_path):
    xml = tmp_path / "pytest.xml"
    xml.write_text('''<testsuite><testcase file="tests/test_demo.py"
      classname="tests.test_demo.BehaviorSuite" name="test_failed[a.b]"><failure/>
      </testcase></testsuite>''')
    result = gate.junit_result(xml, {}, 1)
    assert result["failures"] == ["tests/test_demo.py::BehaviorSuite::test_failed[a.b]"]


def test_output_preflight_failure_returns_two(monkeypatch, capsys):
    def unavailable(**kwargs):
        raise OSError("临时目录不可写")

    monkeypatch.setattr(gate.tempfile, "mkdtemp", unavailable)
    assert gate.main([]) == 2
    assert "结论：未通过（闸门预检失败：临时目录不可写）" in capsys.readouterr().out


def lane(failures=(), passed=10, selected=12, code=1):
    return {"failures": list(failures), "counts": {"passed": passed, "selected": selected}, "returncode": code}


@pytest.mark.parametrize("case,passed,fragment", [
    ("same", True, ""), ("added", False, "新增失败"),
    ("removed", True, "变化：基线失败消失"), ("fewer", True, "通过数少于基线"),
    ("crashed", False, "未完整运行"),
])
def test_compare_lane(case, passed, fragment):
    baseline = lane(["tests/test_x.py::test_a"])
    current = lane(baseline["failures"])
    if case == "added":
        current["failures"].append("tests/test_x.py::test_b")
    elif case == "removed":
        current["failures"], current["returncode"] = [], 0
    elif case == "fewer":
        current["counts"]["passed"] -= 1
    elif case == "crashed":
        current["returncode"] = 124
    comparison = gate.compare_lane(current, baseline)
    assert comparison["passed"] is passed
    assert fragment in "、".join(comparison["notes"] + comparison["errors"])


def write_sources(root, files):
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


@pytest.mark.parametrize("case,bucket", [
    ("equal", "equal"), ("different", "different"), ("missing", "missing"),
    ("pending", "pending"), ("delegated", "equal"), ("reexported", "equal"),
    ("mixin", "equal"), ("exited", "exited"),
])
def test_ast_review_uses_ledger_locator(tmp_path, case, bucket):
    original, target = "scripts/original.py", "src/trippostcollect/platforms/xhs/session.py"
    old = "def f():\n    return 1\n"
    files = {original: "", target: old}
    row = {"file": original, "qualname": "f", "disposition": "迁", "card": "T09",
           "target": "platforms/xhs/session.py"}
    rows = [row]
    if case == "different":
        files[target] = old.replace("1", "2")
    elif case == "missing":
        files[target] = ""
    elif case == "pending":
        files[original] = old.replace("1", "2")
    elif case == "delegated":
        files[original] = ("from trippostcollect.platforms.xhs.session import f as worker\n"
                           "def f():\n    return worker()\n")
    elif case == "reexported":
        files[target] = "from trippostcollect.records.shared import f\n"
        files["src/trippostcollect/records/shared.py"] = old
    elif case == "mixin":
        old = "class C:\n    def f(self):\n        return 1\n"
        row["qualname"] = "C.f"
        rows.append({**row, "qualname": "C", "target": "platforms/xhs/core.py"})
        files[target] = old.replace("class C:", "class Session:")
        files["src/trippostcollect/platforms/xhs/core.py"] = (
            "from trippostcollect.platforms.xhs.session import Session\nclass C(Session):\n    pass\n")
    elif case == "exited":
        row["disposition"] = "退"
    write_sources(tmp_path, files)
    result = gate.ast_review(tmp_path, rows, lambda path: old, cards=["T09"])
    assert result[bucket][0]["definition"] == f"{original}:{row['qualname']}"
    if case == "different":
        assert "-    return 1" in result[bucket][0]["diff"]
        assert "+    return 2" in result[bucket][0]["diff"]
    if case in ("delegated", "reexported", "mixin"):
        location = result["equal"][0]["location"]
        assert location["file"] != original
        assert location["qualname"] == ("Session.f" if case == "mixin" else "f")
    if case == "pending":
        assert result["missing"] == []


def test_ast_automatic_scope_and_manual_pair(tmp_path):
    definition = "def f():\n    return 1\n"
    write_sources(tmp_path, {"scripts/a.py": definition, "scripts/b.py": definition})
    row = {"file": "scripts/a.py", "qualname": "f", "disposition": "迁",
           "target": "scripts/b.py", "card": "T00"}
    assert gate.ast_review(tmp_path, [row], lambda _: definition)["cards"] == []
    result = gate.ast_review(tmp_path, [row], lambda _: definition,
                             pairs=[("scripts/a.py", "scripts/b.py")])
    assert len(result["equal"]) == 1
    (tmp_path / "scripts/a.py").write_text("")
    result = gate.ast_review(tmp_path, [row], lambda _: definition)
    assert result["cards"] == ["T00"]
    assert len(result["equal"]) == 1


@pytest.mark.parametrize("case,bucket,previously_moved", [
    ("changed", "different", 1), ("unchanged", None, 0),
    ("new-move", "equal", 0), ("missing", "missing", 1),
    ("pending", "pending", 1), ("pending-changed", "different", 1),
])
def test_ast_review_locates_both_revisions(tmp_path, case, bucket, previously_moved):
    original, target = "scripts/original.py", "src/trippostcollect/platforms/xhs/session.py"
    old = "def f():\n    return 1\n"
    row = {"file": original, "qualname": "f", "disposition": "迁", "card": "T09",
           "target": "platforms/xhs/session.py"}
    before = {original: "", target: old}
    after = before.copy()
    if case == "changed":
        after[target] = old.replace("1", "2")
    elif case == "new-move":
        before = {original: old, target: ""}
    elif case == "missing":
        after[target] = ""
    elif case == "pending":
        after[original] = old
    elif case == "pending-changed":
        after[original] = old.replace("1", "2")
    reads = []

    def reader(path):
        reads.append(path)
        return before.get(path, "")

    write_sources(tmp_path, after)
    result = gate.ast_review(tmp_path, [row], reader)
    if bucket:
        assert len(result[bucket]) == 1
    assert result["cards"] == (["T09"] if bucket else [])
    assert result["previously_moved"] == previously_moved
    assert len(reads) == len(set(reads))
    if bucket:
        item = result[bucket][0]
        assert item["base_location"]["file"] == (original if case == "new-move" else target)
        assert item["base_location"]["state"] == ("pending" if case == "new-move" else "moved")
        assert item["location"]["state"] == (
            "missing" if case == "missing" else "pending" if case.startswith("pending") else "moved"
        )
        if case in ("changed", "pending-changed"):
            assert "+    return 2" in item["diff"]
        if case.startswith("pending"):
            assert len(result["pending"]) == 1
    else:
        assert not any(result[key] for key in ("equal", "different", "missing", "pending", "exited"))
    summary = gate.render_summary({"ast": result, "canary": {"passed": True}, "report_path": "/tmp/report.json"})
    assert f"其中前序已迁 {previously_moved}" in summary


@pytest.mark.parametrize("binding", ["delegated", "reexported", "mixin"])
def test_ast_base_location_follows_shared_resolution(tmp_path, binding):
    original = "scripts/original.py"
    target = "src/trippostcollect/platforms/xhs/session.py"
    implementation = target
    row = {"file": original, "qualname": "f", "disposition": "迁", "card": "T09",
           "target": "platforms/xhs/session.py"}
    rows = [row]
    files = {original: "", target: "def f():\n    return 1\n"}
    if binding == "delegated":
        files[original] = ("from trippostcollect.platforms.xhs.session import f as worker\n"
                           "def f():\n    return worker()\n")
    elif binding == "reexported":
        implementation = "src/trippostcollect/records/shared.py"
        files[implementation] = files[target]
        files[target] = "from trippostcollect.records.shared import f\n"
    else:
        row["qualname"] = "C.f"
        rows.append({**row, "qualname": "C", "target": "platforms/xhs/core.py"})
        files[target] = "class Session:\n    def f(self):\n        return 1\n"
        files["src/trippostcollect/platforms/xhs/core.py"] = (
            "from trippostcollect.platforms.xhs.session import Session\nclass C(Session):\n    pass\n")
    before = files.copy()
    files[implementation] = files[implementation].replace("return 1", "return 2")
    write_sources(tmp_path, files)
    result = gate.ast_review(tmp_path, rows, lambda path: before.get(path, ""))
    assert len(result["different"]) == result["previously_moved"] == 1
    item = result["different"][0]
    assert item["base_location"] == item["location"] == {
        "state": "moved", "file": implementation, "qualname": "Session.f" if binding == "mixin" else "f",
    }
    assert "return 1" in item["diff"] and "return 2" in item["diff"]


def test_changed_files_only_existing_python(tmp_path):
    write_sources(tmp_path, {"scripts/new.py": "", "README.md": ""})
    result = gate.changed_files(tmp_path, "temp/test.py\0scripts/gone.py\0scripts/new.py\0README.md\0temp\0")
    assert result["root"] == ["README.md", "scripts/gone.py", "scripts/new.py"]
    assert result["compile"] == result["ruff"] == ["scripts/new.py"]


@pytest.mark.parametrize("change", [None, "script_hash", "root_commit", "lanes"])
def test_baseline_cache_identity(change):
    identity = gate.cache_identity("root", "hash")
    assert identity == {"root_commit": "root", "script_hash": "hash"}
    cached = {"identity": identity.copy(), "lanes": dict.fromkeys(gate.ROOT_LANES)}
    if change == "lanes":
        cached["lanes"] = dict.fromkeys(("component", "installation"))
    elif change:
        cached["identity"][change] += "-changed"
    assert gate.cache_valid(cached, identity, gate.ROOT_LANES) is (change is None)


def test_archive_source_exports_root_only(monkeypatch, tmp_path):
    import io
    import tarfile

    calls = []

    def archive(repository, *arguments):
        calls.append((repository, arguments))
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as stream:
            data = b"x = 1\n"
            info = tarfile.TarInfo("src/a.py")
            info.size = len(data)
            stream.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    monkeypatch.setattr(gate, "git_bytes", archive)
    gate.archive_source(tmp_path / "root", "base", tmp_path / "out")
    assert calls == [(tmp_path / "root", ("archive", "base"))]
    assert (tmp_path / "out/src/a.py").read_text() == "x = 1\n"


def test_summary_has_no_fork_transition_line():
    summary = gate.render_summary({
        "canary": {"passed": True}, "report_path": "/tmp/r.json",
        "files": {"root": ["scripts/a.py"], "compile": ["scripts/a.py"], "ruff": ["scripts/a.py"], "failures": []},
        "ast": {"equal": [], "different": [], "missing": [], "pending": [], "exited": [], "previously_moved": 0},
    })
    assert "改动文件：1；编译 1，ruff 1" in summary
    assert "fork" not in summary


@pytest.mark.parametrize("skip,errors,conclusion", [
    (False, [], "结论：通过"), (False, ["编译失败"], "结论：未通过（编译失败）"),
    (True, [], "结论：部分（测试未运行）"), (True, ["编译失败"], "结论：未通过（编译失败）"),
])
def test_summary_conclusion(skip, errors, conclusion):
    report = {"canary": {"passed": True}, "skip_tests": skip,
              "errors": errors, "report_path": "/tmp/report.json"}
    summary = gate.render_summary(report)
    assert summary.splitlines()[-1] == conclusion
    if skip:
        assert "测试：未运行" in summary
    assert "JSON 报告：/tmp/report.json" in summary


def test_no_production_imports():
    # 闸门的依赖限于标准库和台账工具，不依赖或执行业务模块。
    tree = ast.parse(Path(gate.__file__).read_text())
    imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not any(name.startswith("trippostcollect") for name in imports)


def run_bpf(program, arch, nr, arg0=0):
    """最小经典 BPF 解释器：只实现过滤器用到的 LD|W|ABS、JEQ/JGE|K 与 RET|K。"""
    import struct

    instructions = [struct.unpack("HBBI", program[i:i + 8]) for i in range(0, len(program), 8)]
    data = struct.pack("<iIQQ", nr, arch, 0, arg0)
    pc, accumulator = 0, 0
    while True:
        code, jt, jf, k = instructions[pc]
        if code == 0x20:
            accumulator = struct.unpack_from("<I", data, k)[0]
            pc += 1
        elif code in (0x15, 0x35):
            taken = accumulator == k if code == 0x15 else accumulator >= k
            pc += 1 + (jt if taken else jf)
        elif code == 0x06:
            return k
        else:
            raise AssertionError(f"未知指令 {code:#x}")


@pytest.mark.parametrize("machine, arch, nr_socket, nr_other, x32", [
    ("x86_64", 0xC000003E, 41, 0, 0x40000000),
    ("aarch64", 0xC00000B7, 198, 63, None),
])
def test_linux_seccomp_denies_ip_sockets_only(machine, arch, nr_socket, nr_other, x32):
    # 过滤器面向 Linux 内核 ABI，测试同样使用 Linux 取值，在 macOS 上生成与验证结果一致。
    af_unix, af_inet, af_inet6, af_netlink, af_packet = 1, 2, 10, 16, 17
    program = sandbox_linux.seccomp_program(machine)
    allow, deny = 0x7FFF0000, 0x00050000 | 1  # SECCOMP_RET_ERRNO | EPERM
    for family in (af_inet, af_inet6, af_packet):
        assert run_bpf(program, arch, nr_socket, family) == deny
    for family in (af_unix, af_netlink):
        assert run_bpf(program, arch, nr_socket, family) == allow
    assert run_bpf(program, arch, nr_other, af_inet) == allow
    assert run_bpf(program, arch, 425) == deny  # io_uring_setup
    assert run_bpf(program, 0x40000003, nr_socket, af_unix) == deny  # i386 兼容调用
    if x32 is not None:
        assert run_bpf(program, arch, x32 | nr_socket, af_unix) == deny
    with pytest.raises(RuntimeError, match="架构"):
        sandbox_linux.seccomp_program("riscv64")


def test_linux_bwrap_arguments_cover_all_boundaries():
    arguments = sandbox_linux.bwrap_arguments(
        Path("/tmp/带 空格/本轮"), [(Path("/opt/google/chrome"), True), (Path("/usr/bin/xdg-open"), False)],
        Path("/deny/dir"), Path("/deny/file"), 7)
    joined = " ".join(arguments)
    assert arguments[:5] == ["bwrap", "--ro-bind", "/", "/", "--dev"]
    assert "--bind /tmp/带 空格/本轮 /tmp/带 空格/本轮" in joined
    assert "--unshare-net" in arguments and "--die-with-parent" in arguments
    assert "--ro-bind /deny/dir /opt/google/chrome" in joined
    assert "--ro-bind /deny/file /usr/bin/xdg-open" in joined
    assert arguments[-3:] == ["--seccomp", "7", "--"]
    # 遮蔽必须排在只读根之后，否则会被根绑定覆盖。
    assert arguments.index("/opt/google/chrome") > arguments.index("/")


def test_linux_browser_targets_collapse_to_matching_directories(tmp_path):
    opt, bin_dir = tmp_path / "opt", tmp_path / "bin"
    (opt / "google/chrome").mkdir(parents=True)
    (opt / "google/chrome/chrome").write_text("")
    (opt / "google/chrome/google-chrome").write_text("")
    (opt / "editor").mkdir()
    bin_dir.mkdir()
    (bin_dir / "xdg-open").write_text("")
    (bin_dir / "google-chrome").symlink_to(opt / "google/chrome/google-chrome")
    (bin_dir / "python3").write_text("")
    targets = sandbox_linux.browser_targets(str(bin_dir), roots=(opt,))
    assert (opt / "google/chrome").resolve() in targets
    assert (bin_dir / "xdg-open").resolve() in targets
    assert not any(path.name in {"python3", "editor"} for path in targets)
    assert not any((opt / "google/chrome").resolve() in path.parents for path in targets)


def test_profile_stores_match_and_linux_masks_existing_checkout_profiles(tmp_path):
    assert sandbox_linux.PROFILE_STORES == sandbox_macos.PROFILE_STORES
    assert "data/runtime/platform_sessions" in sandbox_linux.PROFILE_STORES
    checkout = tmp_path / "checkout"
    (checkout / "data/runtime/platform_sessions").mkdir(parents=True)
    targets = sandbox_linux.profile_targets(tmp_path / "home", [checkout])
    assert targets == [checkout / "data/runtime/platform_sessions"]


def test_linux_canary_probes_masked_opener_and_profile(tmp_path):
    import errno

    policy = {"masked": [(Path("/usr/bin/xdg-open"), False), (Path("/opt/google/chrome"), True),
                         (Path("/opt/app/chrome-sandbox"), False)]}
    spec = sandbox_linux.canary_spec(tmp_path, policy)
    assert spec["exec"] == ["/usr/bin/xdg-open", "/opt/app/chrome-sandbox"]
    assert spec["profile"] == str(tmp_path / ".config/google-chrome")
    assert spec["profile_exists"] is False
    assert errno.EROFS in spec["denied_errnos"]
    assert sandbox_macos.canary_spec(tmp_path, None)["exec"] == ["/usr/bin/open"]
    assert errno.EROFS not in sandbox_macos.canary_spec(tmp_path, None)["denied_errnos"]


def test_sandbox_backend_follows_platform():
    assert gate.sandbox_backend("darwin") is sandbox_macos
    assert gate.sandbox_backend("linux") is sandbox_linux
    with pytest.raises(RuntimeError, match="不支持的平台"):
        gate.sandbox_backend("win32")


def test_macos_wrap_keeps_seatbelt_command():
    wrapped, descriptors = sandbox_macos.wrap(Path("/tmp/p.sb"), ["python", "-c", "pass"])
    assert wrapped == ["/usr/bin/sandbox-exec", "-f", "/tmp/p.sb", "python", "-c", "pass"]
    assert descriptors == ()


@pytest.mark.parametrize("backend", [sandbox_macos, sandbox_linux])
def test_canary_profile_probes_are_masked_by_both_backends(tmp_path, backend):
    checkout, probes = gate.canary_profile_probes(tmp_path, backend.PROFILE_STORES)
    assert probes == [str(checkout / relative) for relative in backend.PROFILE_STORES]
    assert all((Path(path) / "probe").is_file() and Path(path).is_relative_to(tmp_path) for path in probes)
    if backend is sandbox_macos:
        policy = sandbox_macos.sandbox_policy(tmp_path, [Path("/work/main"), checkout], Path("/Users/测试"))
        for path in probes:
            assert f"(deny file-read* file-write* (subpath {json.dumps(path, ensure_ascii=False)}))" in policy
    else:
        targets = sandbox_linux.profile_targets(tmp_path / "home", [Path("/nonexistent-checkout"), checkout])
        assert [str(path) for path in targets] == probes
    # canary 脚本逐个探测这些目录，任何一个可读即判未通过。
    assert 'spec.get("project_profiles", [])' in gate.CANARY
