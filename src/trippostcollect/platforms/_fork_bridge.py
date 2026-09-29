"""过渡装载未迁 fork 顶层包及旧导出 hook。

仅由 entry.configure 调用，显式补入 fork 与 scripts 路径，不依赖 cwd。
各站迁完且 E 删除后，在 T12/T14 删除此过渡机制。
"""

import sys

from trippostcollect.core.paths import MEDIACRAWLER_DIR, SCRIPTS_ROOT


def install() -> None:
    for directory in (SCRIPTS_ROOT, MEDIACRAWLER_DIR):
        location = str(directory)
        if location not in sys.path:
            sys.path.insert(0, location)
