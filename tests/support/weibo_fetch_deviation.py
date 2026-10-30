"""#114 有意偏离：微博图片不再经第三方代理、不带 Cookie；修复回退删除 Playwright 请求接口（L3）。

固化预期生成之后，根实现有两处行为在机械层面改变（其余请求、事件和产物不变）：

1. 正文图片候选从“`i1.wp.com` 代理 → 新浪 `large` 直连”改为“新浪 `large` 直连 → 平台原始地址”，
   请求头从整份客户端头（含 Cookie、Origin、Content-Type）改为只含 User-Agent、
   ``Referer: https://m.weibo.cn/`` 和图片 Accept；
2. 修复轮详情回退不再调用 ``page.request.get(statuses/show)``，只读精确 detail 页的页面状态。

固化文件与 manifest 不改、不用根实现重新生成。比较前对加载的预期做“钉住旧值 → 单向替换”：
先断言每条图片请求恰为旧代理或旧直连形态、旧请求头恰为旧键集合，再换成新形态；删除浏览器接口
行之前断言它恰是旧 ``statuses/show`` 请求。行为在语义上改变的场景（重试与阻断分类、登录复查等）
不经本模块，其固化文件已删除，改由根侧字面断言覆盖（见 ``tests/test_adapter_t05.py``）。
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlsplit

from support import legacy_expectations as expectations


LEGACY_IMAGE_PROXY = "https://i1.wp.com/"
LEGACY_IMAGE_HEADER_KEYS = frozenset({"Accept", "Content-Type", "Cookie", "Origin", "Referer", "User-Agent"})
IMAGE_REFERER = "https://m.weibo.cn/"
LEGACY_BROWSER_API = "https://m.weibo.cn/statuses/show?id="
LEGACY_REPAIR_ATTEMPTS = "attempts=['browser_api:http_200:empty_detail', 'page_state:empty_detail']"
REPAIR_ATTEMPTS = "attempts=['page_state:empty_detail']"


def _image_headers(old: dict[str, str]) -> dict[str, str]:
    assert set(old) == LEGACY_IMAGE_HEADER_KEYS, old
    return {"User-Agent": old["User-Agent"], "Referer": IMAGE_REFERER, "Accept": old["Accept"]}


def _image_url(url: str, original_url: Callable[[str], str]) -> str:
    if url.startswith(LEGACY_IMAGE_PROXY):
        direct = "https://" + url[len(LEGACY_IMAGE_PROXY):]
        assert urlsplit(direct).hostname.endswith(".sinaimg.cn") and "/large/" in direct, url
        return direct
    parts = urlsplit(url)
    assert parts.hostname.endswith(".sinaimg.cn") and parts.path.startswith("/large/"), url
    return original_url(url)


def _is_image(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host == "i1.wp.com" or host.endswith(".sinaimg.cn")


def drive(expected: dict[str, Any], *, original_url: Callable[[str], str]) -> dict[str, Any]:
    """T05 drive 预期：旧代理行换成新浪 large 直连，旧直连行换成平台原始地址；浏览器接口行删除。"""
    trace = []
    for row in expected["trace"]:
        if row[0] == "browser_api":
            assert row[1] == "GET" and row[2].startswith(LEGACY_BROWSER_API), row
            continue
        if row[0] == "request" and _is_image(row[2]):
            kwargs = {**row[3], "headers": _image_headers(row[3]["headers"])}
            row = ("request", row[1], _image_url(row[2], original_url), kwargs)
        trace.append(row)
    result = {**expected, "trace": trace}
    text = expectations.dumps(result)
    assert "i1.wp.com" not in text and "statuses/show" not in text
    return result


def repair_chain(expected: tuple[list, list]) -> tuple[list, list]:
    """单次修复请求预期：删除浏览器接口尝试行，并从首个异常的 attempts 中去掉对应条目。"""
    trace, errors = expected
    api_rows = [row for row in trace if row[0] == "api"]
    assert all(row[1].startswith(LEGACY_BROWSER_API) for row in api_rows), api_rows
    trace = [row for row in trace if row[0] != "api"]
    if errors and LEGACY_REPAIR_ATTEMPTS in errors[0][1]:
        assert errors[0][1].count(LEGACY_REPAIR_ATTEMPTS) == 1, errors[0]
        errors = [(errors[0][0], errors[0][1].replace(LEGACY_REPAIR_ATTEMPTS, REPAIR_ATTEMPTS), errors[0][2]),
                  *errors[1:]]
    return trace, errors
