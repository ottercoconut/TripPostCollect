"""B站 article 搜索替身：按页给出结果，并带上判断来源耗尽所需的 numPages/numResults。"""

from __future__ import annotations

from typing import Any, Callable

from trippostcollect.platforms.bilibili.models import BilibiliSearchPage


def search_page(items: list[dict[str, Any]], *, num_pages: int, num_results: int | None = None) -> BilibiliSearchPage:
    return BilibiliSearchPage(
        items=list(items),
        num_pages=num_pages,
        num_results=num_results if num_results is not None else num_pages * 20,
    )


def paged_search(
    pages: dict[int, list[dict[str, Any]]],
    *,
    requested: list[int] | None = None,
) -> Callable[..., BilibiliSearchPage]:
    """返回 fetch_bilibili_article_page 替身：最后一个有结果的页就是 numPages，越过它的页为空。"""

    last_page = max((page for page, items in pages.items() if items), default=0)

    def fetch(keyword: str, page: int, **kwargs: Any) -> BilibiliSearchPage:
        if requested is not None:
            requested.append(page)
        return search_page(
            pages.get(page, []),
            num_pages=last_page,
            num_results=sum(len(items) for items in pages.values()),
        )

    return fetch
