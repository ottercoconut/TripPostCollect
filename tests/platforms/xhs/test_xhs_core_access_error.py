# -*- coding: utf-8 -*-
"""TripPostCollect requires XHS access restrictions to stop the whole run."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from trippostcollect.platforms.xhs.errors import IPBlockError, PlatformRuntimeError

from .support import XiaoHongShuCrawler


def make_crawler(xhs_client):
    crawler = XiaoHongShuCrawler.__new__(XiaoHongShuCrawler)
    crawler.xhs_client = xhs_client
    crawler._guarded_pause = AsyncMock()
    return crawler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        IPBlockError("Network connection error"),
        PlatformRuntimeError("XHS request HTTP 403", code="login_required"),
    ],
)
async def test_note_detail_task_preserves_access_error(error):
    xhs_client = AsyncMock()
    xhs_client.get_note_by_id.side_effect = error
    xhs_client.get_note_by_id_from_html.side_effect = AssertionError(
        "被限流后不应再走 HTML 兜底"
    )

    with pytest.raises(type(error)):
        await make_crawler(xhs_client).get_note_detail_async_task(
            note_id="n1",
            xsec_source="pc_search",
            xsec_token="token",
            semaphore=asyncio.Semaphore(1),
        )


@pytest.mark.asyncio
async def test_gather_stops_on_single_blocked_note():
    xhs_client = AsyncMock()

    async def get_note_by_id(note_id, xsec_source, xsec_token):
        if note_id == "blocked":
            raise PlatformRuntimeError(
                "XHS platform security limit, code 300011",
                code="platform_security_limit_300011",
            )
        return {"note_id": note_id}

    xhs_client.get_note_by_id.side_effect = get_note_by_id
    crawler = make_crawler(xhs_client)

    semaphore = asyncio.Semaphore(2)
    with pytest.raises(PlatformRuntimeError) as exc_info:
        await asyncio.gather(
            *[
                crawler.get_note_detail_async_task(
                    note_id=note_id,
                    xsec_source="pc_search",
                    xsec_token="token",
                    semaphore=semaphore,
                )
                for note_id in ("ok1", "blocked", "ok2")
            ]
        )

    assert exc_info.value.code == "platform_security_limit_300011"

