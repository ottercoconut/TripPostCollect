"""显式 TLS 输入的 HTTPX 窄工厂。"""

import httpx


def make_async_client(*, disable_ssl_verify: bool, **kwargs) -> httpx.AsyncClient:
    """每次构造接受调用方当前的 TLS 设置，显式 verify 优先。"""
    kwargs.setdefault("verify", not disable_ssl_verify)
    return httpx.AsyncClient(**kwargs)
