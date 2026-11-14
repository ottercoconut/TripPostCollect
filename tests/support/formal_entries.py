"""正式入口清单与 `--help` 子进程：源码 checkout 与安装态共用。"""

from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
# 正式入口：scripts 下的脚本与 worker 模块入口。
FORMAL_ENTRIES = (
    "crawl_runner", "ctf_resource_crawl", "import_ctf_captures", "info_collection_benchmark", "login_warmup",
    "mediacrawler_crawl", "mediacrawler_login_warmup", "repair_bilibili_articles", "repair_post_details",
    "repair_xhs_posts", "trippostcollect.platforms.entry", "xhs_accounts", "xhs_runner",
)


def child_environment(extra_env: dict[str, str], tmp: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("TRIPPOSTCOLLECT_") and key not in {"PYTHONPATH", "TRIPPOST_PROJECT_ROOT"}}
    environment.update({
        key: value.replace("<TMP>", str(tmp)).replace("<ROOT>", str(ROOT)) for key, value in extra_env.items()
    })
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def help_command(name: str, python: str = sys.executable, root: Path = ROOT) -> list[str]:
    if name == "trippostcollect.platforms.entry":
        return [python, "-P", "-m", name, "--help"]
    return [python, str(root / "scripts" / f"{name}.py"), "--help"]


def help_environment(tmp: Path, workroot: Path) -> dict[str, str]:
    environment = child_environment({}, tmp)
    # 显式固定影响 argparse 排版的终端尺寸与区域设置，不依赖宿主终端。
    environment.update(COLUMNS="100", LINES="24", LC_ALL="C.UTF-8", TRIPPOST_PROJECT_ROOT=str(workroot))
    for name in ("PYTHON_COLORS", "FORCE_COLOR", "NO_COLOR", "TERM"):
        environment.pop(name, None)
    return environment
