"""#185 知乎详情页的页面状态脚本。

只返回页面里已经存在的 ``script#js-initialData`` 文本，不发请求、不改页面。
脚本正文只经 ``state_scripts=`` 交给页面取数通道；调用方只把脚本名传给 ``read_state``。
"""

from __future__ import annotations


INITIAL_DATA_SCRIPT_NAME = "zhihu.initial_data"

_INITIAL_DATA_SCRIPT = """() => {
  const node = document.querySelector("script#js-initialData");
  return node ? (node.textContent || "") : "";
}"""

STATE_SCRIPTS = {
    INITIAL_DATA_SCRIPT_NAME: _INITIAL_DATA_SCRIPT,
}
