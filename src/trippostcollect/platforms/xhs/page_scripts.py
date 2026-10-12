"""小红书页面脚本的唯一位置。

`DOM_SCRIPTS` 只读 DOM 几何、可见文本与选择器状态，不读页面状态；`STATE_SCRIPTS` 是白名单投影，
只能登记给页面取数通道、经 `read_state` 读取并写访问。两类脚本都不得发起请求、加载资源或跳转，
由 tests/test_fetch_boundary.py 静态检查。
"""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import Page


DOM_SCRIPTS: dict[str, str] = {
    # 二维码只作为“已就绪”的布尔信号：可见且 src 非空；不读取、不保存图片内容。
    "qr_ready": """() => {
        const image = document.querySelector('img.qrcode-img');
        if (!image || !image.getAttribute('src')) return false;
        const style = window.getComputedStyle(image);
        const rect = image.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden'
            && rect.width > 0 && rect.height > 0;
    }""",
    "render_diagnostic": """() => ({
        ready_state: document.readyState || '',
        title: (document.title || '').slice(0, 160),
        dom_length: document.documentElement
            ? document.documentElement.outerHTML.length
            : 0,
        body_text_length: document.body
            ? (document.body.innerText || '').trim().length
            : 0,
        body_child_element_count: document.body
            ? document.body.childElementCount
            : 0,
        visibility_state: document.visibilityState || '',
    })""",
}

STATE_SCRIPTS: dict[str, str] = {}


async def _dom(page: Page, name: str, arg: Any = None, *, timeout: float = 5.0) -> Any:
    async with asyncio.timeout(timeout):
        if arg is None:
            return await page.evaluate(DOM_SCRIPTS[name])
        return await page.evaluate(DOM_SCRIPTS[name], arg)


async def dom_qr_ready(page: Page) -> bool:
    try:
        return bool(await _dom(page, "qr_ready"))
    except Exception:
        return False


async def dom_render_diagnostic(page: Page) -> dict[str, Any]:
    observed = await _dom(page, "render_diagnostic")
    return observed if isinstance(observed, dict) else {}
