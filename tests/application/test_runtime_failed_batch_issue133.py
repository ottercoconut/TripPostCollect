"""#133：运行级失败后本批不再判为完整，checkpoint 不越过失败页。

平台在候选处理中遇到运行级失败时先调用 ``mark_runtime_failed(resume_page=失败页)``，随后只跳出
候选循环，仍照常 ``finish_batch(resume_page=下一页, batch_complete=...)``。修正前后写覆盖前写，
失败的若是本页最后一个候选，停止事件就是 ``batch_complete=True``、``resume_page=下一页``，
``effective_discovery_checkpoint_event`` 优先采用该停止事件，checkpoint 因此越过失败页。
"""

from __future__ import annotations

import json

from trippostcollect.application.candidates import AdaptiveAccumulator
from trippostcollect.application.collection import (
    effective_discovery_checkpoint_event,
    load_pagination_evidence,
)


def recording_accumulator(platform: str = "weibo"):
    events: list[tuple[str, dict]] = []
    accumulator = AdaptiveAccumulator(
        platform,
        event_sink=lambda event_type, details: events.append((event_type, dict(details))),
    )
    return accumulator, events


def of(events, kind):
    return [details for event_type, details in events if event_type == kind]


def runtime_failed_on_last_candidate():
    accumulator, events = recording_accumulator()
    accumulator.begin_batch()
    accumulator.consider("p3-a", valid=True)
    accumulator.mark_runtime_failed(
        "login_required", source_page=3, resume_page=3, discovery_phase="frontier",
    )
    stopped = accumulator.finish_batch(
        source_page=3,
        source_has_more=True,
        raw_batch_count=2,
        resume_page=4,
        batch_complete=True,
        discovery_phase="frontier",
    )
    return accumulator, events, stopped


def test_runtime_failed_batch_keeps_failed_page_and_is_incomplete() -> None:
    accumulator, events, stopped = runtime_failed_on_last_candidate()

    assert stopped is True
    [batch] = of(events, "adaptive_batch_completed")
    assert batch["stop_reason"] == "runtime_failed"
    assert batch["batch_complete"] is False
    assert batch["resume_page"] == 3
    # 观察值照常记录本批实际看到的内容。
    assert batch["source_page"] == 3
    assert batch["raw_batch_count"] == 2
    assert batch["source_has_more"] is True

    final_stop = of(events, "adaptive_search_stopped")[-1]
    assert final_stop["stop_reason"] == "runtime_failed"
    assert final_stop["stop_detail"] == "login_required"
    assert final_stop["batch_complete"] is False
    assert final_stop["resume_page"] == 3
    assert final_stop["raw_batch_count"] == 2
    assert accumulator.summary()["resume_page"] == 3


def test_runtime_failed_batch_keeps_failed_offset_and_cursor() -> None:
    accumulator, events = recording_accumulator("douyin")
    accumulator.begin_batch()
    accumulator.mark_runtime_failed(
        "image_auth_required",
        source_page=2,
        source_offset=10,
        source_cursor="search-2",
        resume_page=2,
        resume_offset=10,
        resume_cursor="search-2",
    )
    accumulator.finish_batch(
        source_page=2,
        source_offset=10,
        source_cursor="search-2",
        next_cursor="search-3",
        resume_page=3,
        resume_offset=20,
        resume_cursor="search-3",
        batch_complete=True,
    )

    final_stop = of(events, "adaptive_search_stopped")[-1]
    assert (final_stop["resume_page"], final_stop["resume_offset"], final_stop["resume_cursor"]) == (
        2, 10, "search-2",
    )
    assert final_stop["next_cursor"] == "search-3"
    assert final_stop["batch_complete"] is False


def test_complete_batch_without_stop_reason_is_unchanged() -> None:
    accumulator, events = recording_accumulator()
    accumulator.begin_batch()
    accumulator.consider("p3-a", valid=True)

    assert accumulator.finish_batch(source_page=3, resume_page=4, batch_complete=True) is False

    [batch] = of(events, "adaptive_batch_completed")
    assert (batch["stop_reason"], batch["resume_page"], batch["batch_complete"]) == ("continue", 4, True)
    assert of(events, "adaptive_search_stopped") == []
    summary = accumulator.summary()
    assert (summary["resume_page"], summary["batch_complete"]) == (4, True)


def test_finish_batch_after_source_exhausted_keeps_caller_position() -> None:
    accumulator, events = recording_accumulator()
    accumulator.begin_batch()
    accumulator.mark_source_exhausted(
        "has_more_false", source_page=3, resume_page=3, source_has_more=False, batch_complete=True,
    )

    assert accumulator.finish_batch(source_page=3, resume_page=4, batch_complete=True) is True

    [batch] = of(events, "adaptive_batch_completed")
    assert (batch["stop_reason"], batch["resume_page"], batch["batch_complete"]) == (
        "source_exhausted", 4, True,
    )
    final_stop = of(events, "adaptive_search_stopped")[-1]
    assert (final_stop["resume_page"], final_stop["batch_complete"]) == (4, True)


def test_checkpoint_event_does_not_pass_failed_page(tmp_path) -> None:
    _, events, _ = runtime_failed_on_last_candidate()
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"events": [{"type": kind, "details": details} for kind, details in events]}),
        encoding="utf-8",
    )

    evidence = load_pagination_evidence(state_path)
    event = effective_discovery_checkpoint_event(evidence)

    assert evidence["stop_reason"] == "runtime_failed"
    assert event is not None
    assert event["stop_reason"] == "runtime_failed"
    assert event["resume_page"] == 3
    assert event["batch_complete"] is False
