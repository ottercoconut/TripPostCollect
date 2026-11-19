"""#211 微博页面脚本的唯一位置。

``DOM_SCRIPTS`` 只读搜索页 DOM；``STATE_SCRIPTS`` 是白名单投影，只能经 ``read_state`` 读取。
两类脚本都不得发请求、加载资源、跳转或改写页面。登录态只返回 uid 是否存在、是否为真，不返回原值。
"""

from __future__ import annotations

from typing import Any


DOM_SCRIPTS: dict[str, str] = {
    "weibo.search_cards": """() => {
  const cards = [];
  const nodes = document.querySelectorAll('div.card-wrap[action-type="feed_list_item"][mid]');
  for (const card of nodes) {
    const mid = card.getAttribute("mid") || "";
    let uid = "";
    let mblogid = "";
    const links = card.querySelectorAll("div.from a");
    for (const link of links) {
      const href = link.getAttribute("href") || "";
      const match = /^(?:https?:)?\\/\\/weibo\\.com\\/([^/?#]+)\\/([^/?#]+)(?:[/?#]|$)/i.exec(href);
      if (match) {
        uid = match[1];
        mblogid = match[2];
        break;
      }
    }
    cards.push({ mid: mid, uid: uid, mblogid: mblogid });
  }
  const pager = document.querySelector("div.m-page2");
  let currentText = "";
  let pageInfo = "";
  let nextDisabled = null;
  let maxPage = null;
  if (pager) {
    const current = pager.querySelector("li.cur");
    currentText = current ? (current.textContent || "").trim() : "";
    const info = pager.querySelector("span.page-info");
    pageInfo = info ? (info.textContent || "").trim() : "";
    const next = pager.querySelector("a.next");
    if (next) {
      const className = String(next.className || "");
      nextDisabled = /(^|\\s)disabled(\\s|$)/.test(className) || next.hasAttribute("disabled");
    }
    const numbers = [];
    const infoMatch = /共\\s*(\\d+)\\s*页/.exec(pageInfo);
    if (infoMatch) numbers.push(Number(infoMatch[1]));
    const labeled = pager.querySelectorAll("a, li");
    for (const node of labeled) {
      const text = (node.textContent || "").trim();
      if (/^\\d+$/.test(text)) numbers.push(Number(text));
    }
    if (numbers.length) maxPage = Math.max.apply(null, numbers);
  }
  const bodyText = document.body ? (document.body.innerText || "") : "";
  return {
    cards: cards,
    has_pager: Boolean(pager),
    current_text: currentText,
    page_info: pageInfo,
    next_disabled: nextDisabled,
    max_page: maxPage,
    no_results: bodyText.indexOf("找到0条结果") !== -1,
  };
}""",
    "weibo.at_bottom": """() => {
  const root = document.scrollingElement || document.documentElement;
  if (!root) return true;
  return root.scrollTop + root.clientHeight >= root.scrollHeight - 4;
}""",
}

STATE_SCRIPTS: dict[str, str] = {
    "weibo.config_uid": """() => {
  const config = window.$CONFIG;
  const exists = Boolean(config) && Object.prototype.hasOwnProperty.call(config, "uid");
  const value = exists ? config.uid : undefined;
  const truthy = (typeof value === "number" && value !== 0 && !Number.isNaN(value))
    || (typeof value === "string" && value.length > 0);
  return { exists: exists, truthy: truthy };
}""",
    "weibo.user_agent": "() => navigator.userAgent",
}


async def read_dom(page: Any, name: str) -> Any:
    """执行已登记的 DOM 脚本。调用必须留在本模块，页面取数通道不保存 DOM 脚本文本。"""

    script = DOM_SCRIPTS[name]
    return await page.evaluate(script)
