"""#185 知乎页面脚本的唯一位置。

详情和作者页都只读已经存在的 ``script#js-initialData`` 文本。浏览器 UA 也经状态脚本读取，
供正文图 CDN 直连使用。脚本不得发请求、不改页面。登记表的键必须是字面量；调用方只把脚本名
传给 ``read_state``。
"""

from __future__ import annotations


DOM_SCRIPTS: dict[str, str] = {}

STATE_SCRIPTS = {
    "zhihu.initial_data": """() => {
  const node = document.querySelector("script#js-initialData");
  return node ? (node.textContent || "") : "";
}""",
    "zhihu.user_agent": "() => navigator.userAgent",
}
