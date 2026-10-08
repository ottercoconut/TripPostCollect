"""T14 运行手册第 2、3 段迁移/校验脚本的错误处理回归。

从 docs/operations-runbook.md 按标记提取脚本，在临时项目目录中执行；ditto/cp/mv/stat/cmp/find/shasum
由 PATH 前置的 stub 提供，并可按参数注入失败。macOS 上 stub 调用系统 ditto、BSD stat/find，并固定用
/bin/bash（3.2）执行，验证真实 ditto 与 bash 3.2；Linux 没有 ditto 与 BSD `stat -f`，stub 用 `cp -a` 与
GNU stat 格式转换模拟。测试数据全部是假的，不访问真实 profile。
"""

from __future__ import annotations

import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "operations-runbook.md"
SNAP = "trippostcollect_cookie_snapshot.json"
LEGACY = Path("tools/MediaCrawler/browser_data")
SESSIONS = Path("data/runtime/platform_sessions")
BASH = "/bin/bash" if platform.system() == "Darwin" else "bash"
OLD_MTIME = 1_600_000_000

STUBS = {
    "ditto": """#!/bin/bash
[ -n "${STUB_DITTO_FAIL:-}" ] && exit 8
if [ -x /usr/bin/ditto ]; then
  exec /usr/bin/ditto "$@"
fi
exec /bin/cp -a "$1" "$2"
""",
    "cp": """#!/bin/bash
[ -n "${STUB_CP_FAIL:-}" ] && exit 9
exec /bin/cp "$@"
""",
    "mv": """#!/bin/bash
if [ -n "${STUB_MV_FAIL_MATCH:-}" ]; then
  case "$1" in
    *"$STUB_MV_FAIL_MATCH") exit 7 ;;
  esac
fi
exec /bin/mv "$@"
""",
    "stat": """#!/bin/bash
[ -n "${STUB_STAT_FAIL:-}" ] && exit 1
if [ -n "${STUB_STAT_FAIL_MATCH:-}" ]; then
  for arg in "$@"; do
    case "$arg" in
      *"$STUB_STAT_FAIL_MATCH"*) exit 1 ;;
    esac
  done
fi
if [ "$1" = "-f" ] && /usr/bin/stat --version >/dev/null 2>&1; then
  fmt=$2
  shift 2
  fmt=${fmt//%HT/%F}
  fmt=${fmt//%Lp/%a}
  fmt=${fmt//%Sp/%A}
  fmt=${fmt//%p/%f}
  fmt=${fmt//%z/%s}
  fmt=${fmt//%m/%Y}
  fmt=${fmt//%Su/%U}
  fmt=${fmt//%Sg/%G}
  exec /usr/bin/stat -c "$fmt" "$@"
fi
exec /usr/bin/stat "$@"
""",
    "cmp": """#!/bin/bash
if [ -n "${STUB_CMP_FAIL_MATCH:-}" ]; then
  for arg in "$@"; do
    case "$arg" in
      *"$STUB_CMP_FAIL_MATCH"*) exit 2 ;;
    esac
  done
fi
exec /usr/bin/cmp "$@"
""",
    "find": """#!/bin/bash
if /usr/bin/find --version >/dev/null 2>&1; then
  args=()
  for arg in "$@"; do
    [ "$arg" = "+077" ] && arg=/077
    args+=("$arg")
  done
  exec /usr/bin/find "${args[@]}"
fi
exec /usr/bin/find "$@"
""",
    "shasum": """#!/bin/bash
[ -n "${STUB_SHASUM_FAIL:-}" ] && exit 1
if [ -x /usr/bin/shasum ]; then
  exec /usr/bin/shasum "$@"
fi
exec sha256sum
""",
}


