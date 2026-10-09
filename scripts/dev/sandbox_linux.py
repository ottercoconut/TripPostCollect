"""卡片闸门的 Linux 沙箱后端：bubblewrap（``bwrap``）+ seccomp。

与 ``sandbox_macos.py``（Seatbelt）并行，拒绝语义逐项对应：

| 约束 | macOS Seatbelt | Linux（本模块） |
|---|---|---|
| 禁 IP 网络，保留 AF_UNIX | ``deny network*`` + 放行 unix-socket，connect/bind 时 EPERM | 独立网络命名空间；seccomp 使 AF_INET/AF_INET6/AF_PACKET 套接字在构造时 EPERM；禁 io_uring |
| 禁启动浏览器与桌面打开器 | process-exec 按路径正则拒绝 | 名称匹配同一正则的可执行文件/目录及 xdg-open 等打开器被不可访问文件遮蔽，exec 得 EACCES |
| 只允许临时根与 /dev 写入 | ``file-write*`` require-not | 根目录只读绑定，仅临时根与 /dev 可写，写入得 EROFS |
| 禁读写本机浏览器用户数据 | Library/Application Support 下 Chrome 目录 | ``~/.config`` 下 Chrome/Chromium/Chrome for Testing 目录被 000 空目录遮蔽，得 EACCES |
| 禁读写项目平台登录资料 | checkout 内 ``PROFILE_STORES`` 禁读写 | checkout 内现存的 ``PROFILE_STORES`` 被 000 空目录遮蔽 |

子孙进程继承全部限制；父进程退出时沙箱随之结束。Ubuntu 23.10 起默认限制非特权用户命名空间，
需为 ``/usr/bin/bwrap`` 放行（AppArmor profile 含 ``userns,``），见 docs/testing.md。
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import platform
import re
import shutil
import struct


NAME = "bubblewrap"
BROWSER = re.compile(r"(?i).*(chrome|chromium|safari|firefox|webkit|msedge|MiniBrowser).*")
OPENERS = ("xdg-open", "sensible-browser", "x-www-browser", "gnome-www-browser", "www-browser")
SEARCH_ROOTS = ("/opt", "/snap/bin", "/usr/lib", "/usr/local/lib")
PROFILE_DIRS = (".config/google-chrome", ".config/google-chrome-beta", ".config/google-chrome-unstable",
                ".config/google-chrome-for-testing", ".config/chromium", ".cache/ms-playwright")
# 与 sandbox_macos.PROFILE_STORES 一致（测试守护）；fork 删除后旧 browser_data 备份可能仍在磁盘，继续禁读写。
PROFILE_STORES = ("data/runtime/platform_sessions", "tools/MediaCrawler/browser_data")

# ---------------------------------------------------------------- seccomp（经典 BPF）

_ARCH = {  # uname -m: (AUDIT_ARCH, __NR_socket, __NR_io_uring_setup, x32 位)
    "x86_64": (0xC000003E, 41, 425, 0x40000000),
    "aarch64": (0xC00000B7, 198, 425, None),
}
_LD_ABS_W, _JEQ_K, _JGE_K, _RET_K = 0x20, 0x15, 0x35, 0x06
# Linux 内核 ABI 的地址族取值；不能用 socket.AF_*，它随生成过滤器的宿主平台变化（macOS 的 AF_INET6 为 30）。
LINUX_AF_INET, LINUX_AF_INET6, LINUX_AF_PACKET = 2, 10, 17
LINUX_EPERM = 1
_ALLOW, _ERRNO = 0x7FFF0000, 0x00050000


def seccomp_program(machine=None):
    """纯函数：返回 struct sock_filter 序列（字节）。被拒调用返回 EPERM，其余放行。"""
    machine = machine or platform.machine()
    if machine not in _ARCH:
        raise RuntimeError(f"Linux 沙箱不支持的架构：{machine}")
    arch, nr_socket, nr_uring, x32 = _ARCH[machine]
    deny = _ERRNO | LINUX_EPERM
    families = (LINUX_AF_INET, LINUX_AF_INET6, LINUX_AF_PACKET)
    program = [(_LD_ABS_W, 0, 0, 4)]                       # A = seccomp_data.arch
    program.append((_JEQ_K, 1, 0, arch))                    # 非本机架构（如 i386 兼容调用）一律拒绝
    program.append((_RET_K, 0, 0, deny))
    program.append((_LD_ABS_W, 0, 0, 0))                    # A = seccomp_data.nr
    if x32 is not None:
        program.append((_JGE_K, 0, 1, x32))                 # x32 ABI 调用一律拒绝
        program.append((_RET_K, 0, 0, deny))
    program.append((_JEQ_K, 0, 1, nr_uring))                # io_uring 可绕过 socket 过滤
    program.append((_RET_K, 0, 0, deny))
    program.append((_JEQ_K, 0, len(families) + 1, nr_socket))  # 非 socket 调用跳到放行
    program.append((_LD_ABS_W, 0, 0, 16))                   # A = args[0] 低 32 位（domain）
    for index, family in enumerate(families):
        program.append((_JEQ_K, len(families) - index, 0, family))
    program.append((_RET_K, 0, 0, _ALLOW))
    program.append((_RET_K, 0, 0, deny))
    return b"".join(struct.pack("HBBI", *instruction) for instruction in program)


# ---------------------------------------------------------------- 遮蔽目标

def browser_targets(path_env, roots=SEARCH_ROOTS):
    """列出需遮蔽的浏览器/打开器：PATH 中匹配项解析到真实路径，若位于名称匹配的目录则遮蔽该目录。"""
    found = set()
    directories = [Path(p) for p in path_env.split(os.pathsep) if p] + [Path("/usr/bin"), Path("/usr/local/bin")]
    for directory in directories:
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if BROWSER.match(entry.name) or entry.name in OPENERS:
                try:
                    found.add(entry.resolve(strict=True))
                except OSError:
                    continue
    for root in map(Path, roots):
        if not root.is_dir():
            continue
        for depth in range(1, 4):
            for entry in root.glob("/".join(["*"] * depth)):
                if BROWSER.match(entry.name) and not entry.is_symlink():
                    found.add(entry.resolve())
    collapsed = set()
    for target in found:
        parents = [parent for parent in target.parents if BROWSER.match(parent.name)]
        collapsed.add(parents[-1] if parents else target)
    return sorted(path for path in collapsed if not any(other in path.parents for other in collapsed))


def profile_targets(home, checkouts=()):
    """本机浏览器用户数据，以及各 checkout 内现存的项目平台登录资料。

    局限：bwrap 只能遮蔽 prepare 时已存在的路径；之后才创建的登录资料目录不会被遮蔽读取，
    但其写入仍被只读根挡住（临时根除外），可以接受。
    """
    paths = [Path(home) / relative for relative in PROFILE_DIRS]
    paths += [Path(checkout) / relative for checkout in checkouts for relative in PROFILE_STORES]
    return [path for path in paths if path.exists()]


# ---------------------------------------------------------------- 接口（与 sandbox_macos 一致）

def preflight():
    if not shutil.which("bwrap"):
        raise RuntimeError("缺少 bubblewrap（bwrap）")
    seccomp_program()


def bwrap_arguments(temporary, masked, deny_dir, deny_file, seccomp_fd):
    """纯函数：根只读、仅临时根与 /dev 可写、独立网络命名空间，masked 为 (路径, 是否目录)。"""
    arguments = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                 "--bind", str(temporary), str(temporary), "--unshare-net", "--die-with-parent"]
    for path, is_directory in masked:
        arguments += ["--ro-bind", str(deny_dir if is_directory else deny_file), str(path)]
    return arguments + ["--seccomp", str(seccomp_fd), "--"]


def prepare(output, checkouts, home, path_env):
    """写出 seccomp 过滤器与遮蔽用的空目录/空文件；checkouts 写入由只读根统一覆盖，登录资料另行遮蔽。"""
    state = output / "sandbox-linux"
    state.mkdir()
    deny_dir, deny_file = state / "denied-dir", state / "denied-file"
    deny_dir.mkdir()
    deny_file.touch()
    os.chmod(deny_dir, 0)
    os.chmod(deny_file, 0)
    seccomp = state / "seccomp.bpf"
    seccomp.write_bytes(seccomp_program())
    masked = [(path, path.is_dir()) for path in [*browser_targets(path_env), *profile_targets(home, checkouts)]]
    return {"temporary": output, "masked": masked, "deny_dir": deny_dir, "deny_file": deny_file,
            "seccomp": seccomp}


def wrap(policy, command):
    """返回包装后的命令与需继承的 seccomp 描述符；调用者在子进程结束后关闭描述符。"""
    descriptor = os.open(policy["seccomp"], os.O_RDONLY)
    arguments = bwrap_arguments(policy["temporary"], policy["masked"], policy["deny_dir"],
                                policy["deny_file"], descriptor)
    return [*arguments, *command], (descriptor,)


def canary_spec(home, policy):
    """自检探针：被遮蔽的打开器/浏览器执行、本机 Chrome profile 读取均须被拒绝；只读根写入得 EROFS。"""
    executables = [str(path) for path, is_directory in policy["masked"]
                   if not is_directory and path.name in OPENERS][:1]
    executables += [str(path) for path, is_directory in policy["masked"]
                    if not is_directory and BROWSER.match(path.name)][:1]
    profile = Path(home) / ".config/google-chrome"
    return {"exec": executables, "profile": str(profile), "profile_exists": profile.exists(),
            "denied_errnos": [errno.EPERM, errno.EACCES, errno.EROFS]}
