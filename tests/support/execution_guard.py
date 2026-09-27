"""已审阅测试的执行防误用；不是任意恶意代码的安全沙箱。"""

import errno
import json
import os
from pathlib import Path
import re
import subprocess
import sys


_installed = False
_browser = re.compile(r"chrome|chromium|safari|firefox|webkit|msedge|minibrowser", re.I)


def check_command(executable, arguments=()):
    name = os.fsdecode(executable)
    if _browser.search(name) or Path(name).name in {"open", "osascript"}:
        raise PermissionError(errno.EACCES, "测试执行守卫拒绝浏览器或桌面启动器")
    if Path(name).name in {"sh", "bash", "zsh", "dash"}:
        text = " ".join(map(os.fsdecode, arguments)) if not isinstance(arguments, str) else arguments
        if _browser.search(text) or re.search(r"\b(open|osascript)\b", text):
            raise PermissionError(errno.EACCES, "测试执行守卫拒绝 shell 浏览器启动")


def audit(event, args):
    if event in {"subprocess.Popen", "os.exec", "os.posix_spawn"}:
        check_command(args[0], args[1])
    elif event == "os.system":
        check_command("sh", [os.fsdecode(args[0])])


def install():
    global _installed
    if not _installed:
        sys.addaudithook(audit)
        _installed = True


def canary():
    observations = []
    # 不存在的目标：只有守卫给出 EACCES 才通过；漏拦截的 ENOENT 必须失败。
    for name in ("chromium", "open", "osascript"):
        target = f"/nonexistent-tpc-canary/{name}"
        for boundary, operation in (
            ("subprocess", lambda: subprocess.run([target], check=True)),
            ("exec", lambda: os.execv(target, [target])),
        ):
            try:
                operation()
            except PermissionError as exc:
                if exc.errno != errno.EACCES:
                    raise
                observations.append({"boundary": boundary, "target": name, "errno": exc.errno})
            else:
                raise RuntimeError("执行守卫 canary 未被拒绝")
    return observations


def pytest_configure(config):
    install()
    observations = canary()
    destination = os.environ.get("TPC_EXEC_GUARD_REPORT")
    if destination:
        Path(destination).write_text(json.dumps({"mechanism": "python-execution-guard",
                                                "observations": observations}, indent=2))
