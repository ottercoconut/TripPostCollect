"""mac_lanes 的编排契约。只注入假子进程和假 lane，不启动沙箱。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import resource
import shutil
from types import SimpleNamespace

import pytest


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "mac_lanes.py"
    spec = importlib.util.spec_from_file_location("mac_lanes_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mac_lanes = _load()


def test_non_macos_refuses_to_run(monkeypatch, capsys):
    monkeypatch.setattr(mac_lanes.sys, "platform", "linux")
    called = []
    monkeypatch.setattr(mac_lanes, "working_tree_status", lambda root: called.append(root) or "")
    assert mac_lanes.main([]) == 2
    assert called == []
    assert "只支持 macOS" in capsys.readouterr().err


def test_rejects_arguments_that_could_select_os_lane(monkeypatch, capsys):
    monkeypatch.setattr(mac_lanes.sys, "platform", "darwin")
    called = []
    monkeypatch.setattr(mac_lanes, "working_tree_status", lambda root: called.append(root) or "")
    assert mac_lanes.main(["--lane", "os"]) == 2
    assert called == []
    assert "不接受参数" in capsys.readouterr().err


def test_lanes_do_not_include_os(monkeypatch, tmp_path):
    assert mac_lanes.LANES == ("component", "socket", "installation")
    assert "os" not in mac_lanes.LANES
    with pytest.raises(RuntimeError, match="os"):
        mac_lanes.lane_argv("os", tmp_path / "tests" / "run_lanes.py", tmp_path / "source",
                            tmp_path / "out", tmp_path / "python")
    with pytest.raises(RuntimeError, match="os"):
        mac_lanes.run_lane("os", tmp_path, tmp_path / "tests" / "run_lanes.py",
                           tmp_path / "python", tmp_path)


def test_dirty_worktree_exits_before_checks(monkeypatch, capsys):
    monkeypatch.setattr(mac_lanes.sys, "platform", "darwin")
    monkeypatch.setattr(mac_lanes, "working_tree_status", lambda root: " M scripts/dev/mac_lanes.py\n")
    called = []
    monkeypatch.setattr(mac_lanes, "run_suite", lambda *args: called.append(args))
    assert mac_lanes.main([]) == 1
    assert called == []
    error = capsys.readouterr().err
    assert "未提交" in error
    assert "scripts/dev/mac_lanes.py" in error


def _patch_execution(monkeypatch, tmp_path, fail=None):
    monkeypatch.setattr(mac_lanes.sys, "platform", "darwin")
    monkeypatch.setattr(mac_lanes, "working_tree_status", lambda root: "")
    monkeypatch.setattr(mac_lanes, "raise_nofile_limit", lambda: 65536)
    # 这些用例会在 component lane 的 Seatbelt 里执行。沙箱不允许在 /private/tmp 另建目录，
    # 所以这里不调用真正的 mkdtemp，只验证 main 会建立并清掉自己的工作目录。
    created = []
    work = tmp_path / "work"

    def fake_work_dir():
        work.mkdir()
        created.append(work)
        return work

    def fake_cleanup(path):
        if path.exists():
            shutil.rmtree(path)

    monkeypatch.setattr(mac_lanes, "temporary_work_dir", fake_work_dir)
    monkeypatch.setattr(mac_lanes, "cleanup", fake_cleanup)
    lanes = []
    static_names = []

    def fake_prepare(root, work):
        if fail == "prepare":
            raise RuntimeError("uv sync 失败")
        pristine = work / "pristine"
        (pristine / "tests").mkdir(parents=True)
        return pristine, work / "root-venv" / "bin" / "python"

    def fake_static(name, command, cwd):
        static_names.append(name)
        assert "--lane" not in command
        assert "os" not in command
        ok = name != fail
        return mac_lanes.make_item(name, ok, 0.2, 0 if ok else 1, None, "" if ok else f"{name} 失败")

    def fake_lane(lane, pristine, runner, python, work):
        lanes.append(lane)
        assert lane in mac_lanes.LANES
        assert runner == pristine / "tests" / "run_lanes.py"
        ok = lane != fail
        counts = {
            "selected": 4, "passed": 4 if ok else 3, "failed": 0 if ok else 1,
            "errors": 0, "skipped": 0,
        }
        return mac_lanes.make_item(lane, ok, 1.5, 0 if ok else 1, counts, "" if ok else "lane 失败")

    monkeypatch.setattr(mac_lanes, "prepare_archived_env", fake_prepare)
    monkeypatch.setattr(mac_lanes, "run_static", fake_static)
    monkeypatch.setattr(mac_lanes, "run_lane", fake_lane)
    return created, lanes, static_names


@pytest.mark.parametrize("fail", [
    "component", "socket", "installation", "symbols", "inputs", "drift", "progress", "frozen", "ruff", "prepare",
])
def test_any_failed_item_exits_nonzero(monkeypatch, capsys, tmp_path, fail):
    created, lanes, static_names = _patch_execution(monkeypatch, tmp_path, fail=fail)
    assert mac_lanes.main([]) != 0
    output = capsys.readouterr().out
    assert "合计：未通过" in output
    assert "os lane 没跑，需要 GitHub CI。" in output
    assert "os" not in lanes
    assert static_names == ["symbols", "inputs", "drift", "progress", "frozen", "ruff"]
    if fail == "prepare":
        assert lanes == []
    else:
        assert lanes == ["component", "socket", "installation"]
    assert created and all(not path.exists() for path in created)


def test_success_reports_counts_duration_and_skips_os_lane(monkeypatch, capsys, tmp_path):
    created, lanes, static_names = _patch_execution(monkeypatch, tmp_path)
    assert mac_lanes.main([]) == 0
    output = capsys.readouterr().out
    assert lanes == ["component", "socket", "installation"]
    assert static_names == ["symbols", "inputs", "drift", "progress", "frozen", "ruff"]
    assert "component：通过，选中 4，通过 4，失败 0，错误 0，跳过 0，耗时 1.5 秒" in output
    assert "合计：通过" in output
    assert output.rstrip().endswith("os lane 没跑，需要 GitHub CI。")
    assert created and all(not path.exists() for path in created)


def test_run_lane_reuses_fresh_source_and_temporary_python(monkeypatch, tmp_path):
    pristine = tmp_path / "pristine"
    source = tmp_path / "copied"
    work = tmp_path / "work"
    source.mkdir()
    work.mkdir()
    runner = pristine / "tests" / "run_lanes.py"
    runner.parent.mkdir(parents=True)
    runner.write_text("", encoding="utf-8")
    python = tmp_path / "root-venv" / "bin" / "python"
    matrix = mac_lanes.load_run_matrix()
    seen = {}

    def fake_fresh(origin, destination):
        seen["fresh"] = (origin, destination)
        return source

    def fake_run(command, **kwargs):
        seen["argv"] = list(command)
        seen["cwd"] = kwargs["cwd"]
        seen["env_lane_keys"] = set(kwargs["env"])
        output = Path(command[command.index("-o") + 1])
        output.mkdir(parents=True)
        (output / "result.json").write_text(json.dumps({
            "returncode": 0,
            "counts": {"selected": 3, "passed": 3, "failed": 0, "errors": 0, "skipped": 0},
        }), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(matrix, "fresh_source", fake_fresh)
    monkeypatch.setattr(mac_lanes.subprocess, "run", fake_run)
    item = mac_lanes.run_lane("component", pristine, runner, python, work)
    assert seen["fresh"] == (pristine, work / "lane-component")
    assert seen["argv"][0] == str(python)
    assert seen["argv"][1] == str(runner)
    assert seen["argv"][seen["argv"].index("--lane") + 1] == "component"
    assert "os" not in seen["argv"]
    assert seen["cwd"] == runner.parent.parent
    assert "TPC_BROWSER_REPLAY" not in seen["env_lane_keys"]
    assert item["ok"] is True
    assert item["counts"]["selected"] == 3


def test_temporary_work_dir_is_created_under_private_tmp(monkeypatch):
    def fake_mkdtemp(prefix, dir):
        assert dir == "/private/tmp"
        assert prefix.startswith("tpc-mac-lanes.")
        return "/private/tmp/tpc-mac-lanes.contract"

    monkeypatch.setattr(mac_lanes.tempfile, "mkdtemp", fake_mkdtemp)
    assert mac_lanes.temporary_work_dir() == Path("/private/tmp/tpc-mac-lanes.contract")


def test_cleanup_refuses_to_delete_outside_private_tmp():
    with pytest.raises(RuntimeError, match="拒绝删除"):
        mac_lanes.cleanup(Path("/Users/example/mac-lanes-should-not-delete"))


def test_raise_nofile_limit_from_default_256(monkeypatch):
    state = {"limit": (256, resource.RLIM_INFINITY)}

    def getrlimit(which):
        assert which == resource.RLIMIT_NOFILE
        return state["limit"]

    def setrlimit(which, value):
        assert which == resource.RLIMIT_NOFILE
        state["limit"] = value

    monkeypatch.setattr(mac_lanes.resource, "getrlimit", getrlimit)
    monkeypatch.setattr(mac_lanes.resource, "setrlimit", setrlimit)
    assert mac_lanes.raise_nofile_limit() == 65536
    assert state["limit"] == (65536, resource.RLIM_INFINITY)


def test_raise_nofile_limit_keeps_a_higher_soft_limit(monkeypatch):
    monkeypatch.setattr(mac_lanes.resource, "getrlimit", lambda which: (1048575, resource.RLIM_INFINITY))
    monkeypatch.setattr(mac_lanes.resource, "setrlimit", lambda which, value: (_ for _ in ()).throw(AssertionError(value)))
    assert mac_lanes.raise_nofile_limit() == 1048575
