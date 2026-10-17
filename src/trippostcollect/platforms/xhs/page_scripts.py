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

# 结果列表视图（Mac 人工测量 2026-10-13）：AI 对话布局的列表在 .search-layout 的某个祖先容器内滚动、window 不动，
# 对话栏关闭后整页滚动。listLayout 取 .ai-feeds-page 下的 .search-layout（退而任一个）；listView 从它（含自身）向上
# 找第一个 overflow-y 可滚动且确有可滚内容的元素作滚动容器，到文档根为止（body 只在根不是 visible 时才算自己
# 滚动），找不到为文档；view 是容器可见区（padding 盒）与视口的交集，文档时为整个视口；clip 求矩形与 view 的交集。
# 登记表里的脚本以 _LIST_VIEW_SLOT 占位、模块加载时替换（登记表须保持字面量字符串，见 tests/test_fetch_boundary.py）。
_LIST_VIEW_SLOT = "/*list_view*/"
_LIST_VIEW = """const listLayout = () => document.querySelector('.ai-feeds-page .search-layout')
            || document.querySelector('.search-layout');
        const listView = () => {
            const layout = listLayout();
            const html = document.documentElement;
            const root = document.scrollingElement || html;
            const scrolls = (node) => {
                if (node === document.body && window.getComputedStyle(html).overflowY === 'visible') return false;
                return ['auto', 'scroll', 'overlay'].includes(window.getComputedStyle(node).overflowY)
                    && node.scrollHeight > node.clientHeight + 1;
            };
            let container = null;
            for (let node = layout; node && node !== root && node !== html; node = node.parentElement) {
                if (scrolls(node)) {
                    container = node;
                    break;
                }
            }
            const intersect = (a, b) => {
                if (!a || !b) return null;
                const left = Math.max(a.left, b.left);
                const top = Math.max(a.top, b.top);
                const right = Math.min(a.right, b.right);
                const bottom = Math.min(a.bottom, b.bottom);
                return right > left && bottom > top ? { left, top, right, bottom } : null;
            };
            const viewport = { left: 0, top: 0, right: window.innerWidth || 0, bottom: window.innerHeight || 0 };
            let view = intersect(viewport, viewport);
            if (container) {
                const rect = container.getBoundingClientRect();
                const left = rect.left + container.clientLeft;
                const top = rect.top + container.clientTop;
                view = intersect(
                    { left, top, right: left + container.clientWidth, bottom: top + container.clientHeight }, viewport,
                );
            }
            return { layout, container, root, view, clip: (rect) => intersect(rect, view) };
        };"""
# 详情视图里的作者链接（Mac 人工取证 2026-10-14）：路径为 /user/profile/<作者 ID> 的链接。旧位置 div.author 下的
# a.name 隐藏在前，评论者的 a.name 也在 div.author 下，作者本人的评论同样指向作者主页，所以按作者 ID 过滤并排除
# 评论区（.comment-item 与 .comment-inner-container 来自取证的祖先链，另两个来自互动阶段的评论区选择器）。
# 以 _AUTHOR_LINKS_SLOT 占位、模块加载时替换，同 _LIST_VIEW。
_AUTHOR_LINKS_SLOT = "/*author_links*/"
_AUTHOR_LINKS = """const profileId = (node) => {
            const match = /^\\/user\\/profile\\/([^/]+)\\/?$/.exec(node.pathname || '');
            return match ? match[1] : '';
        };
        const authorLinks = (root, author) => (author ? Array.from(
            root.querySelectorAll('a[href*="/user/profile/"]')
        ).filter((node) => profileId(node) === author) : []);
        const inComments = (node) => Boolean(
            node.closest('.comment-item, .comment-inner-container, .comments-container, .comment-list')
        );"""
