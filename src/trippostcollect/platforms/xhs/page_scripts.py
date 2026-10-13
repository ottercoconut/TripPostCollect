"""小红书页面脚本的唯一位置。

`DOM_SCRIPTS` 只读 DOM 几何、可见文本与选择器状态，不读页面状态；`STATE_SCRIPTS` 是白名单投影，
只能登记给页面取数通道、经 `read_state` 读取并写访问。两类脚本都不得发起请求、加载资源或跳转，
由 tests/test_fetch_boundary.py 静态检查。
"""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import Page


# 浮层只认遮罩；repair 直开的整页先认遮罩，再认整页容器（整页结构待 V9 实测）。
XHS_OVERLAY_ROOTS = ("div.note-detail-mask",)
XHS_NOTE_PAGE_ROOTS = ("div.note-detail-mask", "#noteContainer")

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
    # 笔记浮层（R2）：遮罩与正文是否可见、页码 k/N、关闭按钮、右箭头与作者名链接的中心点。
    # 笔记详情视图：浮层（div.note-detail-mask）或 repair 直开的整页；roots 依次尝试，取第一个可见的根。
    "note_view_state": """(roots) => {
        const visible = (node) => {
            if (!node) return false;
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        };
        const center = (node) => {
            if (!visible(node)) return null;
            const rect = node.getBoundingClientRect();
            return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
        };
        const mask = roots.map((selector) => document.querySelector(selector)).find((node) => visible(node));
        if (!mask) return { visible: false };
        const content = ['.note-content', '#detail-desc', '.author a.name']
            .some((selector) => visible(mask.querySelector(selector)));
        let pageIndex = null;
        let pageTotal = null;
        const nodes = mask.querySelectorAll('.fraction, span, div');
        for (let index = 0; index < nodes.length && index < 400; index += 1) {
            const node = nodes[index];
            if (node.childElementCount) continue;
            const match = /^\\s*(\\d+)\\s*\\/\\s*(\\d+)\\s*$/.exec(node.textContent || '');
            if (match && visible(node)) {
                pageIndex = Number(match[1]);
                pageTotal = Number(match[2]);
                break;
            }
        }
        return {
            visible: true,
            content_visible: content,
            page_index: pageIndex,
            page_total: pageTotal,
            close: center(document.querySelector('div.close-circle')),
            right_arrow: center(mask.querySelector('div.arrow-controller.right')),
            author: center(mask.querySelector('.author a.name')),
        };
    }""",
    "search_ready_counts": """() => ({
        card_count: document.querySelectorAll(
            "section.note-item, a[href*='/explore/'], a[href*='/discovery/item/']"
        ).length,
        profile_count: document.querySelectorAll("a[href*='/user/profile/']").length,
    })""",
}

