"""显式 TLS 输入的 HTTPX 窄工厂。"""

from http.cookiejar import DefaultCookiePolicy

import httpx


def make_async_client(*, disable_ssl_verify: bool, **kwargs) -> httpx.AsyncClient:
    """每次构造接受调用方当前的 TLS 设置，显式 verify 优先。"""
    kwargs.setdefault("verify", not disable_ssl_verify)
    return httpx.AsyncClient(**kwargs)


def make_reused_async_client(*, disable_ssl_verify: bool, **kwargs) -> httpx.AsyncClient:
    """构造跨请求复用的 client：Cookie 只来自每次请求显式传入的请求头。

    client 自带的 cookie jar 会收下响应的 Set-Cookie，再附到之后未带 Cookie 头的同域请求（如正文图片）上。
    复用时必须拒收，否则平台下发的 Cookie 会跨请求累积。
    """
    client = make_async_client(disable_ssl_verify=disable_ssl_verify, **kwargs)
    client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return client
