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
    ("fork-count", False, "fork 选中数不符"), ("crashed", False, "未完整运行"),
])
def test_compare_lane(case, passed, fragment):
    baseline = lane(["tests/test_x.py::test_a"])
    current = lane(baseline["failures"])
    expected = None
    if case == "added":
        current["failures"].append("tests/test_x.py::test_b")
    elif case == "removed":
        current["failures"], current["returncode"] = [], 0
    elif case == "fewer":
        current["counts"]["passed"] -= 1
    elif case == "fork-count":
        expected = 284
    elif case == "crashed":
        current["returncode"] = 124
    comparison = gate.compare_lane(current, baseline, expected)
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
    write_sources(tmp_path, {"scripts/new.py": "", "tools/MediaCrawler/a.py": "", "README.md": ""})
    result = gate.changed_files(tmp_path,
                                "temp/test.py\0scripts/gone.py\0scripts/new.py\0README.md\0tools/MediaCrawler\0",
                                "a.py\0temp/probe.py\0gone.py\0")
    assert result["root"] == ["README.md", "scripts/gone.py", "scripts/new.py"]
    assert result["fork"] == ["a.py", "gone.py"]
    assert result["compile"] == ["scripts/new.py", "tools/MediaCrawler/a.py"]
    assert result["ruff"] == ["scripts/new.py"]


@pytest.mark.parametrize("change", [None, "script_hash", "fork_python", "root_commit", "fork_commit", "lanes"])
def test_baseline_cache_identity(change):
    identity = gate.cache_identity("root", "fork", "hash", Path("/env/bin/python"))
    lanes = gate.selected_lanes("fork-commit")
    cached = {"identity": identity.copy(), "lanes": dict.fromkeys(lanes)}
    if change == "lanes":
        lanes = gate.selected_lanes(None)
    elif change:
        cached["identity"][change] += "-changed"
    assert gate.cache_valid(cached, identity, lanes) is (change is None)


def test_lanes_and_cache_identity_without_fork():
    assert gate.selected_lanes("abc") == ("component", "installation", "os", "fork")
    assert gate.selected_lanes(None) == ("component", "installation", "os")
    identity = gate.cache_identity("root", None, "hash", None)
    assert identity["fork_commit"] is None and identity["fork_python"] is None
    assert gate.cache_valid({"identity": identity, "lanes": dict.fromkeys(gate.ROOT_LANES)}, identity, gate.ROOT_LANES)


@pytest.mark.parametrize("base_fork,head_fork,transition", [
    ("f" * 40, "f" * 40, "present"), ("f" * 40, None, "removing"), (None, None, "absent"),
])
def test_fork_plan_covers_three_transitions(monkeypatch, tmp_path, base_fork, head_fork, transition):
    checkout, common = tmp_path / "main", tmp_path / "main/.git"
    monkeypatch.setattr(gate.ledger, "fork_gitlink",
                        lambda root, revision=None: base_fork if revision else head_fork)
    monkeypatch.setattr(gate.ledger, "git_read", lambda root, *args: str(tmp_path / "wt-git"))
    searched = []

    def repository(candidates, commit):
        searched.append((candidates, commit))
        return candidates[-1]

    monkeypatch.setattr(gate, "fork_repository", repository)
    plan = gate.fork_plan(tmp_path / "wt", "base", checkout, common)
    assert plan["transition"] == transition
    assert plan["lanes"] == gate.selected_lanes(head_fork)
    if transition == "present":
        assert searched == [([tmp_path / "wt" / gate.FORK], base_fork)]
        assert plan["python"] == (checkout / gate.FORK / ".venv/bin/python").absolute()
    elif transition == "removing":
        # 本次已无 fork：基线 fork 源码改由子模块 gitdir、主 checkout 或公共 modules 提供，不要求 fork 解释器。
        assert searched == [([tmp_path / "wt" / gate.FORK, tmp_path / "wt-git/modules" / gate.FORK,
                              checkout / gate.FORK, common / "modules" / gate.FORK], base_fork)]
        assert plan["python"] is None and plan["repository"] == common / "modules" / gate.FORK
    else:
        assert searched == [] and plan["repository"] is None and plan["python"] is None


