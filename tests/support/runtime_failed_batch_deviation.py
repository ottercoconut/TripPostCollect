"""#133 有意偏离：运行级失败后本批不再判为完整，checkpoint 不越过失败页。

固化预期（``tests/fixtures/t14_legacy_expectations/T06``、``T07``）与 manifest 不改、不用根实现重新生成。
旧实现在候选处理中 ``mark_runtime_failed(resume_page=失败页)`` 后，平台仍照常 ``finish_batch``，
传入的下一页位置与 ``batch_complete=True`` 覆盖前写，固化预期因此钉住了缺陷：失败页 1 之后的
``adaptive_batch_completed`` 与随后的 ``adaptive_search_stopped`` 写成 ``resume_page=2``、``batch_complete=True``。

根侧比较对加载的预期做“钉住旧值 → 单向替换”：先断言受影响场景恰有一处
“运行级失败停止 → 运行级失败批次 → 停止”且后两条为完整批次、位置晚于首条停止事件，再把后两条的
``resume_page``/``resume_offset``/``resume_cursor`` 换成首条停止事件（``mark_runtime_failed`` 写出）的值，
``batch_complete`` 换成 ``False``；观察值（``source_page``、``raw_batch_count``、``source_has_more`` 等）不动。
其余场景断言不含运行级失败后的完整批次，原样比较。
"""

from __future__ import annotations

from typing import Any

RESUME_FIELDS = ("resume_page", "resume_offset", "resume_cursor")

# 受影响场景：T06 抖音作者阻断与图片限流，T07 知乎详情 429/401 与图片阻断。
DOUYIN_SCENARIOS = frozenset({"creator_blocked", "image_blocked"})
ZHIHU_SCENARIOS = frozenset({"search_detail_429", "search_detail_401", "image_block"})


def _is_runtime_failed(event: dict[str, Any], kind: str) -> bool:
    return event["type"] == kind and event["details"].get("stop_reason") == "runtime_failed"


def _runtime_failed_batches(events: list) -> list[int]:
    return [
        index for index, event in enumerate(events)
        if _is_runtime_failed(event, "adaptive_batch_completed") and event["details"].get("batch_complete") is True
    ]


def _keep_failed_page(expected: dict[str, Any], affected: bool) -> dict[str, Any]:
    events = expected["events"]
    indexes = _runtime_failed_batches(events)
    if not affected:
        assert indexes == [], indexes
        return expected
    assert len(indexes) == 1, indexes
    (batch,) = indexes
    assert batch >= 1 and batch + 1 < len(events), batch
    failed, stopped = events[batch - 1], events[batch + 1]
    assert _is_runtime_failed(failed, "adaptive_search_stopped"), failed
    assert failed["details"]["batch_complete"] is False, failed
    assert _is_runtime_failed(stopped, "adaptive_search_stopped"), stopped
    assert stopped["details"]["batch_complete"] is True, stopped
    position = {field: failed["details"][field] for field in RESUME_FIELDS}
    for index in (batch, batch + 1):
        details = events[index]["details"]
        # 旧值越过失败页：恢复页为失败页的下一页。
        assert details["resume_page"] == details["source_page"] + 1 == position["resume_page"] + 1, details
    result = list(events)
    for index in (batch, batch + 1):
        details = {**events[index]["details"], **position, "batch_complete": False}
        result[index] = {**events[index], "details": details}
    return {**expected, "events": result}


def douyin_drive(expected: dict[str, Any], scenario: str) -> dict[str, Any]:
    """T06 drive 预期：作者阻断、图片限流场景的运行级失败批次保持失败页、不完整。"""
    return _keep_failed_page(expected, scenario in DOUYIN_SCENARIOS)


def zhihu_drive(expected: dict[str, Any], scenario: str) -> dict[str, Any]:
    """T07 drive 预期：详情 429/401、图片阻断场景的运行级失败批次保持失败页、不完整。"""
    return _keep_failed_page(expected, scenario in ZHIHU_SCENARIOS)
