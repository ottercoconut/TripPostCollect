"""#185 知乎访问门禁：离线账本，不访问平台、不启动浏览器。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from trippostcollect.application.visit_gate import VISIT_POLICIES, validate_content_visits
from trippostcollect.artifacts.image_manifest import ImageManifestEntry, write_manifest_atomic
from trippostcollect.platforms.zhihu.client import ZHIHU_CONTENT_IMAGE_RULE
from trippostcollect.platforms.zhihu.page_scripts import STATE_SCRIPTS
from trippostcollect.platforms.zhihu.search_capture import ZHIHU_SEARCH_RULE
from trippostcollect.runtime.visit_ledger import VisitLedger


T = "2026-11-13T00:00:00+00:00"


def test_zhihu_policy_matches_ledger_rule_names():
    policy = VISIT_POLICIES["zhihu"]
    assert policy.spec("discovery", "search_results").rule == ZHIHU_SEARCH_RULE.name
    assert policy.spec("discovery", "search_results").source == "page_response"
    assert policy.spec("discovery", "note_detail").rule == "zhihu.initial_data"
    assert policy.spec("discovery", "note_detail").trigger == "click_card"
    assert policy.spec("discovery", "note_detail").source == "page_state"
    assert policy.spec("repair", "note_detail").trigger == "navigate"
    assert policy.spec("discovery", "creator_profile").trigger == "click_author"
    assert policy.spec("discovery", "note_image").rule == ZHIHU_CONTENT_IMAGE_RULE
    assert policy.spec("discovery", "note_image").source == "cdn_direct"
    assert "zhihu.initial_data" in STATE_SCRIPTS
    assert policy.checks_filter_entry is False
    assert policy.allow_zero_content_images is True
    assert VISIT_POLICIES["xhs"].allow_zero_content_images is False
    assert tuple(policy.chains) == ("discovery", "repair")


class Run:
    def __init__(self, path: Path) -> None:
        self.ledger = VisitLedger.create(path, platform="zhihu", mode="discovery", clock=lambda: T)
        self.path = path

    def seq(self) -> int:
        return self.ledger.next_seq()

    def action(self, kind: str, target: dict | None = None, *, from_visit: str | None = None,
               page_role: str = "primary") -> str:
        return self.ledger.append(
            "action", seq=self.seq(), page_role=page_role, action=kind,
            target=target or {}, from_visit=from_visit,
        )

    def visit(self, **fields) -> str:
        fields.setdefault("page_role", "primary")
        return self.ledger.append("visit", seq=self.seq(), at=T, recorded_at=T, **fields)

    def pause(self, stage: str, *, continuity: str = "n/a", page_role: str = "primary") -> str:
        start = self.seq()
        return self.ledger.append(
            "pause", seq=self.seq(), page_role=page_role, stage=stage, start_seq=start,
            seconds=1.0, continuity=continuity,
        )


def image_bytes() -> bytes:
    return b"zhihu-image"


def asset_key() -> str:
    return "zhihu:urlsha256:" + sha256(b"identity").hexdigest()


def manifest(path: Path, *, digest: str | None = None) -> Path:
    data = image_bytes()
    write_manifest_atomic(path, [ImageManifestEntry(
        schema_version=1, platform_key="zhihu", platform_post_id="101", image_role="content",
        source_index=0, source_key="image_list", source_asset_key=asset_key(),
        source_url="https://pic1.zhimg.com/v2-body.jpg", fetch_status="downloaded", attempts=1,
        http_status=200, staging_path="zhihu/images/101/000.jpg", size_bytes=len(data),
        mime_type="image/jpeg", width=8, height=5,
        sha256=digest if digest is not None else sha256(data).hexdigest(), error_code=None,
    )])
    return path


def good_run(tmp_path: Path, *, pages: int = 1, click: str = "click_card") -> tuple[Run, dict, str]:
    run = Run(tmp_path / "behavior.content_visits.jsonl")
    run.action("navigate", {"route": "/search", "query_keys": ["q", "type"]})
    search = run.visit(
        stage="search_results", source="page_response", rule="zhihu.search_v3", status=200,
        subject_id=None, trigger=None, search_page=1, search_session="session-1",
        filter_signature="type=content", is_target=True, has_more=pages > 1,
        items=[{"id": "101", "type": "answer"}],
    )
    run.pause("search_results", continuity="completed")
    trigger = run.action(click, {"content_id": "101"})
    detail = run.visit(
        stage="note_detail", source="page_state", rule="zhihu.initial_data", status=None,
        subject_id="101", trigger=trigger, page_role="detail",
    )
    run.pause("note_detail", page_role="detail")
    run.pause("creator_profile", page_role="detail")
    author_click = run.action(
        "click_author", {"content_id": "101"}, from_visit=detail, page_role="detail",
    )
    author = run.visit(
        stage="creator_profile", source="page_state", rule="zhihu.initial_data", status=None,
        subject_id="author", trigger=author_click, url_user_id="author", page_role="author",
    )
    run.pause("author_tab", page_role="author")
    image = run.visit(
        stage="note_image", source="cdn_direct", rule="zhihu.content_image", status=200,
        subject_id="101", trigger=trigger, asset_key=asset_key(),
        sha256=sha256(image_bytes()).hexdigest(),
    )
    last = search
    if pages > 1:
        run.pause("search_page", continuity="completed")
        run.action("scroll", {"phase": "search_page"})
        last = run.visit(
            stage="search_results", source="page_response", rule="zhihu.search_v3", status=200,
            subject_id=None, trigger=None, search_page=pages, search_session="session-1",
            filter_signature="type=content", is_target=True, has_more=False, items=[],
        )
    record = {
        "content_id": "101",
        "creator_url_token": "author",
        "image_assets": [{"source_index": 0, "source_asset_key": asset_key()}],
        "content_visit_refs": {
            "ledger_id": run.ledger.ledger_id,
            "search_results": search,
            "note_detail": detail,
            "creator_profile": author,
            "images": [image],
        },
    }
    return run, record, last


def check(run: Run, record: dict, manifest_path: Path, *, stop: dict | None) -> dict:
    return validate_content_visits(
        [record], [run.path], platform="zhihu", mode="discovery",
        manifests=[manifest_path], stop_event=stop,
    )


def test_full_round_passes_content_visit_gate(tmp_path):
    run, record, last = good_run(tmp_path, pages=2)
    result = check(
        run, record, manifest(tmp_path / "image_manifest.jsonl"),
        stop={"stop_reason": "source_exhausted", "source_visit_id": last},
    )
    assert result["content_visit_ok"] is True, result
    assert result["request_pacing_ok"] is True
    assert result["continuity_ok"] is True


def test_missing_author_visit_fails_r1(tmp_path):
    run, record, last = good_run(tmp_path)
    record["content_visit_refs"]["creator_profile"] = ""
    result = check(
        run, record, manifest(tmp_path / "image_manifest.jsonl"),
        stop={"stop_reason": "source_exhausted", "source_visit_id": last},
    )
    assert result["content_visit_ok"] is False
    assert result["problem_counts"].get("R1")


def test_zero_image_answer_passes_r5(tmp_path):
    run, record, last = good_run(tmp_path)
    record["image_assets"] = []
    record["content_visit_refs"]["images"] = []
    result = check(
        run, record, manifest(tmp_path / "image_manifest.jsonl"),
        stop={"stop_reason": "source_exhausted", "source_visit_id": last},
    )
    assert result["content_visit_ok"] is True, result
    assert result["problem_counts"] == {}


def test_missing_image_registration_fails_r5(tmp_path):
    run, record, last = good_run(tmp_path)
    record["content_visit_refs"]["images"] = []
    result = check(
        run, record, manifest(tmp_path / "image_manifest.jsonl"),
        stop={"stop_reason": "source_exhausted", "source_visit_id": last},
    )
    assert result["problem_counts"].get("R5")


def test_image_hash_mismatch_fails_r5(tmp_path):
    run, record, last = good_run(tmp_path)
    result = check(
        run, record, manifest(tmp_path / "image_manifest.jsonl", digest=sha256(b"other").hexdigest()),
        stop={"stop_reason": "source_exhausted", "source_visit_id": last},
    )
    assert result["problem_counts"].get("R5")


def test_detail_not_opened_by_click_fails_r3(tmp_path):
    run, record, last = good_run(tmp_path, click="navigate")
    result = check(
        run, record, manifest(tmp_path / "image_manifest.jsonl"),
        stop={"stop_reason": "source_exhausted", "source_visit_id": last},
    )
    assert result["problem_counts"].get("R3")


def test_exhaustion_missing_page_fails_g2(tmp_path):
    run, record, _last = good_run(tmp_path, pages=1)
    # 停止事件指向一份不存在的第 2 页：同会话没有连续页码。
    missing = run.visit(
        stage="search_results", source="page_response", rule="zhihu.search_v3", status=200,
        subject_id=None, trigger=None, search_page=3, search_session="session-1",
        filter_signature="type=content", is_target=True, has_more=False, items=[],
    )
    result = check(
        run, record, manifest(tmp_path / "image_manifest.jsonl"),
        stop={"stop_reason": "source_exhausted", "source_visit_id": missing},
    )
    assert result["problem_counts"].get("G2")


def test_repair_chain_accepts_navigate_trigger(tmp_path):
    path = tmp_path / "repair.content_visits.jsonl"
    ledger = VisitLedger.create(path, platform="zhihu", mode="repair", clock=lambda: T)

    def seq() -> int:
        return ledger.next_seq()

    def action(kind: str, target: dict, *, from_visit: str | None = None, page_role: str = "detail") -> str:
        return ledger.append(
            "action", seq=seq(), page_role=page_role, action=kind, target=target, from_visit=from_visit,
        )

    def visit(**fields) -> str:
        fields.setdefault("page_role", "detail")
        return ledger.append("visit", seq=seq(), at=T, recorded_at=T, **fields)

    def pause(stage: str) -> None:
        start = seq()
        ledger.append(
            "pause", seq=seq(), page_role="detail", stage=stage, start_seq=start, seconds=1.0, continuity="n/a",
        )

    trigger = action("navigate", {"content_id": "101"})
    detail = visit(
        stage="note_detail", source="page_state", rule="zhihu.initial_data", status=None,
        subject_id="101", trigger=trigger,
    )
    pause("note_detail")
    pause("creator_profile")
    author_click = action("click_author", {"content_id": "101"}, from_visit=detail)
    author = visit(
        stage="creator_profile", source="page_state", rule="zhihu.initial_data", status=None,
        subject_id="author", trigger=author_click, url_user_id="author", page_role="author",
    )
    image = visit(
        stage="note_image", source="cdn_direct", rule="zhihu.content_image", status=200,
        subject_id="101", trigger=trigger, asset_key=asset_key(),
        sha256=sha256(image_bytes()).hexdigest(), page_role="primary",
    )
    record = {
        "content_id": "101",
        "creator_url_token": "author",
        "image_assets": [{"source_index": 0, "source_asset_key": asset_key()}],
        "content_visit_refs": {
            "ledger_id": ledger.ledger_id,
            "note_detail": detail,
            "creator_profile": author,
            "images": [image],
        },
    }
    result = validate_content_visits(
        [record], [path], platform="zhihu", mode="repair",
        manifests=[manifest(tmp_path / "image_manifest.jsonl")], stop_event=None,
    )
    assert result["content_visit_ok"] is True, result
    json.dumps(result)
