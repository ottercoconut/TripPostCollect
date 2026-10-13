"""真实访问门禁 `content_visit_ok`：每条正式记录都必须经访问账本追溯到真实的页面访问。

逐条规则（R1–R7）对本次入库的全部记录（含跨轮累计）生效；全轮规则（G1–G3）与必需节奏阶段
按每个账本各自判定。所有先后比较都用账本的单调序号 `seq`（截获为到达序号；停顿用 `start_seq`
判断“在某事件之后开始”、用 `seq` 判断“在某事件之前结束”），不看文件行序或写入时刻。

平台差异由 `VisitPolicy` 声明；本模块不导入平台实现，规则名与平台侧的捕获规则由测试核对一致。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from trippostcollect.artifacts.image_manifest import ImageManifestError, parse_manifest
from trippostcollect.runtime.visit_ledger import (
    LedgerFormatError,
    LoadedLedger,
    load_ledger,
    parse_line_id,
)


# 访问来源口径与正式契约一致：L1 为页面响应或页面状态；L2 为页面内主动请求；cdn_direct 是
# 浏览器外直连图片 CDN 的登记例外。Playwright `page.request` / `context.request`（L3）不可声明。
KNOWN_VISIT_SOURCES = frozenset({"page_response", "page_state", "page_fetch_l2", "cdn_direct"})
PACED_STAGES = ("search_results", "search_page", "note_detail", "creator_profile")
CONTINUITY_STAGES = frozenset({"search_results", "search_page"})
DETAIL_PAUSE_END_ACTIONS = frozenset({"flip_image", "click_author", "close_overlay", "click_card", "navigate"})
SAMPLE_LIMIT = 5


@dataclass(frozen=True)
class StageSpec:
    rule: str
    source: str
    trigger: str | None = None


@dataclass(frozen=True)
class RequiredStages:
    pacing: frozenset[str]
    continuity: frozenset[str]


@dataclass(frozen=True)
class VisitPolicy:
    """平台的访问链、各阶段允许的规则与来源、触发动作和必需节奏阶段推导。"""

    platform: str
    chains: Mapping[str, tuple[str, ...]]
    stages: Mapping[str, Mapping[str, StageSpec]]
    target_filter: str
    required_stages: Callable[[LedgerFacts, str], RequiredStages]
    record_id_field: str = "note_id"
    record_user_field: str = "user_id"

    def __post_init__(self) -> None:
        for mode, specs in self.stages.items():
            for stage, spec in specs.items():
                if spec.source not in KNOWN_VISIT_SOURCES:
                    raise ValueError(f"{self.platform}:{mode}:{stage} declares unknown source {spec.source}")

    def spec(self, mode: str, stage: str) -> StageSpec:
        return self.stages[mode][stage]


@dataclass(frozen=True)
class LedgerFacts:
    """供必需阶段推导使用的账本事实。"""

    detail_visits: int
    author_clicks: int
    scroll_triggered_pages: int


def xhs_required_stages(facts: LedgerFacts, mode: str) -> RequiredStages:
    pacing: set[str] = set()
    if mode == "discovery":
        if facts.scroll_triggered_pages:
            pacing.add("search_page")
        if facts.detail_visits:
            pacing.update({"search_results", "note_detail"})
    elif facts.detail_visits:
        pacing.add("note_detail")
    if facts.author_clicks:
        pacing.add("creator_profile")
    return RequiredStages(pacing=frozenset(pacing), continuity=frozenset(pacing & CONTINUITY_STAGES))


_XHS_SHARED = {
    "creator_profile": StageSpec("creator_page_data", "page_state", trigger="click_author"),
    "note_image": StageSpec("xhs.note_image", "page_response"),
}
VISIT_POLICIES: dict[str, VisitPolicy] = {
    "xhs": VisitPolicy(
        platform="xhs",
        chains={
            "discovery": ("search_results", "note_detail", "creator_profile", "images"),
            "repair": ("note_detail", "creator_profile", "images"),
        },
        stages={
            "discovery": {
                "search_results": StageSpec("xhs.search_notes", "page_response"),
                "note_detail": StageSpec("xhs.feed", "page_response", trigger="click_card"),
                **_XHS_SHARED,
            },
            "repair": {
                "note_detail": StageSpec("note_detail_state", "page_state", trigger="navigate"),
                **_XHS_SHARED,
            },
        },
        target_filter="note_type",
        required_stages=xhs_required_stages,
    ),
}
# 已纳入访问门禁的平台；平台在其取数改造完成的批次加入。
VISIT_GATED_PLATFORMS: frozenset[str] = frozenset({"xhs"})


def _seq(line: Mapping[str, Any]) -> int:
    return int(line["seq"])


class _LedgerIndex:
    def __init__(self, loaded: LoadedLedger) -> None:
        self.loaded = loaded
        self.ledger_id = loaded.ledger_id
        self.ordered = sorted(loaded.lines, key=_seq)
        self.by_id = {line["id"]: line for line in loaded.lines}
        self.actions = [line for line in self.ordered if line["kind"] == "action"]
        self.visits = [line for line in self.ordered if line["kind"] == "visit"]
        self.pauses = [line for line in self.ordered if line["kind"] == "pause"]
        self.signals = [line for line in self.ordered if line["kind"] == "signal"]

    def first_action(
        self,
        kinds: Iterable[str],
        *,
        after: int = 0,
        before: int | None = None,
        note_id: str | None = None,
        page_role: str | None = None,
    ) -> dict[str, Any] | None:
        wanted = set(kinds)
        for line in self.actions:
            seq = _seq(line)
            if seq <= after or (before is not None and seq >= before):
                continue
            if line["action"] not in wanted:
                continue
            if note_id is not None and str((line.get("target") or {}).get("note_id") or "") != note_id:
                continue
            if page_role is not None and line.get("page_role") != page_role:
                continue
            return line
        return None

    def last_action_before(self, kinds: Iterable[str], before: int) -> dict[str, Any] | None:
        wanted = set(kinds)
        found = None
        for line in self.actions:
            if _seq(line) >= before:
                break
            if line["action"] in wanted:
                found = line
        return found

    def has_pause(self, stage: str, *, after: int, before: int | None) -> bool:
        return any(
            line["stage"] == stage
            and int(line["start_seq"]) > after
            and (before is None or _seq(line) < before)
            for line in self.pauses
        )


@dataclass
class _Report:
    problems: Counter[str] = field(default_factory=Counter)
    samples: list[str] = field(default_factory=list)
    frontend_self_loads: int = 0
    images_from_earlier_attempt: int = 0

    def fail(self, rule: str, subject: str) -> None:
        self.problems[rule] += 1
        if len(self.samples) < SAMPLE_LIMIT:
            self.samples.append(f"{rule}:{subject}")


def _load_indexes(
    ledger_paths: Iterable[str | Path],
    *,
    platform: str,
    mode: str,
    report: _Report,
) -> dict[str, _LedgerIndex]:
    indexes: dict[str, _LedgerIndex] = {}
    for raw in sorted({str(Path(path)) for path in ledger_paths}):
        try:
            loaded = load_ledger(raw)
        except LedgerFormatError:
            report.fail("LEDGER", Path(raw).name)
            continue
        if loaded.platform != platform or loaded.mode != mode:
            report.fail("LEDGER", loaded.ledger_id)
            continue
        index = _LedgerIndex(loaded)
        known = indexes.get(index.ledger_id)
        if known is None:
            indexes[index.ledger_id] = index
            continue
        # 同一账本的快照与续写文件：较短者必须是较长者的前缀，取较长者。
        shorter, longer = sorted((known, index), key=lambda item: len(item.loaded.lines))
        if list(longer.loaded.lines[: len(shorter.loaded.lines)]) != list(shorter.loaded.lines):
            report.fail("LEDGER", index.ledger_id)
            continue
        indexes[index.ledger_id] = longer
    return indexes


def _manifest_hashes(manifests: Iterable[str | Path]) -> dict[tuple[str, str], str]:
    hashes: dict[tuple[str, str], str] = {}
    for raw in sorted({str(Path(path)) for path in manifests}):
        try:
            entries = parse_manifest(Path(raw).read_bytes())
        except (OSError, ImageManifestError, ValueError):
            continue
        for entry in entries:
            if entry.fetch_status == "downloaded" and entry.sha256:
                hashes[(str(entry.platform_post_id), str(entry.source_asset_key))] = str(entry.sha256)
    return hashes


def _resolve(
    indexes: Mapping[str, _LedgerIndex], visit_id: Any
) -> tuple[_LedgerIndex, dict[str, Any]] | None:
    try:
        ledger_id, _, _ = parse_line_id(str(visit_id or ""))
    except LedgerFormatError:
        return None
    index = indexes.get(ledger_id)
    if index is None:
        return None
    line = index.by_id.get(str(visit_id))
    return (index, line) if line is not None else None


def _visit_matches(line: Mapping[str, Any], spec: StageSpec, stage: str) -> bool:
    return (
        line.get("kind") == "visit"
        and line.get("stage") == stage
        and line.get("source") == spec.source
        and line.get("rule") == spec.rule
    )


def _search_item_types(visit: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(item.get("id")): str(item.get("type") or "")
        for item in visit.get("items") or []
        if isinstance(item, Mapping) and item.get("id")
    }


def _check_record(
    record: Mapping[str, Any],
    *,
    policy: VisitPolicy,
    mode: str,
    indexes: Mapping[str, _LedgerIndex],
    hashes: Mapping[tuple[str, str], str],
    report: _Report,
    used_details: Counter[str],
    used_images: Counter[str],
) -> None:
    note_id = str(record.get(policy.record_id_field) or "")
    user_id = str(record.get(policy.record_user_field) or "")
    subject = note_id or "<missing-id>"
    refs = record.get("content_visit_refs")
    chain = policy.chains[mode]
    if not isinstance(refs, Mapping) or not note_id:
        report.fail("R1", subject)
        return
    resolved: dict[str, tuple[_LedgerIndex, dict[str, Any]]] = {}
    for stage in chain:
        if stage == "images":
            continue
        found = _resolve(indexes, refs.get(stage))
        if found is None:
            report.fail("R1", subject)
            return
        resolved[stage] = found
    images = refs.get("images")
    if not isinstance(images, list) or not all(_resolve(indexes, item) is not None for item in images):
        report.fail("R1", subject)
        return
    detail_index, detail = resolved["note_detail"]
    if str(refs.get("ledger_id") or "") != detail_index.ledger_id:
        report.fail("R1", subject)
        return

    detail_spec = policy.spec(mode, "note_detail")
    trigger_kind = str(detail_spec.trigger)

    # R2 搜索访问（仅 discovery）。
    search: dict[str, Any] | None = None
    if "search_results" in chain:
        search_index, search = resolved["search_results"]
        types = _search_item_types(search)
        if not (
            _visit_matches(search, policy.spec(mode, "search_results"), "search_results")
            and search.get("status") == 200
            and search.get("is_target") is True
            and note_id in types
            and types[note_id] != "video"
        ):
            report.fail("R2", subject)
        if search_index is not detail_index:
            report.fail("R3", subject)

    # R3 详情访问由同一笔记的列表点击（或 repair 的直开）触发，且晚于搜索访问。
    trigger = detail_index.by_id.get(str(detail.get("trigger") or ""))
    detail_ok = (
        _visit_matches(detail, detail_spec, "note_detail")
        and detail.get("subject_id") == note_id
        and detail.get("page_role") == "primary"
        and (detail.get("source") != "page_response" or detail.get("status") == 200)
        and trigger is not None
        and trigger.get("kind") == "action"
        and trigger.get("action") == trigger_kind
        and str((trigger.get("target") or {}).get("note_id") or "") == note_id
        and _seq(trigger) < _seq(detail)
        and (search is None or _seq(search) < _seq(trigger))
    )
    if not detail_ok:
        report.fail("R3", subject)

    # R4 作者访问由详情内点击作者名触发，作者 ID 与页面 URL 一致。
    creator_index, creator = resolved["creator_profile"]
    creator_spec = policy.spec(mode, "creator_profile")
    author_click = creator_index.by_id.get(str(creator.get("trigger") or ""))
    from_visit = (
        creator_index.by_id.get(str(author_click.get("from_visit") or "")) if author_click is not None else None
    )
    creator_ok = bool(
        user_id
        and creator_index is detail_index
        and _visit_matches(creator, creator_spec, "creator_profile")
        and creator.get("subject_id") == user_id
        and creator.get("url_user_id") == user_id
        and author_click is not None
        and author_click.get("kind") == "action"
        and author_click.get("action") == creator_spec.trigger
        and _seq(author_click) < _seq(creator)
        and from_visit is not None
        and from_visit.get("kind") == "visit"
        and from_visit.get("stage") == "note_detail"
    )
    if not creator_ok:
        report.fail("R4", subject)

    # R5 逐张图片：资产键、哈希与 manifest 一致，且晚于本账本中该笔记的首个触发动作。
    assets = sorted(
        (item for item in record.get("image_assets") or [] if isinstance(item, Mapping)),
        key=lambda item: int(item.get("source_index") or 0),
    )
    first_trigger = detail_index.first_action([trigger_kind], note_id=note_id)
    image_spec = policy.spec(mode, "note_image")
    images_ok = bool(assets) and len(images) == len(assets) and first_trigger is not None
    if images_ok:
        for position, asset in enumerate(assets):
            if int(asset.get("source_index") or 0) != position:
                images_ok = False
                break
            found = _resolve(indexes, images[position])
            assert found is not None
            image_index, image = found
            asset_key = str(asset.get("source_asset_key") or "")
            if not (
                image_index is detail_index
                and _visit_matches(image, image_spec, "note_image")
                and image.get("subject_id") == note_id
                and image.get("status") == 200
                and image.get("asset_key") == asset_key
                and image.get("sha256") == hashes.get((note_id, asset_key))
                and _seq(image) > _seq(first_trigger)
            ):
                images_ok = False
                break
            if trigger is not None and _seq(image) < _seq(trigger):
                report.images_from_earlier_attempt += 1
    if not images_ok:
        report.fail("R5", subject)

    # R6 节奏落点：搜索结果停顿、详情停顿与作者停顿各在对应动作之前。
    pacing_ok = True
    if search is not None:
        page_ids = set(_search_item_types(search))
        first_click = next(
            (
                line
                for line in detail_index.actions
                if _seq(line) > _seq(search)
                and line["action"] == trigger_kind
                and str((line.get("target") or {}).get("note_id") or "") in page_ids
            ),
            None,
        )
        pacing_ok = first_click is not None and detail_index.has_pause(
            "search_results", after=_seq(search), before=_seq(first_click)
        )
    detail_end = detail_index.first_action(DETAIL_PAUSE_END_ACTIONS, after=_seq(detail))
    pacing_ok = pacing_ok and detail_index.has_pause(
        "note_detail", after=_seq(detail), before=_seq(detail_end) if detail_end is not None else None
    )
    if author_click is not None:
        previous = creator_index.last_action_before([trigger_kind], _seq(author_click))
        pacing_ok = pacing_ok and creator_index.has_pause(
            "creator_profile", after=_seq(previous) if previous is not None else 0, before=_seq(author_click)
        )
    if not pacing_ok:
        report.fail("R6", subject)

    used_details[str(detail["id"])] += 1
    for item in images:
        used_images[str(item)] += 1


def _session_key(visit: Mapping[str, Any]) -> tuple[str, str]:
    return str(visit.get("search_session") or ""), str(visit.get("filter_signature") or "")


def _target_search_visits(index: _LedgerIndex, spec: StageSpec) -> list[dict[str, Any]]:
    return [
        line
        for line in index.visits
        if _visit_matches(line, spec, "search_results") and line.get("is_target") is True
    ]


def _check_entry(index: _LedgerIndex, policy: VisitPolicy, report: _Report) -> None:
    """G1：目标会话第 1 页由点选目标筛选触发、关键词一致，且之前有回车及其非目标第 1 页。"""
    spec = policy.spec("discovery", "search_results")
    for visit in _target_search_visits(index, spec):
        trigger = index.by_id.get(str(visit.get("trigger") or ""))
        if not (
            visit.get("search_page") == 1
            and visit.get("keyword_matches_target") is True
            and trigger is not None
            and trigger.get("action") == "apply_filter"
            and (trigger.get("target") or {}).get("filter") == policy.target_filter
        ):
            continue
        for enter in index.actions:
            if _seq(enter) >= _seq(trigger) or enter["action"] != "press_enter":
                continue
            if any(
                _visit_matches(line, spec, "search_results")
                and line.get("trigger") == enter["id"]
                and line.get("is_target") is False
                and line.get("search_page") == 1
                for line in index.visits
            ):
                return
    report.fail("G1", index.ledger_id)


def _check_page_pauses(index: _LedgerIndex, policy: VisitPolicy, report: _Report) -> int:
    """G3：目标会话第 k≥2 页之前若有我们的列表滚动，首个滚动之前须有翻页停顿。"""
    spec = policy.spec("discovery", "search_results")
    scroll_triggered = 0
    sessions: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for visit in _target_search_visits(index, spec):
        sessions.setdefault(_session_key(visit), []).append(visit)
    for visits in sessions.values():
        for visit in visits:
            page = visit.get("search_page")
            if not isinstance(page, int) or page < 2:
                continue
            previous = [
                line for line in visits if line.get("search_page") == page - 1 and _seq(line) < _seq(visit)
            ]
            if not previous:
                report.fail("G3", str(visit["id"]))
                continue
            before = previous[-1]
            scroll = index.first_action(["scroll"], after=_seq(before), before=_seq(visit), page_role="primary")
            if scroll is None:
                report.frontend_self_loads += 1
                continue
            scroll_triggered += 1
            if not index.has_pause("search_page", after=_seq(before), before=_seq(scroll)):
                report.fail("G3", str(visit["id"]))
    return scroll_triggered


def _check_exhaustion(
    indexes: Mapping[str, _LedgerIndex],
    policy: VisitPolicy,
    stop_event: Mapping[str, Any] | None,
    report: _Report,
) -> None:
    """G2：来源耗尽停止事件指向目标会话中 has_more=false 的页，且同会话第 1..N 页连续存在。"""
    if not stop_event or stop_event.get("stop_reason") != "source_exhausted":
        return
    found = _resolve(indexes, stop_event.get("source_visit_id"))
    spec = policy.spec("discovery", "search_results")
    if found is None:
        report.fail("G2", "<missing-source-visit>")
        return
    index, visit = found
    last_page = visit.get("search_page")
    if not (
        _visit_matches(visit, spec, "search_results")
        and visit.get("is_target") is True
        and visit.get("has_more") is False
        and isinstance(last_page, int)
        and last_page >= 1
    ):
        report.fail("G2", str(visit["id"]))
        return
    pages = {
        line.get("search_page")
        for line in _target_search_visits(index, spec)
        if _session_key(line) == _session_key(visit) and _seq(line) <= _seq(visit)
    }
    if not set(range(1, last_page + 1)) <= pages:
        report.fail("G2", str(visit["id"]))


def validate_content_visits(
    records: Iterable[Mapping[str, Any]],
    ledger_paths: Iterable[str | Path],
    *,
    platform: str,
    mode: str,
    manifests: Iterable[str | Path],
    stop_event: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """`records` 为正式有效集合（含跨轮累计）；`ledger_paths` 为本轮账本与累计摘要携带的快照。"""
    policy = VISIT_POLICIES[platform]
    if mode not in policy.chains:
        raise ValueError(f"unsupported content visit mode: {mode}")
    report = _Report()
    indexes = _load_indexes(ledger_paths, platform=platform, mode=mode, report=report)
    if not indexes:
        report.fail("LEDGER", "<no-ledger>")
    hashes = _manifest_hashes(manifests)
    used_details: Counter[str] = Counter()
    used_images: Counter[str] = Counter()
    checked = 0
    for record in records:
        checked += 1
        _check_record(
            record,
            policy=policy,
            mode=mode,
            indexes=indexes,
            hashes=hashes,
            report=report,
            used_details=used_details,
            used_images=used_images,
        )
    for visit_id, count in [*used_details.items(), *used_images.items()]:
        if count > 1:
            report.fail("R7", visit_id)

    required_pacing: set[str] = set()
    required_continuity: set[str] = set()
    observed: set[str] = set()
    completed: set[str] = set()
    pacing_ok = continuity_ok = bool(indexes)
    detail_spec_by_mode = policy.spec(mode, "note_detail")
    for index in indexes.values():
        scroll_triggered = 0
        if mode == "discovery":
            _check_entry(index, policy, report)
            scroll_triggered = _check_page_pauses(index, policy, report)
        facts = LedgerFacts(
            detail_visits=sum(
                1 for line in index.visits if _visit_matches(line, detail_spec_by_mode, "note_detail")
            ),
            author_clicks=sum(1 for line in index.actions if line["action"] == "click_author"),
            scroll_triggered_pages=scroll_triggered,
        )
        required = policy.required_stages(facts, mode)
        ledger_observed = {line["stage"] for line in index.pauses if line["stage"] in PACED_STAGES}
        ledger_completed = {line["stage"] for line in index.pauses if line.get("continuity") == "completed"}
        pacing_ok = pacing_ok and required.pacing <= ledger_observed
        continuity_ok = continuity_ok and required.continuity <= ledger_completed
        required_pacing |= required.pacing
        required_continuity |= required.continuity
        observed |= ledger_observed
        completed |= ledger_completed & CONTINUITY_STAGES
    if mode == "discovery":
        _check_exhaustion(indexes, policy, stop_event, report)

    signals = [line for index in indexes.values() for line in index.signals]
    return {
        "required": True,
        "platform": platform,
        "mode": mode,
        "content_visit_ok": not report.problems,
        "checked_records": checked,
        "ledger_count": len(indexes),
        "problem_counts": dict(sorted(report.problems.items())),
        "sample_ids": list(report.samples),
        "required_stages": sorted(required_pacing),
        "observed_stages": sorted(observed),
        "continuity_required_stages": sorted(required_continuity),
        "continuity_completed_stages": sorted(completed),
        "request_pacing_ok": pacing_ok,
        "continuity_ok": continuity_ok,
        "frontend_self_loads": report.frontend_self_loads,
        "signals_ignored": sum(1 for line in signals if line.get("outcome") == "ignored"),
        "signals_verification_surfaced": sum(
            1 for line in signals if line.get("outcome") == "verification_surfaced"
        ),
        "images_from_earlier_attempt": report.images_from_earlier_attempt,
    }


def validate_summary_content_visits(
    summary: Mapping[str, Any],
    valid_items: Iterable[Mapping[str, Any]],
    platform: str,
    *,
    repair_mode: bool,
    stop_event: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """从 child 摘要取账本路径（本轮与累计快照的并集），对正式有效集合执行访问门禁。"""
    items = [item for item in valid_items if item.get("platform") == platform]
    ledger_paths = {
        str(path)
        for record in summary.get("records") or []
        if isinstance(record, Mapping) and record.get("platform") == platform
        for path in ((record.get("output") or {}).get("content_visit_paths") or [])
    }
    manifests = {str(path) for item in items for path in item.get("manifest_paths") or []}
    return validate_content_visits(
        [item["record"] for item in items],
        ledger_paths,
        platform=platform,
        mode="repair" if repair_mode else "discovery",
        manifests=manifests,
        stop_event=None if repair_mode else stop_event,
    )
