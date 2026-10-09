"""卡片闸门的 macOS 沙箱后端：Seatbelt（``/usr/bin/sandbox-exec``）。

与 ``sandbox_linux.py`` 并行，两者对外接口与拒绝语义一一对应：禁网络（AF_UNIX 除外）、禁启动浏览器与
桌面打开器、只允许临时根与 /dev 写入、禁读写本机 Chrome/Chrome for Testing 用户数据及项目内平台登录资料。
子孙进程继承策略。
"""

from __future__ import annotations

import errno
import json
from pathlib import Path


NAME = "Seatbelt"
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
# 项目内真实登录资料目录（相对 checkout），禁读写。T14-A 起非小红书 profile 位于 platform_sessions；
# T14-C 删除 fork 后，Mac 工作副本中旧 tools/MediaCrawler/browser_data 作为未跟踪备份仍留在磁盘，继续禁读写。
# sandbox_linux 保持同一清单（测试守护）。
PROFILE_STORES = ("data/runtime/platform_sessions", "tools/MediaCrawler/browser_data")


def preflight():
    if not SANDBOX_EXEC.is_file():
        raise RuntimeError("缺少 Seatbelt（/usr/bin/sandbox-exec）")


def sandbox_policy(temporary, checkouts, home):
    """纯函数：调用者传入规范路径，只允许临时根与设备目录写入。"""
    def quote(path):
        return json.dumps(str(path), ensure_ascii=False)

    rules = ["(version 1)", "(allow default)", "(deny network*)",
             "(allow network* (remote unix-socket))",
             '(deny process-exec (literal "/usr/bin/open") (literal "/usr/bin/osascript")',
             '  (regex #"(?i).*(chrome|chromium|safari|firefox|webkit|msedge|MiniBrowser).*"))']
    for relative in ("Google/Chrome", "Google/Chrome for Testing", "Google/ChromeForTesting", "Chromium"):
        path = Path(home) / "Library/Application Support" / relative
        rules.append(f"(deny file-read* file-write* (subpath {quote(path)}))")
    for checkout in sorted(set(map(Path, checkouts))):
        for relative in ("data", "outputs"):
            rules.append(f"(deny file-write* (subpath {quote(checkout / relative)}))")
        for relative in PROFILE_STORES:
            rules.append(f"(deny file-read* file-write* (subpath {quote(checkout / relative)}))")
    rules.extend([
        "(deny file-write* (require-all",
        f"  (require-not (subpath {quote(temporary)}))",
        '  (require-not (subpath "/dev"))))',
    ])
    return "\n".join(rules) + "\n"


def prepare(output, checkouts, home, path_env):
    """写出策略文件；返回值交给 wrap 使用。path_env 仅为与 Linux 后端接口一致。"""
    policy = output / "sandbox.sb"
    policy.write_text(sandbox_policy(output, checkouts, home), encoding="utf-8")
    return policy


def wrap(policy, command):
    """返回包装后的命令与需继承的文件描述符（Seatbelt 无需额外描述符）。"""
    return [str(SANDBOX_EXEC), "-f", str(policy), *command], ()


def canary_spec(home, policy):
    """自检探针：/usr/bin/open 执行、本机 Chrome profile 读取均须被拒绝。"""
    profile = Path(home) / "Library/Application Support/Google/Chrome"
    return {"exec": ["/usr/bin/open"], "profile": str(profile), "profile_exists": profile.exists(),
            "denied_errnos": [errno.EPERM, errno.EACCES]}
