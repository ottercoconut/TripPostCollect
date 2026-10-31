"""#127 有意偏离：知乎、微博浏览器不再被强制 UA，浏览器外请求的 UA 取自浏览器自报的 navigator.userAgent。

固化预期（``tests/fixtures/t14_legacy_expectations/T05``、``T07``）与 manifest 不改、不用根实现重新生成。
根侧比较对加载的预期做“钉住旧值 → 单向替换”：先断言旧 UA 恰在预期位置、恰为写死或固定的旧值，
再换成新值；其余部分仍整体 ``==`` 比较。drive 的假 page 对 ``navigator.userAgent`` 返回
``BROWSER_USER_AGENT``，该求值不进入轨迹。

1. 微博（T05）：浏览器上下文 ``new_context`` 的 ``user_agent`` 由固定 iPhone Safari 改为 ``None``，视口由
   1920×1080 改为与 warmup 相同的 1280×900；m.weibo.cn 接口与图片请求头 ``User-Agent`` 由 iPhone Safari
   改为浏览器 UA。
2. 知乎（T07）：CDP 启动参数 ``user_agent`` 由写死的 Chrome/128 改为 ``None``；API 请求头与图片请求头
   ``user-agent`` 由 Chrome/128 改为浏览器 UA。CDP 路径复用持久 profile 的默认 context，视口不经 crawl 设置。
"""

from __future__ import annotations

from typing import Any

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
LEGACY_WEIBO_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
)
LEGACY_ZHIHU_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
LEGACY_VIEWPORT = {"width": 1920, "height": 1080}
PROFILE_VIEWPORT = {"width": 1280, "height": 900}


def _swap_header(kwargs: dict[str, Any], key: str, legacy: str) -> dict[str, Any]:
    headers = kwargs["headers"]
    assert headers[key] == legacy, headers
    return {**kwargs, "headers": {**headers, key: BROWSER_USER_AGENT}}


def weibo(expected: dict[str, Any]) -> dict[str, Any]:
    """1：T05 drive 预期中 new_context 恰一次、每条 request 都带旧 iPhone UA；全部换成新值。"""
    trace = []
    contexts = 0
    for row in expected["trace"]:
        if row[0] == "new_context":
            contexts += 1
            assert row[1] == {"user_agent": LEGACY_WEIBO_USER_AGENT, "viewport": LEGACY_VIEWPORT}, row
            row = ("new_context", {"user_agent": None, "viewport": PROFILE_VIEWPORT})
        elif row[0] == "request":
            row = (*row[:3], _swap_header(row[3], "User-Agent", LEGACY_WEIBO_USER_AGENT), *row[4:])
        trace.append(row)
    assert contexts == 1, contexts
    result = {**expected, "trace": trace}
    assert "iPhone" not in repr(result)
    return result


def zhihu(expected: dict[str, Any]) -> dict[str, Any]:
    """2：T07 预期中 launch 恰一次带 Chrome/128，request 与 image 行的 user-agent 都是 Chrome/128。"""
    trace = []
    launches = 0
    for row in expected["trace"]:
        if row[0] == "launch":
            launches += 1
            assert row[1]["user_agent"] == LEGACY_ZHIHU_USER_AGENT, row
            row = ("launch", {**row[1], "user_agent": None})
        elif row[0] in {"request", "image"}:
            row = (*row[:3], _swap_header(row[3], "user-agent", LEGACY_ZHIHU_USER_AGENT), *row[4:])
        trace.append(row)
    assert launches == 1, launches
    result = {**expected, "trace": trace}
    assert "Chrome/128" not in repr(result)
    return result


# 3. B站（T11 冻结执行器源码）：写死 UA 常量的重导出删除；两个图片薄包装改为显式转发会话 UA。
#    须在 #115 变换之后应用（#115 已去掉 cookie_header 形参）。
T11_REPLACEMENTS = (
    ("    BILIBILI_BROWSER_USER_AGENT as BILIBILI_BROWSER_USER_AGENT,\n", ""),
    (
        "    post_id: str,\n) -> RemoteImagePreview:\n"
        "    return _bilibili_client.fetch_bilibili_image_bytes(\n        source_url, post_id,\n",
        "    post_id: str,\n    *,\n    user_agent: str,\n) -> RemoteImagePreview:\n"
        "    return _bilibili_client.fetch_bilibili_image_bytes(\n        source_url, post_id, user_agent=user_agent,\n",
    ),
    (
        "    max_attempts: int = BILIBILI_IMAGE_MAX_ATTEMPTS,\n) -> list[ImageManifestEntry]:\n"
        "    return _bilibili_core.download_bilibili_record_images(\n"
        "        record, platform_data_root=platform_data_root,\n"
        "        fetcher=fetcher, sleep_fn=sleep_fn, log_fn=log_fn, max_attempts=max_attempts,\n",
        "    max_attempts: int = BILIBILI_IMAGE_MAX_ATTEMPTS,\n    user_agent: str = \"\",\n"
        ") -> list[ImageManifestEntry]:\n"
        "    return _bilibili_core.download_bilibili_record_images(\n"
        "        record, platform_data_root=platform_data_root,\n"
        "        fetcher=fetcher, sleep_fn=sleep_fn, log_fn=log_fn, max_attempts=max_attempts, user_agent=user_agent,\n",
    ),
)


def t11_source(text: str) -> str:
    """在 #115 之后的冻结执行器源码上做 #127 的同样替换；每处必须恰好出现一次。"""
    for old, new in T11_REPLACEMENTS:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    assert "BILIBILI_BROWSER_USER_AGENT" not in text
    return text
