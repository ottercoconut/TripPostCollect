"""#211 微博详情衔接。

正式搜索把每条未知候选交给 ``WeiboDetailPages.open_from_search``。
点时间链接、截获 ``statuses/show`` 与 ``sideUser`` 在下一段实现；这一段默认抛出未实现，
测试注入假实现。不得再从正式搜索回到 ``m.weibo.cn`` 详情。
"""

from __future__ import annotations

from typing import Any


class WeiboDetailNotImplemented(NotImplementedError):
    """桌面详情尚未接通。调用方不得把它当成候选级失败写入 seen。"""

    def __init__(self, mid: str) -> None:
        super().__init__(f"weibo desktop detail is not implemented yet: mid={mid}")
        self.mid = mid


class WeiboDetailPages:
    """与知乎 ``ZhihuDetailPages`` 同一用法：搜索页持有通道，按候选打开详情。"""

    def __init__(self, capture: Any, search_page: Any) -> None:
        self.capture = capture
        self.search_page = search_page

    async def open_from_search(self, candidate: Any) -> Any:
        mid = str(getattr(candidate, "mid", "") or "")
        raise WeiboDetailNotImplemented(mid)
