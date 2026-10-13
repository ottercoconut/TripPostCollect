"""小红书网络暂停用例的共用片段：可恢复的规则请求失败与调用方的暂停—重做顺序。"""

from __future__ import annotations

import asyncio

from trippostcollect.platforms.xhs.errors import (
    XHSNetworkRecoveryTimeout,
    XHSRequestFailed,
    is_recoverable_xhs_navigation_failure,
)


def offline(stage: str = "note_detail") -> XHSRequestFailed:
    """规则请求的 requestfailed：失败文本带可恢复的 net::ERR_* 标记。"""
    return XHSRequestFailed(stage, "net::ERR_INTERNET_DISCONNECTED")


async def retry_after_network_pause(crawler, operation, *, stage: str):
    """调用方（单篇重做、导航）共用的网络暂停顺序：可恢复失败经 `_pause_for_network_recovery` 退避后
    重做同一动作，成功写恢复事件；预算耗尽为 XHSNetworkRecoveryTimeout；其他失败写中止事件后上抛。"""
    state: dict[str, object] = {}
    while True:
        try:
            result = await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            recoverable = (isinstance(exc, XHSRequestFailed) and exc.recoverable) or (
                is_recoverable_xhs_navigation_failure(exc)
            )
            if not recoverable:
                await crawler._abort_network_recovery(stage=stage, state=state, error=exc)
                raise
            if await crawler._pause_for_network_recovery(exc, stage=stage, state=state):
                continue
            started_at = float(state.get("started_at") or crawler._popup_monotonic())
            raise XHSNetworkRecoveryTimeout(stage, crawler._popup_monotonic() - started_at) from exc
        await crawler._finish_network_recovery(stage=stage, state=state)
        return result