def runbook_script(step: str) -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    match = re.search(
        rf"<!-- t14-migrate:{step} -->\n\s*```bash\n(.*?)\n\s*```\n\s*<!-- /t14-migrate:{step} -->", text, re.S,
    )
    assert match, f"运行手册缺少 {step} 标记代码段"
    return "\n".join(line[3:] if line.startswith("   ") else line for line in match.group(1).splitlines()) + "\n"


def make_profile(directory: Path, code: str, root_mode: int, *, snapshot: bool) -> None:
    (directory / "Default" / "Sub").mkdir(parents=True)
    (directory / "Default" / "Cookies").write_bytes(b"cookie-db-" + code.encode())
    (directory / "Default" / "Sub" / "state").write_text("state", encoding="utf-8")
    (directory / "Default" / "Sub" / "state").chmod(0o640)
    (directory / "link").symlink_to("Default/Cookies")
    # Chrome SingletonLock 风格的悬空链接。
    (directory / "SingletonLock").symlink_to("fake-host-12345")
    if snapshot:
        (directory / SNAP).write_text('{"cookies": []}', encoding="utf-8")
        (directory / SNAP).chmod(0o600)
        os.utime(directory / SNAP, (OLD_MTIME + 3, OLD_MTIME + 3))
    os.utime(directory / "Default" / "Sub" / "state", (OLD_MTIME, OLD_MTIME))
    os.utime(directory / "Default" / "Sub", (OLD_MTIME + 1, OLD_MTIME + 1))
    os.utime(directory / "Default", (OLD_MTIME + 2, OLD_MTIME + 2))
    directory.chmod(root_mode)
    os.utime(directory, (OLD_MTIME + 4, OLD_MTIME + 4))


@pytest.fixture(params=[0o700, 0o755], ids=["root700", "root755"])
def root_mode(request) -> int:
    # 旧根权限不写死为 700：755 时 ditto 原样复制，第 3 段只要求新旧相同。
    return request.param


@pytest.fixture
def project(root_mode: int, tmp_path: Path) -> Path:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in STUBS.items():
        (stubs / name).write_text(body, encoding="utf-8")
        (stubs / name).chmod(0o755)
    root = tmp_path / "project"
    for code in ("bili", "wb"):
        make_profile(root / LEGACY / f"{code}_user_data_dir", code, root_mode, snapshot=True)
    make_profile(root / LEGACY / "cdp_dy_user_data_dir", "dy", root_mode, snapshot=False)
    return root


def run(project: Path, step: str, **flags: str) -> subprocess.CompletedProcess[str]:
    script = project.parent / f"{step}.sh"
    script.write_text(runbook_script(step), encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("STUB_")}
    environment["PATH"] = f"{project.parent / 'stubs'}{os.pathsep}{environment.get('PATH', '')}"
    environment.update(flags)
    return subprocess.run(
        [BASH, str(script)], cwd=project, env=environment,
        capture_output=True, text=True, timeout=300, check=False,
    )


def detail(result: subprocess.CompletedProcess[str]) -> str:
    """断言消息：返回码与 stdout/stderr 尾部（测试数据全为假数据），CI 失败时 junit 直接可见。"""
    return (f"bash={BASH} rc={result.returncode}\n--- stdout tail ---\n{result.stdout[-2000:]}"
            f"\n--- stderr tail ---\n{result.stderr[-2000:]}")


def verify_status(result: subprocess.CompletedProcess[str]) -> int:
    """第 3 段代码块以 `echo "exit=$?"` 结尾，脚本退出码以该行为准。"""
    lines = result.stdout.strip().splitlines()
    assert lines and re.fullmatch(r"exit=\d+", lines[-1]), detail(result)
    return int(lines[-1].split("=", 1)[1])


def tree_state(root: Path) -> dict[str, tuple[int, int, bytes | str | None]]:
    """旧侧逐条目权限、mtime 与内容（含根目录本身），用 lstat 不跟随链接。"""
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        if path.is_symlink():
            content: bytes | str | None = os.readlink(path)
        elif path.is_file():
            content = path.read_bytes()
        else:
            content = None
        result[str(path.relative_to(root))] = (stat.S_IMODE(info.st_mode), info.st_mtime_ns, content)
    return result