def test_fork_plan_rejects_reintroduced_fork(monkeypatch, tmp_path):
    monkeypatch.setattr(gate.ledger, "fork_gitlink", lambda root, revision=None: None if revision else "f" * 40)
    with pytest.raises(RuntimeError, match="不得重新引入"):
        gate.fork_plan(tmp_path, "base", tmp_path, tmp_path / ".git")


def test_fork_repository_requires_commit(tmp_path):
    import subprocess

    repo = tmp_path / "fork"
    repo.mkdir()
    run = lambda *args: subprocess.run(["git", "-C", str(repo), *args], check=True,  # noqa: E731
                                       capture_output=True, text=True).stdout.strip()
    run("init", "-q")
    run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "x")
    commit = run("rev-parse", "HEAD")
    assert gate.fork_repository([tmp_path / "missing", repo], commit) == repo
    with pytest.raises(RuntimeError, match="无法导出基线"):
        gate.fork_repository([repo], "0" * 40)


@pytest.mark.parametrize("fork_base", [None, "f" * 40])
def test_archive_source_exports_fork_only_when_base_has_gitlink(monkeypatch, tmp_path, fork_base):
    import io
    import tarfile

    calls = []

    def archive(repository, *arguments):
        calls.append((repository, arguments))
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w"):
            pass
        return buffer.getvalue()

    monkeypatch.setattr(gate, "git_bytes", archive)
    gate.archive_source(tmp_path / "root", "base", fork_base, tmp_path / "out", tmp_path / "modules")
    expected = [(tmp_path / "root", ("archive", "base"))]
    if fork_base:
        expected.append((tmp_path / "modules", ("archive", fork_base)))
    assert calls == expected
    assert (tmp_path / "out" / gate.FORK).is_dir() is bool(fork_base)


@pytest.mark.parametrize("case,bucket", [
    ("moved", "retired"), ("exited", "retired"), ("missing", "missing"), ("not-retiring", "different"),
])
def test_ast_review_treats_fork_removal_as_expected_exit(tmp_path, monkeypatch, case, bucket):
    original = gate.FORK + "/store/demo.py"
    target = "src/trippostcollect/artifacts/staging.py"
    row = {"file": original, "qualname": "DemoImage.__init__", "disposition": "拆", "card": "T06",
           "target": "artifacts/staging.py"}
    old = "class DemoImage:\n    def __init__(self):\n        self.root = 1\n"
    kind = "退出" if case == "exited" else "迁至"
    monkeypatch.setattr(gate.ledger, "PROGRESS_RESOLUTIONS", {
        (original, row["qualname"]): (kind, None if kind == "退出" else "Stager.__init__", "依据")})
    write_sources(tmp_path, {target: "" if case == "missing" else
                             "class Stager:\n    def __init__(self, root):\n        self.root = root\n"})
    result = gate.ast_review(tmp_path, [row], lambda path: old if path == original else "",
                             retiring=case != "not-retiring")
    assert [item["definition"] for item in result[bucket]] == [f"{original}:{row['qualname']}"]
    assert result["cards"] == ["T06"]
    if bucket == "retired":
        assert result["retired"][0]["base_location"]["state"] == "pending"
        assert result["retired"][0]["location"]["state"] == ("exited" if case == "exited" else "moved")
    summary = gate.render_summary({"ast": result, "canary": {"passed": True}, "report_path": "/tmp/r.json",
                                   "fork": {"transition": "removing"}})
    assert f"随 fork 删除预期退出 {len(result['retired'])}" in summary
    assert "T14-C 过渡" in summary


def test_retired_source_only_covers_deleted_fork_and_bridge_files(tmp_path):
    write_sources(tmp_path, {"scripts/kept.py": ""})
    assert gate.retired_source(tmp_path, gate.FORK + "/main.py")
    assert gate.retired_source(tmp_path, "scripts/mediacrawler_export_entrypoint.py")
    assert not gate.retired_source(tmp_path, "scripts/gone.py")
    write_sources(tmp_path, {gate.FORK + "/main.py": ""})
    assert not gate.retired_source(tmp_path, gate.FORK + "/main.py")


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
