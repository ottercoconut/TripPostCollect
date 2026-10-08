"""T14 运行手册第 2、3 段迁移/校验脚本的错误处理回归。

从 docs/operations-runbook.md 按标记提取脚本，在临时项目目录中用 bash 执行；ditto/cp/stat/find/shasum
由 PATH 前置的 stub 提供（Linux 没有 ditto，也没有 BSD `stat -f`），并可注入失败。不访问真实 profile。
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "operations-runbook.md"
SNAP = "trippostcollect_cookie_snapshot.json"
LEGACY = Path("tools/MediaCrawler/browser_data")
SESSIONS = Path("data/runtime/platform_sessions")

STUBS = {
    "ditto": """#!/bin/bash
[ -n "${STUB_DITTO_FAIL:-}" ] && exit 8
exec /bin/cp -a "$1" "$2"
""",
    "cp": """#!/bin/bash
[ -n "${STUB_CP_FAIL:-}" ] && exit 9
exec /bin/cp "$@"
""",
    "stat": """#!/bin/bash
[ -n "${STUB_STAT_FAIL:-}" ] && exit 1
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


@pytest.fixture
def project(tmp_path: Path) -> Path:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in STUBS.items():
        (stubs / name).write_text(body, encoding="utf-8")
        (stubs / name).chmod(0o755)
    root = tmp_path / "project"
    for code, files in (("bili", True), ("wb", True)):
        profile = root / LEGACY / f"{code}_user_data_dir"
        (profile / "Default" / "Sub").mkdir(parents=True)
        (profile / "Default" / "Cookies").write_bytes(b"cookie-db-" + code.encode())
        (profile / "Default" / "Sub" / "state").write_text("state", encoding="utf-8")
        (profile / "Default" / "Sub" / "state").chmod(0o640)
        (profile / "link").symlink_to("Default/Cookies")
        if files:
            (profile / SNAP).write_text('{"cookies": []}', encoding="utf-8")
            (profile / SNAP).chmod(0o600)
        os.utime(profile / "Default", (1_700_000_000, 1_700_000_000))
        profile.chmod(0o700)
        os.utime(profile, (1_700_000_100, 1_700_000_100))
    cdp = root / LEGACY / "cdp_dy_user_data_dir"
    (cdp / "Default").mkdir(parents=True)
    (cdp / "Default" / "Prefs").write_text("{}", encoding="utf-8")
    cdp.chmod(0o700)
    return root


def run(project: Path, step: str, **flags: str) -> subprocess.CompletedProcess[str]:
    script = project.parent / f"{step}.sh"
    script.write_text(runbook_script(step), encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("STUB_")}
    environment["PATH"] = f"{project.parent / 'stubs'}{os.pathsep}{environment.get('PATH', '')}"
    environment.update(flags)
    return subprocess.run(
        ["bash", str(script)], cwd=project, env=environment,
        capture_output=True, text=True, timeout=120, check=False,
    )


def snapshot_tree(root: Path) -> dict[str, tuple[int, bytes | str | None]]:
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if path.is_symlink():
            content: bytes | str | None = os.readlink(path)
        elif path.is_file():
            content = path.read_bytes()
        else:
            content = None
        result[str(path.relative_to(root))] = (stat.S_IMODE(info.st_mode), content)
    return result


def test_runbook_blocks_parse_with_bash(tmp_path: Path) -> None:
    for step in ("step2", "step3"):
        script = tmp_path / f"{step}.sh"
        script.write_text(runbook_script(step), encoding="utf-8")
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr


def test_existing_partial_stops_whole_script(project: Path) -> None:
    (project / SESSIONS / "bilibili" / "profile.partial").mkdir(parents=True)
    result = run(project, "step2")
    assert result.returncode != 0
    assert "FAILED: bilibili" in result.stderr
    assert not (project / SESSIONS / "bilibili" / "profile").exists()
    assert not (project / SESSIONS / "weibo").exists()
    assert not (project / SESSIONS / "douyin").exists()


def test_snapshot_copy_failure_does_not_land_profile(project: Path) -> None:
    result = run(project, "step2", STUB_CP_FAIL="1")
    assert result.returncode != 0
    assert "FAILED: bilibili snapshot: cp" in result.stderr
    bilibili = project / SESSIONS / "bilibili"
    assert not (bilibili / "profile").exists()
    assert not (bilibili / SNAP).exists()
    assert (bilibili / "profile.partial").is_dir()
    assert not (project / SESSIONS / "weibo").exists()
    # 中途失败的残留在重跑时被明确拒绝。
    rerun = run(project, "step2")
    assert rerun.returncode != 0 and "left by an earlier run" in rerun.stderr


def test_ditto_failure_stops_before_later_platforms(project: Path) -> None:
    result = run(project, "step2", STUB_DITTO_FAIL="1")
    assert result.returncode != 0
    assert "FAILED: bilibili profile: ditto" in result.stderr
    assert not (project / SESSIONS / "bilibili" / "profile").exists()
    assert not (project / SESSIONS / "bilibili" / SNAP).exists()
    assert not (project / SESSIONS / "weibo").exists()


def test_normal_migration_layout_and_verification(project: Path) -> None:
    before = snapshot_tree(project / LEGACY)
    result = run(project, "step2")
    assert result.returncode == 0, result.stderr
    assert snapshot_tree(project / LEGACY) == before
    for platform, code in (("bilibili", "bili"), ("weibo", "wb")):
        session = project / SESSIONS / platform
        assert stat.S_IMODE(session.stat().st_mode) == 0o700
        assert (session / "profile" / "Default" / "Cookies").read_bytes() == b"cookie-db-" + code.encode()
        assert os.readlink(session / "profile" / "link") == "Default/Cookies"
        assert not (session / "profile" / SNAP).exists()
        assert (session / SNAP).read_bytes() == (project / LEGACY / f"{code}_user_data_dir" / SNAP).read_bytes()
        assert stat.S_IMODE((session / SNAP).stat().st_mode) == 0o600
    assert (project / SESSIONS / "douyin" / "cdp_profile" / "Default" / "Prefs").is_file()
    assert not (project / SESSIONS / "douyin" / "profile").exists()
    assert not (project / SESSIONS / "zhihu").exists()
    assert not list((project / SESSIONS).rglob("*.partial"))
    rerun = run(project, "step2")
    assert rerun.returncode == 0, rerun.stderr
    assert "target exists, not overwritten" in rerun.stdout

    verify = run(project, "step3")
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert verify.stdout.count("snapshot: 一致") == 2 and "不一致" not in verify.stdout
    assert verify.stdout.count("same_entry_set=yes") == 3 and "mismatched=0" in verify.stdout
    assert verify.stdout.count("root_mode_700=yes") == 3 and "=no" not in verify.stdout
    assert SNAP not in verify.stdout and str(project) not in verify.stdout


def test_verification_failures_exit_nonzero(project: Path) -> None:
    assert run(project, "step2").returncode == 0
    broken = run(project, "step3", STUB_STAT_FAIL="1")
    assert broken.returncode not in (0, 2)
    assert "一致" not in broken.stdout
    changed = project / SESSIONS / "weibo" / "profile" / "Default" / "Sub" / "state"
    original = changed.stat()
    changed.write_text("other", encoding="utf-8")
    os.utime(changed, ns=(original.st_atime_ns, original.st_mtime_ns))
    mismatch = run(project, "step3")
    assert mismatch.returncode == 2
    assert "weibo profile: same_entry_set=yes entries=5 mismatched=1" in mismatch.stdout
