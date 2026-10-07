"""冻结文件不可变标志的 macOS/Linux 两套实现；只注入假系统调用，不改真实文件属性。"""

import errno
from pathlib import Path
import stat
import struct
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/ci"))
import frozen_flags  # noqa: E402


def test_dispatch_follows_platform(monkeypatch):
    assert frozen_flags._backend("darwin")[0] is frozen_flags.macos_is_immutable
    assert frozen_flags._backend("linux")[1] is frozen_flags.linux_set_immutable
    with pytest.raises(RuntimeError, match="不支持的平台"):
        frozen_flags._backend("win32")


def test_macos_uchg_set_and_clear_preserve_other_flags(monkeypatch, tmp_path):
    target = tmp_path / "frozen.md"
    target.write_text("x")
    flags = {"value": stat.UF_NODUMP}
    calls = []

    def fake_lstat(self):
        return SimpleNamespace(st_flags=flags["value"])

    def fake_chflags(path, value, follow_symlinks=True):
        calls.append(follow_symlinks)
        flags["value"] = value

    monkeypatch.setattr(frozen_flags.Path, "lstat", fake_lstat)
    monkeypatch.setattr(frozen_flags.os, "chflags", fake_chflags, raising=False)
    assert not frozen_flags.macos_is_immutable(target)
    frozen_flags.macos_set_immutable(target)
    assert flags["value"] == stat.UF_NODUMP | stat.UF_IMMUTABLE
    assert frozen_flags.macos_is_immutable(target)
    frozen_flags.macos_clear_immutable(target)
    assert flags["value"] == stat.UF_NODUMP
    assert calls == [False, False]


@pytest.mark.parametrize("raw_flags, expected", [(0x10, True), (0x80000, False), (0x80010, True)])
def test_linux_reads_immutable_attribute(monkeypatch, tmp_path, raw_flags, expected):
    import fcntl

    target = tmp_path / "frozen.md"
    target.write_text("x")

    def fake_ioctl(descriptor, request, argument):
        assert request == frozen_flags.LINUX_FS_IOC_GETFLAGS
        return struct.pack("i", raw_flags)

    monkeypatch.setattr(fcntl, "ioctl", fake_ioctl)
    assert frozen_flags.linux_is_immutable(target) is expected


def test_linux_unsupported_filesystem_is_not_immutable(monkeypatch, tmp_path):
    import fcntl

    target = tmp_path / "frozen.md"
    target.write_text("x")

    def fake_ioctl(*args):
        raise OSError(errno.ENOTTY, "not supported")

    monkeypatch.setattr(fcntl, "ioctl", fake_ioctl)
    assert frozen_flags.linux_is_immutable(target) is False


@pytest.mark.parametrize("euid, prefix", [(0, []), (1000, ["sudo", "-n"])])
def test_linux_chattr_uses_sudo_only_without_root(monkeypatch, tmp_path, euid, prefix):
    commands = []
    monkeypatch.setattr(frozen_flags.os, "geteuid", lambda: euid)
    monkeypatch.setattr(frozen_flags.subprocess, "run",
                        lambda command, **kwargs: commands.append(command) or SimpleNamespace(returncode=0))
    frozen_flags.linux_set_immutable(tmp_path / "a")
    frozen_flags.linux_clear_immutable(tmp_path / "a")
    assert commands == [[*prefix, "chattr", "+i", str(tmp_path / "a")],
                        [*prefix, "chattr", "-i", str(tmp_path / "a")]]


def test_linux_chattr_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(frozen_flags.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(frozen_flags.subprocess, "run",
                        lambda command, **kwargs: SimpleNamespace(returncode=1, stderr="a password is required"))
    with pytest.raises(PermissionError, match="免密 sudo"):
        frozen_flags.linux_set_immutable(tmp_path / "a")