# 失败诊断共用的节点描述：tagOf 为 tag.class（class 内连续 16 个及以上字母数字的片段遮为 <id>），chain 为自身起
# 至多 6 层祖先的 tagOf。以 _CLASS_CHAIN_SLOT 占位、模块加载时替换，同 _LIST_VIEW。
_CLASS_CHAIN_SLOT = "/*class_chain*/"
_CLASS_CHAIN = """const tagOf = (node) => [
            node.tagName.toLowerCase(),
            ...Array.from(node.classList || []).map((name) => name.replace(/[A-Za-z0-9]{16,}/g, '<id>')),
        ].join('.');
        const chain = (node) => {
            const parts = [];
            for (let current = node; current && parts.length < 6; current = current.parentElement) {
                parts.push(tagOf(current));
            }
            return parts;
        };"""

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
    # 结果列表内（取不到 .search-layout 时为全文档）与列表可见区相交、带 data-note-id 的卡片：只读 ID、序号与几何，
    # 不读内容。visible_ratio 按卡片与 view 的交集计算；cover_hit 为封面中心点 elementFromPoint 落在这张卡片内
    # （被固定头部、对话栏或弹层遮住为假）。
    "visible_cards": """() => {
        /*list_view*/
        const { layout, clip } = listView();
        return Array.from((layout || document).querySelectorAll('section.note-item')).map((node) => {
            const rect = node.getBoundingClientRect();
            const index = Number(node.getAttribute('data-index'));
            const shown = clip(rect);
            const cover = node.querySelector('a.cover');
            const coverRect = cover ? cover.getBoundingClientRect() : null;
            const x = coverRect ? coverRect.left + coverRect.width / 2 : 0;
            const y = coverRect ? coverRect.top + coverRect.height / 2 : 0;
            const hit = shown && coverRect ? document.elementFromPoint(x, y) : null;
            return {
                note_id: node.getAttribute('data-note-id') || '',
                data_index: Number.isFinite(index) ? index : -1,
                visible_ratio: shown && rect.height > 0 ? (shown.bottom - shown.top) / rect.height : 0,
                top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right,
                cover: coverRect ? {
                    x, y, left: coverRect.left, top: coverRect.top, width: coverRect.width, height: coverRect.height,
                } : null,
                cover_hit: Boolean(hit && node.contains(hit)),
            };
        }).filter((card) => card.note_id && card.visible_ratio > 0);
    }""",
    # 未登记卡片停止前才调用：结果列表内（范围同 visible_cards）每个 ID 的卡片个数、data-index 原值、有无 a.cover、
    # 封面链接路径的第一段（只读 pathname，不含查询串）、卡片与首个子元素的 tag.class（见 _CLASS_CHAIN），以及
    # 卡片相对列表可见区左上角的位置；不含文字或地址。
    "card_diagnostic": """(ids) => {
        /*list_view*/
        /*class_chain*/
        const { layout, view } = listView();
        const cards = Array.from((layout || document).querySelectorAll('section.note-item'));
        const origin = view || { left: 0, top: 0 };
        return ids.map((id) => {
            const matches = cards.filter((node) => node.getAttribute('data-note-id') === id);
            const node = matches[0];
            if (!node) return { note_id: id, nodes: 0 };
            const cover = node.querySelector('a.cover');
            const first = node.firstElementChild;
            const rect = node.getBoundingClientRect();
            return {
                note_id: id,
                nodes: matches.length,
                data_index: node.getAttribute('data-index'),
                has_cover: Boolean(cover),
                cover_path: cover ? '/' + ((cover.pathname || '').split('/')[1] || '') : null,
                card: tagOf(node),
                first_child: first ? tagOf(first) : null,
                position: {
                    left: Math.round(rect.left - origin.left),
                    top: Math.round(rect.top - origin.top),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                },
            };
        });
    }""",
    # 列表的滚动位置：容器为元素时取它的 scrollTop / clientHeight / scrollHeight，为文档时取 window 与文档根。
    # view_rect 是列表可见区（容器可见区与视口的交集）；list_rect 是结果列表（.search-layout）与 view_rect 的交集，
    # 滚轮落点限定在其中（AI 对话布局右侧的对话栏可独立滚动，不能让滚轮落在上面），取不到时为 null；
    # scroll_container 只用于诊断。
    "scroll_metrics": """() => {
        /*list_view*/
        const { layout, container, root, view, clip } = listView();
        return {
            scroll_container: container ? 'element' : 'document',
            scroll_y: Math.round(container ? container.scrollTop : (window.scrollY || root.scrollTop || 0)),
            inner_height: container ? container.clientHeight : (window.innerHeight || 0),
            inner_width: container ? container.clientWidth : (window.innerWidth || 0),
            scroll_height: Math.round((container || root).scrollHeight || 0),
            view_rect: view,
            list_rect: layout ? clip(layout.getBoundingClientRect()) : null,
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
            left: rect.left, top: rect.top, width: rect.width, height: rect.height,
        };
    }""",
    "search_layout": """() => {
        /*list_view*/
        const layout = listLayout();
        if (!layout) return { visible: false };
        const rect = layout.getBoundingClientRect();
        return { visible: rect.width > 0 && rect.height > 0 };
    }""",
    # 筛选入口（Mac 人工侦察 2026-10-13）：两种已知布局——V16 在 .search-layout 内，AI 对话布局
    # （.ai-feeds-page.with-ai-chat）在右侧 .ai-chat-inner 内、面板挂在入口内部。全文档取第一个可见且直属
    # span 文字精确为“筛选”/“已筛选”的 div.filter（前面可能有隐藏副本）；入口文字与点击落点都只取这个 span。
    # 选项各组都有 .tags，不按位置或 class 取；每个标签有一份重叠的 aria-hidden 副本，排除后按精确文字取可见的。
    # 轮询用，保持精简；整页计数在 filter_diagnostic。
    "filter_state": """(label) => {
        const labels = ['筛选', '已筛选'];
        const textOf = (node) => (node.innerText || node.textContent || '').trim();
        const visible = (node) => {
            if (!node) return false;
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        };
        const center = (node) => {
            if (!visible(node)) return null;
            const rect = node.getBoundingClientRect();
            return {
                x: rect.left + rect.width / 2, y: rect.top + rect.height / 2,
                left: rect.left, top: rect.top, width: rect.width, height: rect.height,
            };
        };
        const labelOf = (node) => Array.from(node.querySelectorAll(':scope > span')).find(
            (span) => visible(span) && labels.includes(textOf(span))
        ) || null;
        const entry = Array.from(document.querySelectorAll('div.filter')).find(
            (node) => visible(node) && labelOf(node)
        );
        const span = entry ? labelOf(entry) : null;
        const option = Array.from(
            document.querySelectorAll('.filters-wrapper .filters .tag-container .tags')
        ).find((node) => !node.closest('[aria-hidden="true"]') && visible(node) && textOf(node) === label);
        return {
            entry_text: span ? textOf(span) : '',
            entry: center(span),
            panel_visible: visible(document.querySelector('.filters-wrapper')),
            option: center(option),
        };
    }""",
    # 筛选失败时才调用：整页计数、布尔与至多 2 条入口文字节点的祖先链（见 _CLASS_CHAIN），不含正文或地址。
    # entry_candidates 为全文档 div.filter 个数；panel_count、panel_visible_count 为 .filters-wrapper 的个数与其中
    # 可见的个数（filter_state 的 panel_visible 只看第一个）。
    "filter_diagnostic": """() => {
        const labels = ['筛选', '已筛选'];
        const visible = (node) => {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        };
        /*class_chain*/
        const candidates = Array.from(document.querySelectorAll('div.filter'));
        const panels = Array.from(document.querySelectorAll('.filters-wrapper'));
        const labelNodes = Array.from(document.querySelectorAll('body *')).filter(
            (node) => node.childElementCount === 0 && labels.includes((node.textContent || '').trim())
        );
        return {
            layout_count: document.querySelectorAll('.search-layout').length,
            entry_candidates: candidates.length,
            entry_visible: candidates.some(visible),
            panel_count: panels.length,
            panel_visible_count: panels.filter(visible).length,
            label_nodes: labelNodes.length,
            label_chains: labelNodes.slice(0, 2).map(chain),
            inner_width: window.innerWidth,
            inner_height: window.innerHeight,
            scroll_y: window.scrollY,
        };
    }""",
    # 笔记浮层（R2）：遮罩与正文是否可见、页码 k/N、关闭按钮、右箭头的元素框（带中心点 x、y）。
    # 笔记详情视图：浮层（div.note-detail-mask）或 repair 直开的整页；roots 依次尝试，取第一个可见的根。
    # author 为本篇作者 ID 时另给 author_links：根内该作者主页链接（见 _AUTHOR_LINKS）按文档顺序的元素框（不可见为
    # null）、是否在评论区、是否为有文字的 a.name；不返回文字或地址，选点在 Python 侧。
    "note_view_state": """({ roots, author }) => {
        const visible = (node) => {
            if (!node) return false;
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        };
        const center = (node) => {
            if (!visible(node)) return null;
            const rect = node.getBoundingClientRect();
            return {
                x: rect.left + rect.width / 2, y: rect.top + rect.height / 2,
                left: rect.left, top: rect.top, width: rect.width, height: rect.height,
            };
        };
        /*author_links*/
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
            author_links: authorLinks(mask, author).map((node) => ({
                point: center(node),
                comment: inComments(node),
                named: node.classList.contains('name') && (node.textContent || '').trim().length > 0,
            })),
        };
    }""",
    # 作者标签页没打开时才调用：详情视图（取法同 note_view_state）内作者名链接、作者主页链接、指向本篇作者的
    # 主页链接（见 _AUTHOR_LINKS）的个数与可见个数，前 4 个主页链接的可见性、target 属性与祖先链（见 _CLASS_CHAIN）；
    # 不含文字或地址。
    "author_link_diagnostic": """({ roots, author }) => {
        const visible = (node) => {
            if (!node) return false;
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        };
        /*class_chain*/
        /*author_links*/
        const mask = roots.map((selector) => document.querySelector(selector)).find((node) => visible(node));
        if (!mask) return { view_visible: false };
        const names = Array.from(mask.querySelectorAll('.author a.name'));
        const links = Array.from(mask.querySelectorAll('a[href*="/user/profile/"]'));
        const own = authorLinks(mask, author);
        return {
            view_visible: true,
            author_names: names.length,
            author_names_visible: names.filter(visible).length,
            profile_links: links.length,
            profile_links_visible: links.filter(visible).length,
            author_id_links: own.length,
            author_id_links_visible: own.filter(visible).length,
            profile_link_nodes: links.slice(0, 4).map((node) => ({
                visible: visible(node),
                target: node.hasAttribute('target') ? node.getAttribute('target').slice(0, 16) : null,
                chain: chain(node),
            })),
        };
    }""",
    # 点赞控件（浮层内）的按下状态：只读属性、类名与可见文本，不读页面状态。
    "like_control_state": """element => {
            const control = element.closest('button,[role="button"]') || element;
            const className = String(control.className || element.className || '');
            const activeDescendant = Boolean(
                control.querySelector('[class*="liked"], [class*="active"], [data-state="active"], [aria-pressed="true"]')
            );
            const useElement = control.querySelector('use');
            return {
                aria_pressed: control.getAttribute('aria-pressed') || '',
                aria_label: control.getAttribute('aria-label') || '',
                title: control.getAttribute('title') || '',
                data_state: control.getAttribute('data-state') || '',
                class_name: className.slice(0, 240),
                text: String(control.innerText || element.innerText || '').trim().slice(0, 120),
                icon_ref: useElement ? (useElement.getAttribute('href') || useElement.getAttribute('xlink:href') || '') : '',
                active_descendant: activeDescendant,
            };
        }""",
    # 只数结果列表内的卡片、笔记链接与作者链接（AI 对话布局的对话栏里也有笔记链接）；取不到列表时为全文档。
    "search_ready_counts": """() => {
        /*list_view*/
        const root = listLayout() || document;
        return {
            card_count: root.querySelectorAll(
                "section.note-item, a[href*='/explore/'], a[href*='/discovery/item/']"
            ).length,
            profile_count: root.querySelectorAll("a[href*='/user/profile/']").length,
        };
    }""",
}
DOM_SCRIPTS = {
    name: script.replace(_LIST_VIEW_SLOT, _LIST_VIEW)
    .replace(_CLASS_CHAIN_SLOT, _CLASS_CHAIN)
    .replace(_AUTHOR_LINKS_SLOT, _AUTHOR_LINKS)
    for name, script in DOM_SCRIPTS.items()
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


async def dom_card_diagnostic(page: Page, note_ids: list[str]) -> list[dict[str, Any]]:
    cards = await _dom(page, "card_diagnostic", list(note_ids))
    return [card for card in cards if isinstance(card, dict)] if isinstance(cards, list) else []


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


async def dom_filter_diagnostic(page: Page) -> dict[str, Any]:
    state = await _dom(page, "filter_diagnostic")
    return state if isinstance(state, dict) else {}


async def dom_note_view_state(page: Page, roots: tuple[str, ...], author: str = "") -> dict[str, Any]:
    state = await _dom(page, "note_view_state", {"roots": list(roots), "author": author})
    return state if isinstance(state, dict) else {"visible": False}


async def dom_author_link_diagnostic(page: Page, roots: tuple[str, ...], author: str) -> dict[str, Any]:
    state = await _dom(page, "author_link_diagnostic", {"roots": list(roots), "author": author})
    return state if isinstance(state, dict) else {}


async def dom_overlay_state(page: Page) -> dict[str, Any]:
    return await dom_note_view_state(page, XHS_OVERLAY_ROOTS)


async def dom_like_control_state(locator: Any) -> dict[str, Any]:
    """在点赞控件元素上执行：返回按下状态相关的属性与文本摘要。"""
    state = await _dom(locator, "like_control_state")
    return state if isinstance(state, dict) else {}


async def dom_search_ready_counts(page: Page) -> dict[str, Any]:
    counts = await _dom(page, "search_ready_counts")
    return counts if isinstance(counts, dict) else {}
