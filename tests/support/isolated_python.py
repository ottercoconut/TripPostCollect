"""在隔离子进程中运行一段 Python：只带被测副本的 src，并拦截退出切片依赖的导入。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


# 退出切片依赖：根环境不安装，四站与入口导入链不得触及。
EXIT_SLICE_MODULES = ("redis", "sqlalchemy", "aiomysql", "motor", "jieba", "matplotlib", "wordcloud", "typer")


def child_environment() -> dict[str, str]:
    """只保留被测副本自己的 src，避免 lane 的 tests/scripts 路径或生产 checkout 掩盖入口装载缺陷。"""
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def run_python(code: str, *, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """在隔离子进程中运行，并拦截退出切片依赖的导入。"""
    blocker = (
        "import sys\n"
        f"_BLOCKED = {EXIT_SLICE_MODULES!r}\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in _BLOCKED:\n"
        "            raise ImportError('exit-slice module imported: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
    )
    environment = child_environment()
    return subprocess.run(
        [sys.executable, "-P", "-c", blocker + code],
        cwd=cwd, env=environment, capture_output=True, text=True, timeout=timeout, check=False,
    )


def json_tail(result: subprocess.CompletedProcess[str]):
    assert result.returncode == 0, result.stderr[-4000:]
    return json.loads(result.stdout.strip().splitlines()[-1])