STATE_SCRIPTS: dict[str, str] = {
    # 作者页白名单投影（经通道 read_state 原样使用这一份）：只读取页面已执行的
    # window.__INITIAL_STATE__.user.userPageData。先在该作者记录内有界收集头像证据 URL（键与路径来自
    # records.sanitization），再按白名单只复制标量字段，删除与证据 URL 完全相同的字符串；证据集合不返回。
    # 只有自有数据属性 __v_isRef === true 的节点才按 Vue ref 解包，其余对象的全部字段都参与证据扫描。
    # 计数类字段不投影布尔值。只读自有数据属性（不触发 getter 或 toJSON），检查做不完时拒绝整个投影。
    # 不序列化整份状态，不返回 HTML。
    "creator_page_data": """(spec) => {
    const INCOMPLETE = 'projection_avatar_check_incomplete';
    const avatarKeys = new Set(spec.avatar_keys);
    const avatarPaths = new Set(spec.avatar_paths.map((path) => JSON.stringify(path)));
    const isObject = (value) => value !== null && typeof value === 'object';
    const ownValue = (node, key) => {
        const descriptor = Object.getOwnPropertyDescriptor(node, key);
        if (descriptor === undefined) return undefined;
        if (!('value' in descriptor)) throw new Error(INCOMPLETE);
        return descriptor.value;
    };
    const numericFields = new Set(spec.numeric_fields);
    const stripChars = new Set(Array.from(spec.strip_chars));
    const pyStrip = (text) => {
        let start = 0;
        let end = text.length;
        while (start < end && stripChars.has(text[start])) start += 1;
        while (end > start && stripChars.has(text[end - 1])) end -= 1;
        return text.slice(start, end);
    };
    const isRef = (value) => {
        const marker = Object.getOwnPropertyDescriptor(value, '__v_isRef');
        return marker !== undefined && 'value' in marker && marker.value === true;
    };
    const unref = (value) => {
        let current = value;
        for (let depth = 0; depth < 4; depth += 1) {
            if (!isObject(current) || !isRef(current)) return current;
            const wrapped = Object.getOwnPropertyDescriptor(current, '_rawValue')
                || Object.getOwnPropertyDescriptor(current, '_value');
            if (wrapped === undefined || !('value' in wrapped)) throw new Error(INCOMPLETE);
            current = wrapped.value;
        }
        return current;
    };
    const isPlain = (value) => {
        if (!isObject(value) || Array.isArray(value)) return false;
        const prototype = Object.getPrototypeOf(value);
        return prototype === Object.prototype || prototype === null;
    };
    const isAvatarKey = (key) => avatarKeys.has(key.toLowerCase())
        || avatarKeys.has(key.toUpperCase().toLowerCase());
    const evidencedUrl = (value) => {
        if (typeof value !== 'string') return null;
        const candidate = pyStrip(value);
        if (!candidate || (candidate.length > 8192 && Array.from(candidate).length > 8192)) return null;
        const lowered = candidate.toLowerCase();
        let rest = null;
        if (lowered.startsWith('http://')) rest = candidate.slice(7);
        else if (lowered.startsWith('https://')) rest = candidate.slice(8);
        else if (lowered.startsWith('//')) rest = candidate.slice(2);
        if (rest === null || !rest.split(/[\\/?#]/, 1)[0]) return null;
        return candidate;
    };
    const evidence = new Set();
    const ancestors = [];
    let remaining = spec.max_nodes;
    const visit = (value, path, inAvatar, depth) => {
        remaining -= 1;
        if (remaining < 0 || depth > spec.max_depth) throw new Error(INCOMPLETE);
        const node = unref(value);
        if (inAvatar) {
            const url = evidencedUrl(node);
            if (url !== null) {
                evidence.add(url);
                return;
            }
        }
        if (node === null || node === undefined) return;
        const kind = typeof node;
        if (kind === 'string' || kind === 'number' || kind === 'boolean') return;
        if (kind !== 'object') throw new Error(INCOMPLETE);
        if (ancestors.includes(node)) throw new Error(INCOMPLETE);
        ancestors.push(node);
        try {
            if (Array.isArray(node)) {
                const length = ownValue(node, 'length');
                for (let index = 0; index < length; index += 1) {
                    visit(ownValue(node, String(index)), path, inAvatar, depth + 1);
                }
            } else if (node instanceof Set) {
                for (const item of node) visit(item, path, inAvatar, depth + 1);
            } else if (node instanceof Map) {
                for (const [key, item] of node) {
                    visit(key, path, inAvatar, depth + 1);
                    visit(item, path, inAvatar, depth + 1);
                }
            } else if (!(node instanceof Date)) {
                if (!isPlain(node)) throw new Error(INCOMPLETE);
                for (const key of Object.keys(node)) {
                    const nestedPath = path.concat([key]);
                    const nestedAvatar = inAvatar || isAvatarKey(key)
                        || avatarPaths.has(JSON.stringify(nestedPath));
                    visit(ownValue(node, key), nestedPath, nestedAvatar, depth + 1);
                }
            }
        } finally {
            ancestors.pop();
        }
    };
    const scalarFields = (source, fields) => {
        const result = {};
        for (const field of fields) {
            const value = ownValue(source, field);
            if (typeof value === 'string') {
                if (!evidence.has(pyStrip(value))) result[field] = value;
            } else if (typeof value === 'number' && Number.isFinite(value)) {
                result[field] = value;
            } else if (typeof value === 'boolean' && !numericFields.has(field)) {
                result[field] = value;
            }
        }
        return result;
    };
    try {
        const state = unref(ownValue(window, '__INITIAL_STATE__'));
        const user = isPlain(state) ? unref(ownValue(state, 'user')) : undefined;
        const data = isPlain(user) ? unref(ownValue(user, 'userPageData')) : undefined;
        if (!isPlain(data)) return { status: 'missing' };
        visit(data, [], false, 0);
        const creator = scalarFields(data, spec.top_fields);
        for (const [key, fields] of spec.object_fields) {
            const container = unref(ownValue(data, key));
            if (isPlain(container)) creator[key] = scalarFields(container, fields);
        }
        for (const [key, fields] of spec.list_fields) {
            const items = unref(ownValue(data, key));
            if (!Array.isArray(items)) continue;
            const rebuilt = [];
            const length = Math.min(ownValue(items, 'length'), spec.max_items);
            for (let index = 0; index < length; index += 1) {
                const item = unref(ownValue(items, String(index)));
                if (isPlain(item)) rebuilt.push(scalarFields(item, fields));
            }
            creator[key] = rebuilt;
        }
        return { status: 'ok', creator };
    } catch (error) {
        return { status: 'rejected', reason: INCOMPLETE };
    }
}""",
    # repair 直开笔记页的白名单投影（§9.3）：只读 window.__INITIAL_STATE__.note.noteDetailMap[<id>].note，
    # 按 spec 的字段表只复制标量（Vue ref 解包同作者投影）；头像、xsecToken、互动状态与图片流信息不在字段表内。
    # 只读自有数据属性，不序列化整份状态。
    "note_detail_state": """(spec) => {
    const isObject = (value) => value !== null && typeof value === 'object';
    const ownValue = (node, key) => {
        if (!isObject(node)) return undefined;
        const descriptor = Object.getOwnPropertyDescriptor(node, key);
        if (descriptor === undefined || !('value' in descriptor)) return undefined;
        return descriptor.value;
    };
    const isRef = (value) => {
        const marker = Object.getOwnPropertyDescriptor(value, '__v_isRef');
        return marker !== undefined && 'value' in marker && marker.value === true;
    };
    const unref = (value) => {
        let current = value;
        for (let depth = 0; depth < 4; depth += 1) {
            if (!isObject(current) || !isRef(current)) return current;
            const wrapped = Object.getOwnPropertyDescriptor(current, '_rawValue')
                || Object.getOwnPropertyDescriptor(current, '_value');
            if (wrapped === undefined || !('value' in wrapped)) return undefined;
            current = wrapped.value;
        }
        return current;
    };
    const field = (node, key) => unref(ownValue(unref(node), key));
    const isPlain = (value) => {
        if (!isObject(value) || Array.isArray(value)) return false;
        const prototype = Object.getPrototypeOf(value);
        return prototype === Object.prototype || prototype === null;
    };
    const scalars = (source, fields) => {
        const result = {};
        if (!isPlain(source)) return result;
        for (const key of fields) {
            const value = field(source, key);
            if (typeof value === 'string' || typeof value === 'boolean'
                || (typeof value === 'number' && Number.isFinite(value))) {
                result[key] = value;
            }
        }
        return result;
    };
    try {
        const detailMap = field(field(field(window, '__INITIAL_STATE__'), 'note'), 'noteDetailMap');
        const note = field(field(detailMap, spec.note_id), 'note');
        if (!isPlain(note)) return { status: 'missing' };
        const projected = scalars(note, spec.top_fields);
        for (const [key, fields] of spec.object_fields) {
            projected[key] = scalars(field(note, key), fields);
        }
        for (const [key, fields] of spec.list_fields) {
            const items = field(note, key);
            const rebuilt = [];
            if (Array.isArray(items)) {
                const length = Math.min(ownValue(items, 'length'), spec.max_items);
                for (let index = 0; index < length; index += 1) {
                    rebuilt.push(scalars(unref(ownValue(items, String(index))), fields));
                }
            }
            projected[key] = rebuilt;
        }
        return { status: 'ok', note: projected };
    } catch (error) {
        return { status: 'rejected' };
    }
}""",
}


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


async def dom_note_view_state(page: Page, roots: tuple[str, ...]) -> dict[str, Any]:
    state = await _dom(page, "note_view_state", list(roots))
    return state if isinstance(state, dict) else {"visible": False}


async def dom_overlay_state(page: Page) -> dict[str, Any]:
    return await dom_note_view_state(page, XHS_OVERLAY_ROOTS)


async def dom_search_ready_counts(page: Page) -> dict[str, Any]:
    counts = await _dom(page, "search_ready_counts")
    return counts if isinstance(counts, dict) else {}
