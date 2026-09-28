"""浏览器可执行文件定位；保留原执行器的查找顺序与判定。"""

import os
from pathlib import Path
import re


def discover_cdp_browser_path() -> str | None:
    for env_key in ("TRIPPOSTCOLLECT_CUSTOM_BROWSER_PATH", "CUSTOM_BROWSER_PATH"):
        value = os.environ.get(env_key)
        if value and Path(value).is_file():
            return value

    playwright_cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    cache_candidates = sorted(
        playwright_cache.glob(
            "chromium-*/chrome-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        ),
        key=lambda path: int(match.group(1)) if (match := re.search(r"chromium-(\d+)", str(path))) else -1,
        reverse=True,
    )
    for path in cache_candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)

    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta"),
        Path("/Applications/Google Chrome Dev.app/Contents/MacOS/Google Chrome Dev"),
        Path("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path("/Applications/Microsoft Edge Beta.app/Contents/MacOS/Microsoft Edge Beta"),
        Path("/Applications/Microsoft Edge Dev.app/Contents/MacOS/Microsoft Edge Dev"),
        Path("/Applications/Microsoft Edge Canary.app/Contents/MacOS/Microsoft Edge Canary"),
    ]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None
