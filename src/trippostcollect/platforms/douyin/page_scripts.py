"""#222 抖音页面脚本的唯一位置。

``DOM_SCRIPTS`` 只读可见性与几何；``STATE_SCRIPTS`` 是白名单投影，只能经 ``read_state`` 读取。
两类脚本都不得发请求、加载资源、跳转或改写页面。登录态只返回 ``HasUserLogin`` 是否为 ``"1"``，
不返回原值。
"""

from __future__ import annotations

from typing import Any


DOM_SCRIPTS: dict[str, str] = {
    # 隐藏的空验证码容器常驻 DOM。必须同时看尺寸、display、visibility、opacity 和是否在可视区。
    "douyin.visible_risk": """() => {
  const text = document.body ? (document.body.innerText || "") : "";
  const phrases = ["验证码", "请完成下列验证", "操作过于频繁", "请稍后再试", "访问太频繁", "请求过于频繁"];
  const hits = [];
  for (let i = 0; i < phrases.length; i += 1) {
    if (text.indexOf(phrases[i]) !== -1) hits.push(phrases[i]);
  }
  const host = String(location.host || "").toLowerCase();
  const verifyHost = host.indexOf("captcha") !== -1 || host.indexOf("verify") !== -1;
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 40 || rect.height < 40) return false;
    return rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth;
  };
  const captchaNodes = Array.from(document.querySelectorAll(
    '[class*="captcha"], [id*="captcha"], iframe[src*="captcha"], iframe[src*="verify"]'
  ));
  const loginNodes = Array.from(document.querySelectorAll(
    '#login-panel-new, [id*="login-panel"], [class*="login-mask"]'
  ));
  let hiddenCaptcha = 0;
  for (let i = 0; i < captchaNodes.length; i += 1) {
    if (!visible(captchaNodes[i])) hiddenCaptcha += 1;
  }
  return {
    hits: hits,
    verify_host: verifyHost,
    captcha_visible: captchaNodes.some(visible),
    login_panel_visible: loginNodes.some(visible),
    hidden_captcha_count: hiddenCaptcha,
  };
}""",
    # 左上角圆形关闭控件。侦察里 data-e2e 关闭选择器点不掉浮层，有效落点在视口左上角。
    # 这里返回元素本身，调用方用 evaluate_handle 点击，不写死坐标。
    "douyin.corner_close": """() => {
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width >= 24 && rect.height >= 24;
  };
  const nodes = document.querySelectorAll("button, [role='button'], div");
  let best = null;
  let bestScore = 1e9;
  for (let i = 0; i < nodes.length; i += 1) {
    const el = nodes[i];
    if (!visible(el)) continue;
    const rect = el.getBoundingClientRect();
    if (rect.left > 140 || rect.top > 140) continue;
    if (rect.width > 88 || rect.height > 88) continue;
    if (Math.abs(rect.width - rect.height) > 16) continue;
    const score = rect.left + rect.top;
    if (score < bestScore) {
      bestScore = score;
      best = el;
    }
  }
  return best;
}""",
}

STATE_SCRIPTS: dict[str, str] = {
    "douyin.login_state": """() => {
  const value = window.localStorage.getItem("HasUserLogin");
  return { logged_in: value === "1" };
}""",
    "douyin.user_agent": "() => navigator.userAgent",
    "douyin.modal_state": """() => {
  const params = new URLSearchParams(location.search || "");
  const modalId = params.get("modal_id") || "";
  const active = document.querySelector('[data-e2e="feed-active-video"]');
  const button = document.querySelector('[data-e2e="video-player-auto-play"] button');
  const className = button ? String(button.className || "") : "";
  const checked = /(^|\\s)xg-switch-checked(\\s|$)/.test(className);
  return { modal_id: modalId, has_active_video: Boolean(active), autoplay_on: checked };
}""",
}


async def read_dom(page: Any, name: str) -> Any:
    """执行已登记的 DOM 脚本。调用必须留在本模块。"""

    script = DOM_SCRIPTS[name]
    return await page.evaluate(script)


async def corner_close_handle(page: Any) -> Any:
    """左上角圆形关闭控件的元素句柄；没有符合几何条件的控件时返回 None。"""

    handle = await page.evaluate_handle(DOM_SCRIPTS["douyin.corner_close"])
    if handle is None:
        return None
    element = handle.as_element() if hasattr(handle, "as_element") else handle
    return element
