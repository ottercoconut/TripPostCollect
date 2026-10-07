"""冻结文件的文件系统不可变标志；macOS 与 Linux 两套实现并行，按运行平台选用。

- macOS：BSD 用户不可变标志 ``uchg``（``os.chflags``/``st_flags``），文件所有者即可设置与解除。
- Linux：inode 不可变属性 ``chattr +i``（``FS_IOC_GETFLAGS``/``FS_IOC_SETFLAGS``）。读取不需要特权；
  设置与解除需要 ``CAP_LINUX_IMMUTABLE``，非 root 进程经 ``sudo -n chattr`` 完成，不可用时直接报错。

只依赖标准库，供冻结校验、测试副本与卡片闸门按文件路径装载；不导入业务包、不读写业务数据。
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys


# ---------------------------------------------------------------- macOS（chflags uchg）

def macos_is_immutable(path: Path) -> bool:
    return bool(Path(path).lstat().st_flags & stat.UF_IMMUTABLE)


def macos_set_immutable(path: Path) -> None:
    os.chflags(path, Path(path).lstat().st_flags | stat.UF_IMMUTABLE, follow_symlinks=False)


def macos_clear_immutable(path: Path) -> None:
    os.chflags(path, Path(path).lstat().st_flags & ~stat.UF_IMMUTABLE, follow_symlinks=False)


# ---------------------------------------------------------------- Linux（chattr +i）

LINUX_FS_IOC_GETFLAGS = 0x80086601
LINUX_FS_IMMUTABLE_FL = 0x00000010


def linux_is_immutable(path: Path) -> bool:
    import fcntl

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    try:
        raw = fcntl.ioctl(descriptor, LINUX_FS_IOC_GETFLAGS, struct.pack("i", 0))
    except OSError as exc:
        # 不支持 inode 属性的文件系统不可能带不可变标志。
        if exc.errno in (errno.ENOTTY, errno.EOPNOTSUPP, errno.EINVAL):
            return False
        raise
    finally:
        os.close(descriptor)
    return bool(struct.unpack("i", raw)[0] & LINUX_FS_IMMUTABLE_FL)


def _linux_chattr(flag: str, path: Path) -> None:
    command = ["chattr", flag, os.fspath(path)]
    if os.geteuid() != 0:
        command = ["sudo", "-n", *command]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise PermissionError(
            errno.EPERM,
            f"Linux 设置不可变属性需要 root 或免密 sudo（{' '.join(command)}）：{result.stderr.strip()}",
        )


def linux_set_immutable(path: Path) -> None:
    _linux_chattr("+i", path)


def linux_clear_immutable(path: Path) -> None:
    _linux_chattr("-i", path)


# ---------------------------------------------------------------- 按平台分派

def _backend(platform: str | None = None):
    platform = platform or sys.platform
    if platform == "darwin":
        return macos_is_immutable, macos_set_immutable, macos_clear_immutable
    if platform.startswith("linux"):
        return linux_is_immutable, linux_set_immutable, linux_clear_immutable
    raise RuntimeError(f"不支持的平台：{platform}")


def is_immutable(path: Path) -> bool:
    return _backend()[0](path)


def set_immutable(path: Path) -> None:
    _backend()[1](path)


def clear_immutable(path: Path) -> None:
    _backend()[2](path)
