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
    # 以下选择器定值依据 V1、V16、R2（issuecomment-6107337442、6106301408）。
    # 视口内带 data-note-id 的卡片：只读 ID、序号与几何，不读内容。
    "visible_cards": """() => {
        const height = window.innerHeight || document.documentElement.clientHeight || 0;
        return Array.from(document.querySelectorAll('section.note-item')).map((node) => {
            const rect = node.getBoundingClientRect();
            const index = Number(node.getAttribute('data-index'));
            const visible = Math.max(0, Math.min(rect.bottom, height) - Math.max(rect.top, 0));
            const cover = node.querySelector('a.cover');
            const coverRect = cover ? cover.getBoundingClientRect() : null;
            return {
                note_id: node.getAttribute('data-note-id') || '',
                data_index: Number.isFinite(index) ? index : -1,
                visible_ratio: rect.height > 0 ? visible / rect.height : 0,
                top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right,
                cover: coverRect ? {
                    x: coverRect.left + coverRect.width / 2,
                    y: coverRect.top + coverRect.height / 2,
                    width: coverRect.width, height: coverRect.height,
                } : null,
            };
        }).filter((card) => card.note_id && card.visible_ratio > 0);
    }""",
    "scroll_metrics": """() => {
        const root = document.scrollingElement || document.documentElement;
        return {
            scroll_y: Math.round(window.scrollY || root.scrollTop || 0),
            inner_height: window.innerHeight || 0,
            inner_width: window.innerWidth || 0,
            scroll_height: Math.round(root.scrollHeight || 0),
        };
    }""",
    "search_box": """() => {
        const box = document.querySelector('.search-area .input-box textarea.textarea');
        if (!box) return { present: false, visible: false };
        const rect = box.getBoundingClientRect();
        const style = window.getComputedStyle(box);
        return {
            present: true,
            visible: style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0,
            focused: document.activeElement === box,
            value_length: (box.value || '').length,
            x: rect.left + rect.width / 2, y: rect.top + rect.height / 2,
        };
    }""",
    "search_layout": """() => {
        const layout = document.querySelector('.ai-feeds-page .search-layout')
            || document.querySelector('.search-layout');
        if (!layout) return { visible: false };
        const rect = layout.getBoundingClientRect();
        return { visible: rect.width > 0 && rect.height > 0 };
    }""",
    # 筛选入口文字、面板可见性与按精确文字定位的选项；各组都有 .tags，不按位置或 class 取。
    "filter_state": """(label) => {
        const center = (node) => {
            if (!node) return null;
            const rect = node.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) return null;
            return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
        };
        const visible = (node) => {
            if (!node) return false;
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        };
        const entry = document.querySelector('.search-layout div.filter');
        const panel = document.querySelector('.filters-wrapper');
        const option = Array.from(
            document.querySelectorAll('.filters-wrapper .filters .tag-container .tags')
        ).find((node) => (node.innerText || node.textContent || '').trim() === label);
        return {
            entry_text: entry ? (entry.innerText || entry.textContent || '').trim() : '',
            entry: center(entry),
            panel_visible: visible(panel),
            option: visible(option) ? center(option) : null,
        };
    }""",
    "search_ready_counts": """() => ({
        card_count: document.querySelectorAll(
            "section.note-item, a[href*='/explore/'], a[href*='/discovery/item/']"
        ).length,
        profile_count: document.querySelectorAll("a[href*='/user/profile/']").length,
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


async def dom_visible_cards(page: Page) -> list[dict[str, Any]]:
    cards = await _dom(page, "visible_cards")
    if not isinstance(cards, list):
        return []
    return sorted((card for card in cards if isinstance(card, dict)), key=lambda card: card.get("data_index", -1))


async def dom_scroll_metrics(page: Page) -> dict[str, Any]:
    metrics = await _dom(page, "scroll_metrics")
    return metrics if isinstance(metrics, dict) else {}


async def dom_search_box(page: Page) -> dict[str, Any]:
    state = await _dom(page, "search_box")
    return state if isinstance(state, dict) else {}


async def dom_search_layout_visible(page: Page) -> bool:
    try:
        state = await _dom(page, "search_layout")
    except Exception:
        return False
    return bool(isinstance(state, dict) and state.get("visible"))


async def dom_filter_state(page: Page, label: str) -> dict[str, Any]:
    state = await _dom(page, "filter_state", label)
    return state if isinstance(state, dict) else {}


async def dom_search_ready_counts(page: Page) -> dict[str, Any]:
    counts = await _dom(page, "search_ready_counts")
    return counts if isinstance(counts, dict) else {}
