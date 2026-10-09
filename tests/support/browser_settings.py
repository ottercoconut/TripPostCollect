"""浏览器运行时测试的显式配置；不依赖 fork 可变全局状态。"""

from trippostcollect.runtime.browser import CDPBrowserSettings

BROWSER_SETTINGS = CDPBrowserSettings(
    PLATFORM="xhs",
    CDP_CONNECT_EXISTING=False,
    CDP_DEBUG_PORT=9222,
    BROWSER_LAUNCH_TIMEOUT=60,
    CUSTOM_BROWSER_PATH="",
    SAVE_LOGIN_STATE=True,
    AUTO_CLOSE_BROWSER=True,
)