def migrated(project: Path) -> None:
    result = run(project, "step2")
    assert result.returncode == 0, detail(result)


def test_runbook_blocks_parse_with_bash(tmp_path: Path) -> None:
    for step in ("step2", "step3"):
        script = tmp_path / f"{step}.sh"
        script.write_text(runbook_script(step), encoding="utf-8")
        result = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, detail(result)


# ---------- 第 2 段：失败即整体停止，旧侧不变 ----------

@pytest.mark.parametrize("leftover", ["profile.partial", "cdp_profile.partial", f"{SNAP}.partial"])
def test_partial_leftover_stops_whole_script(project: Path, leftover: str) -> None:
    before = tree_state(project / LEGACY)
    target = project / SESSIONS / "bilibili" / leftover
    target.parent.mkdir(parents=True)
    if leftover.endswith(".json.partial"):
        target.write_text("partial", encoding="utf-8")
    else:
        target.mkdir()
    result = run(project, "step2")
    assert result.returncode != 0, detail(result)
    assert "FAILED: bilibili" in result.stderr and "left by an earlier run" in result.stderr, detail(result)
    assert not (project / SESSIONS / "bilibili" / "profile").exists()
    assert not (project / SESSIONS / "weibo").exists()
    assert not (project / SESSIONS / "douyin").exists()
    assert tree_state(project / LEGACY) == before


def test_snapshot_copy_failure_does_not_land_profile(project: Path) -> None:
    before = tree_state(project / LEGACY)
    result = run(project, "step2", STUB_CP_FAIL="1")
    assert result.returncode != 0, detail(result)
    assert "FAILED: bilibili snapshot: cp" in result.stderr, detail(result)
    bilibili = project / SESSIONS / "bilibili"
    assert not (bilibili / "profile").exists()
    assert not (bilibili / SNAP).exists()
    assert (bilibili / "profile.partial").is_dir()
    assert not (project / SESSIONS / "weibo").exists()
    assert tree_state(project / LEGACY) == before
    rerun = run(project, "step2")
    assert rerun.returncode != 0 and "left by an earlier run" in rerun.stderr, detail(rerun)


def test_ditto_failure_stops_before_later_platforms(project: Path) -> None:
    before = tree_state(project / LEGACY)
    result = run(project, "step2", STUB_DITTO_FAIL="1")
    assert result.returncode != 0, detail(result)
    assert "FAILED: bilibili profile: ditto" in result.stderr, detail(result)
    assert not (project / SESSIONS / "bilibili" / "profile").exists()
    assert not (project / SESSIONS / "bilibili" / SNAP).exists()
    assert not (project / SESSIONS / "weibo").exists()
    assert tree_state(project / LEGACY) == before


def test_final_profile_mv_failure_stops(project: Path) -> None:
    before = tree_state(project / LEGACY)
    result = run(project, "step2", STUB_MV_FAIL_MATCH="/profile.partial")
    assert result.returncode != 0, detail(result)
    assert "FAILED: bilibili: mv" in result.stderr, detail(result)
    bilibili = project / SESSIONS / "bilibili"
    assert not (bilibili / "profile").exists()
    assert (bilibili / "profile.partial").is_dir()
    assert (bilibili / SNAP).is_file()
    assert not (project / SESSIONS / "weibo").exists()
    assert tree_state(project / LEGACY) == before


# ---------- 正常路径与第 3 段 ----------

