"""过渡装载 fork 顶层包与 scripts 路径，仅供旧桥与对照测试使用。

T12 起新入口与五站装配都不再调用；旧桥（E 与 fork main.py/cmd_arg 及薄子类）和对照测试
需要显式补入 fork 与 scripts 路径时使用，不依赖 cwd。随旧桥在 T14 删除。
"""

import sys

from trippostcollect.core.paths import MEDIACRAWLER_DIR, SCRIPTS_ROOT


def install() -> None:
    for directory in (SCRIPTS_ROOT, MEDIACRAWLER_DIR):
        location = str(directory)
        if location not in sys.path:
            sys.path.insert(0, location)
