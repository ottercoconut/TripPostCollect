"""访问门禁 content_visit_ok：逐条规则 R1–R7、全轮规则 G1–G3、必需阶段推导、跨轮账本并集与被引用账本。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from trippostcollect.application.visit_gate import (
    VISIT_GATED_PLATFORMS,
    VISIT_POLICIES,
    StageSpec,
    VisitPolicy,
    validate_content_visits,
    validate_summary_content_visits,
    xhs_required_stages,
)
from trippostcollect.artifacts.image_manifest import ImageManifestEntry, write_manifest_atomic
from trippostcollect.runtime.visit_ledger import VisitLedger, snapshot_ledger


T = "2026-10-11T00:00:00+00:00"
SIGNATURE = json.dumps([["filter_note_type", ["普通笔记"]], ["sort_type", ["general"]]], ensure_ascii=False)


def clock() -> str:
    return T


def image_bytes(note_id: str, index: int) -> bytes:
    return f"img-{note_id}-{index}".encode()


def asset_key(note_id: str, index: int) -> str:
    return f"xhs:path:/{note_id}/{index}"


class Run:
    """按真实时序手工写一份账本；序号由账本分配，行可以晚于序号写出。"""

    def __init__(self, path: Path, *, mode: str = "discovery") -> None:
        self.ledger = VisitLedger.create(path, platform="xhs", mode=mode, clock=clock)
        self.path = path
        self.session = "s-target"
        self.page_visits: dict[int, str] = {}
        self.feed: dict[str, str] = {}
        self.clicks: dict[str, str] = {}
        self.images: dict[str, list[str]] = {}
        self.creators: dict[str, str] = {}

    def seq(self) -> int:
        return self.ledger.next_seq()

    def action(self, kind: str, target: dict | None = None, *, from_visit: str | None = None,
               page_role: str = "primary") -> str:
        return self.ledger.append("action", seq=self.seq(), page_role=page_role, action=kind,
                                  target=target or {}, from_visit=from_visit)

    def visit(self, *, seq: int | None = None, stage: str, rule: str, source: str = "page_response",
              status: int | None = 200, subject_id: str | None = None, trigger: str | None = None,
              page_role: str = "primary", **extra) -> str:
        return self.ledger.append(
            "visit", seq=seq or self.seq(), page_role=page_role, at=T, stage=stage, source=source, rule=rule,
            status=status, subject_id=subject_id, trigger=trigger, recorded_at=T, **extra,
        )

    def search(self, page: int, items: list[tuple[str, str]], *, trigger: str | None = None,
               target: bool = True, has_more: bool = True, keyword_ok: bool = True,
               session: str | None = None) -> str:
        visit = self.visit(
            stage="search_results", rule="xhs.search_notes", trigger=trigger, route="/search_result_ai",
            query_keys=["keyword", "source"], search_session=session or (self.session if target else "s-enter"),
            filter_signature=SIGNATURE if target else "none", is_target=target, keyword_matches_target=keyword_ok,
            search_page=page, has_more=has_more, items=[{"id": item, "type": kind} for item, kind in items],
        )
        if target and (session or self.session) == self.session:
            self.page_visits[page] = visit
        return visit

    def pause(self, stage: str, continuity: str = "n/a") -> str:
        start = self.seq()
        return self.ledger.append("pause", seq=self.seq(), page_role="primary", stage=stage, start_seq=start,
                                  seconds=1.0, continuity=continuity)

    def enter(self, items: list[tuple[str, str]], *, has_more: bool = True, keyword_ok: bool = True,
              filter_label: str = "note_type") -> str:
        enter = self.action("press_enter")
        self.search(1, [("x1", "video")], trigger=enter, target=False)
        self.action("apply_filter", {"filter": "panel_open"})
        choose = self.action("apply_filter", {"filter": filter_label, "label": "图文"})
        first = self.search(1, items, trigger=choose, has_more=has_more, keyword_ok=keyword_ok)
        self.action("apply_filter", {"filter": "panel_close"})
        return first

    def behavior_stage(self) -> None:
        self.pause("search_page", "completed")
        self.action("scroll", {"phase": "behavior_stage"})
        self.action("scroll", {"phase": "to_top", "delta_y": -400})

    def image(self, note_id: str, index: int, trigger: str | None, *, attempt: int = 1,
              content: bytes | None = None) -> str:
        data = content if content is not None else image_bytes(note_id, index)
        visit = self.visit(stage="note_image", rule="xhs.note_image", subject_id=note_id, trigger=trigger,
                           route="/explore/<id>", asset_key=asset_key(note_id, index),
                           sha256=sha256(data).hexdigest(), bytes=len(data), attempt=attempt)
        self.images.setdefault(note_id, []).append(visit)
        return visit

    def note(self, note_id: str, user_id: str, *, images: int = 2, first_page_note: bool = True,
             author_cached: bool = False, early_image: bool = True, detail_pause: bool = True,
             author_pause: bool = True, line_out_of_order: bool = True, trigger_kind: str = "click_card") -> None:
        if first_page_note:
            self.pause("search_results", "completed")
        click = self.action(trigger_kind, {"note_id": note_id, "data_index": 0})
        self.clicks[note_id] = click
        self.images[note_id] = []
        if early_image:
            self.image(note_id, 0, click)
        feed_seq = self.seq()
        if line_out_of_order and detail_pause:
            # feed 先到、访问行晚写：停顿行在文件中先于 feed 访问行出现。
            self.pause("note_detail")
            self.feed[note_id] = self.visit(seq=feed_seq, stage="note_detail", rule="xhs.feed",
                                            subject_id=note_id, trigger=click, route="/explore/<id>")
        else:
            self.feed[note_id] = self.visit(seq=feed_seq, stage="note_detail", rule="xhs.feed",
                                            subject_id=note_id, trigger=click, route="/explore/<id>")
            if detail_pause:
                self.pause("note_detail")
        for index in range(0 if not early_image else 1, images):
            if index:
                self.action("flip_image", {"note_id": note_id, "to": index})
            self.image(note_id, index, click)
        if not author_cached:
            if author_pause:
                self.pause("creator_profile")
            author = self.action("click_author", {"user_id": user_id}, from_visit=self.feed[note_id])
            self.creators[user_id] = self.visit(
                stage="creator_profile", rule="creator_page_data", source="page_state", status=None,
                subject_id=user_id, trigger=author, page_role="author_tab", route="/user/profile/<id>",
                url_user_id=user_id,
            )
            self.action("close_tab")
        self.action("close_overlay", {"note_id": note_id})

    def record(self, note_id: str, user_id: str, *, images: int = 2, search_page: int = 1) -> dict:
        return {
            "note_id": note_id,
            "user_id": user_id,
            "image_assets": [
                {"url": f"https://img.example.test/{note_id}/{i}", "source_index": i,
                 "source_asset_key": asset_key(note_id, i)}
                for i in range(images)
            ],
            "content_visit_refs": {
                "ledger_id": self.ledger.ledger_id,
                "search_results": self.page_visits[search_page],
                "note_detail": self.feed[note_id],
                "creator_profile": self.creators[user_id],
                "images": list(self.images[note_id]),
            },
        }


def manifest(path: Path, notes: dict[str, int], *, tamper: str | None = None) -> Path:
    entries = []
    for note_id, count in notes.items():
        for index in range(count):
            data = image_bytes(note_id, index) if note_id != tamper else b"other"
            entries.append(ImageManifestEntry(
                schema_version=1, platform_key="xhs", platform_post_id=note_id, image_role="content",
                source_index=index, source_key="image_list", source_asset_key=asset_key(note_id, index),
                source_url=f"https://img.example.test/{note_id}/{index}", fetch_status="downloaded", attempts=1,
                http_status=200, staging_path=f"images/{note_id}/{index}.png", size_bytes=len(data),
                mime_type="image/png", width=1, height=1, sha256=sha256(data).hexdigest(), error_code=None,
            ))
    write_manifest_atomic(path, entries)
    return path


def standard_run(tmp_path: Path) -> tuple[Run, list[dict], Path]:
    run = Run(tmp_path / "logs" / "behavior_evidence.content_visits.jsonl")
    run.enter([("n1", "normal"), ("v1", "video"), ("n2", "normal")])
    run.behavior_stage()
    run.note("n1", "u1")
    run.note("n2", "u1", first_page_note=False, author_cached=True)
    run.action("scroll", {"delta_y": 600})
    last = run.search(2, [], has_more=False)
    run.stop = {"stop_reason": "source_exhausted", "source_visit_id": last}
    records = [run.record("n1", "u1"), run.record("n2", "u1")]
    return run, records, manifest(tmp_path / "image_manifest.jsonl", {"n1": 2, "n2": 2})


def check(run: Run, records: list[dict], manifest_path: Path, *, paths=None, mode="discovery", stop=None):
    return validate_content_visits(
        records, paths if paths is not None else [run.path], platform="xhs", mode=mode,
        manifests=[manifest_path], stop_event=stop if stop is not None else getattr(run, "stop", None),
    )


def test_discovery_chain_passes_with_all_stages_and_out_of_order_lines(tmp_path):
    run, records, manifest_path = standard_run(tmp_path)
    result = check(run, records, manifest_path)
    assert result["content_visit_ok"] is True, result
    assert result["problem_counts"] == {} and result["sample_ids"] == []
    assert result["checked_records"] == 2 and result["ledger_count"] == 1 and result["checked_ledger_count"] == 1
    assert result["required_stages"] == ["creator_profile", "note_detail", "search_page", "search_results"]
    assert result["continuity_required_stages"] == ["search_page", "search_results"]
    assert result["request_pacing_ok"] is True and result["continuity_ok"] is True
    assert result["frontend_self_loads"] == 0 and result["images_from_earlier_attempt"] == 0
    # 输出只含编号、计数与 id，不含 URL。
    assert "://" not in json.dumps(result)


def test_missing_or_unresolvable_refs_fail_rule_1(tmp_path):
    run, records, manifest_path = standard_run(tmp_path)
    records[0].pop("content_visit_refs")
    records[1]["content_visit_refs"]["note_detail"] = f"{run.ledger.ledger_id}:v999999"
    result = check(run, records, manifest_path)
    assert result["content_visit_ok"] is False
    assert result["problem_counts"] == {"R1": 2}
    assert result["sample_ids"] == ["R1:n1", "R1:n2"]


def test_wrong_ledger_id_in_refs_fails_rule_1(tmp_path):
    run, records, manifest_path = standard_run(tmp_path)
    records[0]["content_visit_refs"]["ledger_id"] = "f" * 32
    assert check(run, records, manifest_path)["problem_counts"] == {"R1": 1}


def test_non_target_session_and_video_items_cannot_be_search_visits(tmp_path):
    run, records, manifest_path = standard_run(tmp_path)
    entry_visit = next(
        line["id"] for line in run.ledger.path.read_text().splitlines()
        for line in [json.loads(line)] if line.get("is_target") is False
    )
    records[0]["content_visit_refs"]["search_results"] = entry_visit
    result = check(run, records, manifest_path)
    assert result["problem_counts"]["R2"] == 1


def test_detail_must_be_clicked_after_search_and_match_subject(tmp_path):
    run = Run(tmp_path / "ledger.jsonl")
    run.enter([("n1", "normal")])
    run.behavior_stage()
    run.note("n1", "u1", trigger_kind="navigate")
    records = [run.record("n1", "u1")]
    result = check(run, records, manifest(tmp_path / "m.jsonl", {"n1": 2}), stop={})
    assert result["problem_counts"]["R3"] == 1


def test_detail_before_search_fails_rule_3(tmp_path):
    run = Run(tmp_path / "ledger.jsonl")
    run.enter([("n0", "normal")])
    run.behavior_stage()
    run.note("n1", "u1")
    run.action("scroll", {"delta_y": 400})
    late = run.search(2, [("n1", "normal")], has_more=False)
    record = run.record("n1", "u1", search_page=2)
    assert record["content_visit_refs"]["search_results"] == late
    result = check(run, [record], manifest(tmp_path / "m.jsonl", {"n1": 2}), stop={})
    assert result["problem_counts"]["R3"] == 1


def test_creator_mismatch_and_cache_reference(tmp_path):
    run, records, manifest_path = standard_run(tmp_path)
    # n2 引用 n1 首次打开作者页的访问（作者缓存）本身合法；作者 ID 不符则失败。
    assert records[1]["content_visit_refs"]["creator_profile"] == records[0]["content_visit_refs"]["creator_profile"]
    records[1]["user_id"] = "u2"
    result = check(run, records, manifest_path)
    assert result["problem_counts"] == {"R4": 1}


def test_image_hash_must_match_manifest(tmp_path):
    run, records, _ = standard_run(tmp_path)
    tampered = manifest(tmp_path / "tampered.jsonl", {"n1": 2, "n2": 2}, tamper="n2")
    assert check(run, records, tampered)["problem_counts"] == {"R5": 1}


def test_image_count_and_order_must_match_assets(tmp_path):
    run, records, manifest_path = standard_run(tmp_path)
    records[0]["content_visit_refs"]["images"].reverse()
    records[1]["content_visit_refs"]["images"].pop()
    assert check(run, records, manifest_path)["problem_counts"] == {"R5": 2}


def test_images_after_first_click_may_come_from_earlier_attempt(tmp_path):
    run = Run(tmp_path / "ledger.jsonl")
    run.enter([("n1", "normal")])
    run.behavior_stage()
    run.pause("search_results", "completed")
    first_click = run.action("click_card", {"note_id": "n1"})
    early = run.image("n1", 0, first_click)
    run.action("close_overlay", {"note_id": "n1"})
    run.note("n1", "u1", first_page_note=False, early_image=False, images=2)
    run.images["n1"] = [early, run.images["n1"][1]]
    record = run.record("n1", "u1")
    result = check(run, [record], manifest(tmp_path / "m.jsonl", {"n1": 2}), stop={})
    assert result["content_visit_ok"] is True, result
    assert result["images_from_earlier_attempt"] == 1


def test_image_before_first_click_fails(tmp_path):
    run = Run(tmp_path / "ledger.jsonl")
    run.enter([("n1", "normal")])
    run.behavior_stage()
    stray = run.image("n1", 0, None)
    run.note("n1", "u1", early_image=False)
    run.images["n1"][0] = stray
    record = run.record("n1", "u1")
    result = check(run, [record], manifest(tmp_path / "m.jsonl", {"n1": 2}), stop={})
    assert result["problem_counts"] == {"R5": 1}


@pytest.mark.parametrize(
    "options",
    [{"detail_pause": False}, {"author_pause": False}, {"first_page_note": False}],
    ids=["no_detail_pause", "no_author_pause", "no_search_results_pause"],
)
def test_pacing_order_is_proven_per_record(tmp_path, options):
    run = Run(tmp_path / "ledger.jsonl")
    run.enter([("n1", "normal")])
    run.behavior_stage()
    run.note("n1", "u1", **options)
    result = check(run, [run.record("n1", "u1")], manifest(tmp_path / "m.jsonl", {"n1": 2}), stop={})
    assert result["problem_counts"] == {"R6": 1}
    assert result["request_pacing_ok"] is False


def test_detail_pause_started_before_feed_does_not_count(tmp_path):
    run = Run(tmp_path / "ledger.jsonl")
    run.enter([("n1", "normal")])
    run.behavior_stage()
    run.pause("search_results", "completed")
    click = run.action("click_card", {"note_id": "n1"})
    start = run.seq()
    run.feed["n1"] = run.visit(stage="note_detail", rule="xhs.feed", subject_id="n1", trigger=click)
    run.ledger.append("pause", seq=run.seq(), page_role="primary", stage="note_detail", start_seq=start,
                      seconds=4.0, continuity="n/a")
    run.images["n1"] = [run.image("n1", 0, click)]
    run.action("flip_image")
    run.image("n1", 1, click)
    run.pause("creator_profile")
    author = run.action("click_author", from_visit=run.feed["n1"])
    run.creators["u1"] = run.visit(stage="creator_profile", rule="creator_page_data", source="page_state",
                                   status=None, subject_id="u1", trigger=author, url_user_id="u1")
    run.action("close_overlay")
    result = check(run, [run.record("n1", "u1")], manifest(tmp_path / "m.jsonl", {"n1": 2}), stop={})
    assert result["problem_counts"] == {"R6": 1}


def test_detail_and_image_visits_are_referenced_once(tmp_path):
    run, records, manifest_path = standard_run(tmp_path)
    records.append(json.loads(json.dumps(records[0])))
    result = check(run, records, manifest_path)
    assert result["problem_counts"] == {"R7": 3}


def test_zero_new_records_round_only_requires_observed_stages(tmp_path):
    run = Run(tmp_path / "ledger.jsonl")
    run.enter([("n1", "normal")])
    run.behavior_stage()
    run.action("scroll", {"delta_y": 600})
    last = run.search(2, [("n9", "normal")], has_more=False)
    result = check(run, [], tmp_path / "absent.jsonl",
                    stop={"stop_reason": "source_exhausted", "source_visit_id": last})
    assert result["content_visit_ok"] is True, result
    assert result["required_stages"] == ["search_page"]
    assert result["continuity_required_stages"] == ["search_page"]
    assert result["request_pacing_ok"] is True and result["continuity_ok"] is True


def test_frontend_self_load_is_diagnostic_only(tmp_path):
    run = Run(tmp_path / "ledger.jsonl")
    run.enter([("n1", "normal")])
    # 视口未填满时前端自行补页：第 1、2 页之间没有我们的滚动。
    last = run.search(2, [], has_more=False)
    result = check(run, [], tmp_path / "absent.jsonl",
                    stop={"stop_reason": "source_exhausted", "source_visit_id": last})
    assert result["content_visit_ok"] is True, result
    assert result["frontend_self_loads"] == 1
    assert result["required_stages"] == []


@pytest.mark.parametrize("pause_before_previous_page", [False, True])
def test_scroll_triggered_page_needs_page_pause_after_previous_page(tmp_path, pause_before_previous_page):
    run = Run(tmp_path / "ledger.jsonl")
    if pause_before_previous_page:
        run.pause("search_page", "completed")
    run.enter([("n1", "normal")])
    run.action("scroll", {"delta_y": 600})
    last = run.search(2, [], has_more=False)
    result = check(run, [], tmp_path / "absent.jsonl",
                    stop={"stop_reason": "source_exhausted", "source_visit_id": last})
    assert result["problem_counts"] == {"G3": 1}
    assert result["required_stages"] == ["search_page"]


@pytest.mark.parametrize("pause_after_redelivery", [False, True])
def test_redelivered_previous_page_needs_its_own_page_pause(tmp_path, pause_after_redelivery):
    """#95：第 2 页请求网络失败（失败请求不写访问）后，第 2 页送达两次：一次在网络暂停之前，一次由恢复后的重触发滚动
    带出。G3 从第 2 页最近一次送达算起：之后触发第 3 页的第一次滚动之前须另有翻页停顿。"""
    run = Run(tmp_path / "ledger.jsonl")
    run.enter([("n1", "normal")])
    run.pause("search_page", "completed")
    behavior = run.action("scroll", {"phase": "behavior_stage"})
    run.search(2, [("n2", "normal")], trigger=behavior)
    run.pause("search_page", "completed")
    retrigger = run.action("scroll", {"phase": "retrigger", "delta_y": 720})
    run.search(2, [("n2", "normal")], trigger=retrigger)
    if pause_after_redelivery:
        run.pause("search_page", "completed")
    backfill = run.action("scroll", {"phase": "backfill", "delta_y": -720})
    last = run.search(3, [], trigger=backfill, has_more=False)
    result = check(run, [], tmp_path / "absent.jsonl",
                   stop={"stop_reason": "source_exhausted", "source_visit_id": last})
    if pause_after_redelivery:
        assert result["content_visit_ok"] is True, result
        assert result["problem_counts"] == {}
    else:
        assert result["problem_counts"] == {"G3": 1}
        assert result["sample_ids"] == [f"G3:{last}"]
    assert result["frontend_self_loads"] == 0


def test_missing_previous_page_fails_page_rule(tmp_path):
    run = Run(tmp_path / "ledger.jsonl")
    run.enter([("n1", "normal")])
    run.behavior_stage()
    last = run.search(3, [], has_more=False)
    result = check(run, [], tmp_path / "absent.jsonl",
                   stop={"stop_reason": "source_exhausted", "source_visit_id": last})
    assert result["problem_counts"] == {"G2": 1, "G3": 1}


@pytest.mark.parametrize(
    "kwargs", [{"keyword_ok": False}, {"filter_label": "panel_close"}], ids=["keyword_mismatch", "wrong_trigger"]
)
def test_entry_rule_requires_target_filter_and_matching_keyword(tmp_path, kwargs):
    """没有记录时，来源耗尽停止事件引用的账本同样做 G1。"""
    run = Run(tmp_path / "ledger.jsonl")
    first = run.enter([("n1", "normal")], has_more=False, **kwargs)
    result = check(run, [], tmp_path / "absent.jsonl",
                   stop={"stop_reason": "source_exhausted", "source_visit_id": first})
    assert result["problem_counts"] == {"G1": 1}
    assert result["checked_ledger_count"] == 1


def test_exhaustion_rule_requires_target_has_more_false_and_contiguous_pages(tmp_path):
    run, records, manifest_path = standard_run(tmp_path)
    assert check(run, records, manifest_path, stop={"stop_reason": "source_exhausted"})["problem_counts"] == {
        "G2": 1
    }
    first = run.page_visits[1]
    result = check(run, records, manifest_path, stop={"stop_reason": "source_exhausted", "source_visit_id": first})
    assert result["problem_counts"] == {"G2": 1}
    gap = Run(tmp_path / "gap.jsonl")
    gap.enter([], has_more=True)
    gap.behavior_stage()
    gap.search(2, [])
    gap.pause("search_page", "completed")
    gap.action("scroll")
    last = gap.search(4, [], has_more=False)
    result = check(gap, [], tmp_path / "absent.jsonl",
                   stop={"stop_reason": "source_exhausted", "source_visit_id": last})
    assert result["problem_counts"] == {"G2": 1, "G3": 1}


def test_cross_round_ledgers_are_validated_as_a_union(tmp_path):
    prior = Run(tmp_path / "prior" / "ledger.jsonl")
    prior.enter([("n1", "normal")])
    prior.behavior_stage()
    prior.note("n1", "u1")
    prior_record = prior.record("n1", "u1")
    snapshot = snapshot_ledger(prior.path, 3)
    current, current_records, _ = standard_run(tmp_path / "current")
    records = [prior_record, current_records[1]]
    manifest_path = manifest(tmp_path / "m.jsonl", {"n1": 2, "n2": 2})
    result = check(current, records, manifest_path, paths=[snapshot, current.path])
    assert result["content_visit_ok"] is True, result
    assert result["ledger_count"] == 2 and result["checked_ledger_count"] == 2
    missing = check(current, records, manifest_path, paths=[current.path])
    assert missing["problem_counts"] == {"R1": 1}


def stalled_at_filter(path: Path) -> tuple[Run, str]:
    """停在筛选的失败轮：只有输入、回车与非目标第 1 页，没有目标会话，也没有记录。"""
    run = Run(path)
    run.action("type_search")
    enter = run.action("press_enter")
    return run, run.search(1, [("x1", "normal")], trigger=enter, target=False)


def test_carried_ledger_without_references_is_only_used_for_resolving(tmp_path):
    stalled, _ = stalled_at_filter(tmp_path / "round1" / "ledger.jsonl")
    current, records, manifest_path = standard_run(tmp_path / "round2")
    result = check(current, records, manifest_path, paths=[stalled.path, current.path])
    assert result["content_visit_ok"] is True, result
    assert result["ledger_count"] == 2 and result["checked_ledger_count"] == 1


def test_carried_ledger_referenced_by_a_record_is_checked(tmp_path):
    stalled, enter_visit = stalled_at_filter(tmp_path / "round1" / "ledger.jsonl")
    current, records, manifest_path = standard_run(tmp_path / "round2")
    records[0]["content_visit_refs"]["search_results"] = enter_visit
    result = check(current, records, manifest_path, paths=[stalled.path, current.path])
    assert result["problem_counts"]["G1"] == 1
    assert f"G1:{stalled.ledger.ledger_id}" in result["sample_ids"]
    assert result["checked_ledger_count"] == 2


def test_round_without_records_or_stop_reference_skips_ledger_rules(tmp_path):
    stalled, _ = stalled_at_filter(tmp_path / "ledger.jsonl")
    result = check(stalled, [], tmp_path / "absent.jsonl", stop={})
    assert result["problem_counts"] == {}
    assert result["ledger_count"] == 1 and result["checked_ledger_count"] == 0
    assert result["request_pacing_ok"] is False and result["continuity_ok"] is False


def test_snapshot_and_live_ledger_with_same_id_use_the_longer(tmp_path):
    run, records, manifest_path = standard_run(tmp_path)
    snapshot = snapshot_ledger(run.path, 1)
    run.action("scroll")
    result = check(run, records, manifest_path, paths=[snapshot, run.path])
    assert result["content_visit_ok"] is True and result["ledger_count"] == 1
    conflict = tmp_path / "conflict.jsonl"
    lines = run.path.read_text().splitlines()
    conflict.write_text("\n".join([lines[0], *lines[2:]]) + "\n")
    result = check(run, records, manifest_path, paths=[conflict, run.path])
    assert result["problem_counts"]["LEDGER"] == 1


def test_ledger_problems_fail_closed(tmp_path):
    run, records, manifest_path = standard_run(tmp_path)
    broken = tmp_path / "broken.jsonl"
    broken.write_text("{broken\n")
    assert check(run, records, manifest_path, paths=[run.path, broken])["problem_counts"] == {"LEDGER": 1}
    repair = Run(tmp_path / "repair.jsonl", mode="repair")
    assert check(run, records, manifest_path, paths=[run.path, repair.path])["problem_counts"] == {"LEDGER": 1}
    none = check(run, records, manifest_path, paths=[])
    assert none["content_visit_ok"] is False and none["request_pacing_ok"] is False
    assert none["problem_counts"]["LEDGER"] == 1


def repair_run(tmp_path: Path, *, trigger_kind: str = "navigate") -> tuple[Run, dict]:
    run = Run(tmp_path / "repair.jsonl", mode="repair")
    target = run.action(trigger_kind, {"note_id": "n1", "route": "/explore/<id>"})
    run.images["n1"] = [run.image("n1", 0, target)]
    run.feed["n1"] = run.visit(stage="note_detail", rule="note_detail_state", source="page_state", status=None,
                               subject_id="n1", trigger=target, route="/explore/<id>")
    run.pause("note_detail")
    run.action("flip_image")
    run.image("n1", 1, target)
    run.pause("creator_profile")
    author = run.action("click_author", from_visit=run.feed["n1"])
    run.creators["u1"] = run.visit(stage="creator_profile", rule="creator_page_data", source="page_state",
                                   status=None, subject_id="u1", trigger=author, url_user_id="u1")
    record = {
        "note_id": "n1", "user_id": "u1",
        "image_assets": [{"source_index": i, "source_asset_key": asset_key("n1", i)} for i in range(2)],
        "content_visit_refs": {
            "ledger_id": run.ledger.ledger_id, "note_detail": run.feed["n1"],
            "creator_profile": run.creators["u1"], "images": run.images["n1"],
        },
    }
    return run, record


def test_repair_mode_uses_navigation_and_page_state(tmp_path):
    run, record = repair_run(tmp_path)
    result = check(run, [record], manifest(tmp_path / "m.jsonl", {"n1": 2}), mode="repair")
    assert result["content_visit_ok"] is True, result
    assert result["required_stages"] == ["creator_profile", "note_detail"]
    assert result["continuity_required_stages"] == []
    clicked, record = repair_run(tmp_path / "clicked", trigger_kind="click_card")
    result = check(clicked, [record], manifest(tmp_path / "m2.jsonl", {"n1": 2}), mode="repair")
    assert result["problem_counts"]["R3"] == 1


def test_summary_helper_unions_ledgers_and_manifests(tmp_path):
    run, records, manifest_path = standard_run(tmp_path)
    summary = {"records": [
        {"platform": "xhs", "output": {"content_visit_paths": [str(run.path)]}},
        {"platform": "douyin", "output": {"content_visit_paths": ["/nonexistent"]}},
    ]}
    items = [{"platform": "xhs", "record": record, "manifest_paths": [str(manifest_path)]} for record in records]
    result = validate_summary_content_visits(summary, items, "xhs", repair_mode=False, stop_event=run.stop)
    assert result["content_visit_ok"] is True and result["checked_records"] == 2


def test_summary_carrying_failed_round_ledger_does_not_fail_entry_rule(tmp_path):
    """Mac 实测 2026-10-14：第 1 轮停在筛选的账本随累计摘要进入后续轮次，不再报 G1。"""
    stalled, _ = stalled_at_filter(tmp_path / "round1" / "ledger.jsonl")
    run, records, manifest_path = standard_run(tmp_path / "round2")
    summary = {"records": [
        {"platform": "xhs", "output": {"content_visit_paths": [str(stalled.path)]}},
        {"platform": "xhs", "output": {"content_visit_paths": [str(run.path)]}},
    ]}
    items = [{"platform": "xhs", "record": record, "manifest_paths": [str(manifest_path)]} for record in records]
    result = validate_summary_content_visits(summary, items, "xhs", repair_mode=False, stop_event=run.stop)
    assert result["content_visit_ok"] is True, result
    assert result["ledger_count"] == 2 and result["checked_ledger_count"] == 1


def test_policies_never_declare_playwright_requests():
    assert VISIT_GATED_PLATFORMS == frozenset({"xhs"}) <= set(VISIT_POLICIES)
    assert set(VISIT_POLICIES) == {"xhs"}
    with pytest.raises(ValueError):
        VisitPolicy(
            platform="demo", chains={"discovery": ("note_detail",)},
            stages={"discovery": {"note_detail": StageSpec("demo.detail", "page_request")}},
            target_filter="x", required_stages=xhs_required_stages,
        )