def test_normal_migration_layout_and_verification(project: Path, root_mode: int) -> None:
    before = tree_state(project / LEGACY)
    migrated(project)
    assert tree_state(project / LEGACY) == before
    for platform_key, code in (("bilibili", "bili"), ("weibo", "wb")):
        session = project / SESSIONS / platform_key
        assert stat.S_IMODE(session.stat().st_mode) == 0o700
        profile = session / "profile"
        assert stat.S_IMODE(profile.stat().st_mode) == root_mode
        assert (profile / "Default" / "Cookies").read_bytes() == b"cookie-db-" + code.encode()
        assert os.readlink(profile / "link") == "Default/Cookies"
        assert os.readlink(profile / "SingletonLock") == "fake-host-12345"
        assert stat.S_IMODE((profile / "Default" / "Sub" / "state").stat().st_mode) == 0o640
        assert not (profile / SNAP).exists()
        assert (session / SNAP).read_bytes() == (project / LEGACY / f"{code}_user_data_dir" / SNAP).read_bytes()
        assert stat.S_IMODE((session / SNAP).stat().st_mode) == 0o600
    assert (project / SESSIONS / "douyin" / "cdp_profile" / "Default" / "Cookies").is_file()
    assert not (project / SESSIONS / "douyin" / "profile").exists()
    assert not (project / SESSIONS / "zhihu").exists()
    assert not [path for path in (project / SESSIONS).rglob("*") if path.name.endswith(".partial")]
    rerun = run(project, "step2")
    assert rerun.returncode == 0, detail(rerun)
    assert "target exists, not overwritten" in rerun.stdout, detail(rerun)

    verify = run(project, "step3")
    assert verify_status(verify) == 0, detail(verify)
    out = verify.stdout
    assert "problems=0" in out, detail(verify)
    assert out.count("snapshot: 一致") == 2 and "不一致" not in out, detail(verify)
    assert out.count("same_entry_set=yes entries=6 mismatched=0") == 3, detail(verify)
    assert out.count("root_mode_same=yes") == 3, detail(verify)
    expected_700 = "yes" if root_mode == 0o700 else "no"
    assert out.count(f"root_mode_700={expected_700}") == 3, detail(verify)
    assert "=no" not in out.replace("root_mode_700=no", ""), detail(verify)
    assert SNAP not in out and str(project) not in out, detail(verify)


def test_leftover_snapshot_copy_in_new_profile_is_reported(project: Path) -> None:
    migrated(project)
    shutil.copy2(project / LEGACY / "wb_user_data_dir" / SNAP, project / SESSIONS / "weibo" / "profile" / SNAP)
    verify = run(project, "step3")
    assert verify_status(verify) == 2, detail(verify)
    assert "weibo profile: same_entry_set=no" in verify.stdout, detail(verify)


def test_content_mismatch_exits_2(project: Path) -> None:
    migrated(project)
    changed = project / SESSIONS / "weibo" / "profile" / "Default" / "Sub" / "state"
    original = changed.stat()
    changed.write_text("other", encoding="utf-8")
    os.utime(changed, ns=(original.st_atime_ns, original.st_mtime_ns))
    verify = run(project, "step3")
    assert verify_status(verify) == 2, detail(verify)
    assert "weibo profile: same_entry_set=yes entries=6 mismatched=1" in verify.stdout, detail(verify)
    assert "problems=1" in verify.stdout, detail(verify)


@pytest.mark.parametrize(("flags", "message"), [
    ({"STUB_STAT_FAIL": "1"}, "FAILED: stat old root"),
    ({"STUB_STAT_FAIL_MATCH": "Default/Cookies"}, "FAILED: stat old entry"),
    ({"STUB_CMP_FAIL_MATCH": "Default/Cookies"}, "FAILED: cmp entry"),
    ({"STUB_SHASUM_FAIL": "1"}, "FAILED: hash old snapshot"),
], ids=["stat_root", "stat_entry", "cmp_error", "shasum"])
def test_verification_command_failures_exit_nonzero(project: Path, flags: dict[str, str], message: str) -> None:
    migrated(project)
    verify = run(project, "step3", **flags)
    assert verify_status(verify) not in (0, 2), detail(verify)
    assert message in verify.stderr, detail(verify)
    assert "problems=" not in verify.stdout, detail(verify)
