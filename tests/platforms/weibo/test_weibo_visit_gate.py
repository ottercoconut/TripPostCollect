"""#211 微博访问门禁：离线账本，不访问平台、不启动浏览器。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from trippostcollect.application.visit_gate import VISIT_GATED_PLATFORMS, VISIT_POLICIES, validate_content_visits
from trippostcollect.artifacts.image_manifest import ImageManifestEntry, write_manifest_atomic
from trippostcollect.platforms.weibo.client import WEIBO_CONTENT_IMAGE_RULE
from trippostcollect.platforms.weibo.page_scripts import STATE_SCRIPTS
from trippostcollect.platforms.weibo.search_page import WEIBO_SHOW_RULE, WEIBO_SIDE_USER_RULE
from trippostcollect.runtime.visit_ledger import VisitLedger


T = "2026-11-19T00:00:00+00:00"


def test_weibo_policy_matches_ledger_rule_names():
    policy = VISIT_POLICIES["weibo"]
    assert "weibo" in VISIT_GATED_PLATFORMS
    assert policy.creator_from_detail_page_response is True
    assert VISIT_POLICIES["xhs"].creator_from_detail_page_response is False
    assert VISIT_POLICIES["zhihu"].creator_from_detail_page_response is False
    assert policy.spec("discovery", "search_results").rule == "weibo.search_cards"
    assert policy.spec("discovery", "search_results").source == "page_state"
    assert policy.spec("discovery", "note_detail").rule == WEIBO_SHOW_RULE.name
    assert policy.spec("discovery", "note_detail").trigger == "click_card"
    assert policy.spec("repair", "note_detail").trigger == "navigate"
    assert policy.spec("discovery", "creator_profile").rule == WEIBO_SIDE_USER_RULE.name
    assert policy.spec("discovery", "creator_profile").source == "page_response"
    assert policy.spec("discovery", "note_image").rule == WEIBO_CONTENT_IMAGE_RULE
    assert policy.spec("discovery", "note_image").source == "cdn_direct"
    assert policy.checks_filter_entry is False
    assert policy.allow_zero_content_images is False
    assert "weibo.config_uid" in STATE_SCRIPTS
    assert tuple(policy.chains["discovery"]) == (
        "search_results", "note_detail", "creator_profile", "images",
    )
    assert tuple(policy.chains["repair"]) == ("note_detail", "creator_profile", "images")


class Run:
    def __init__(self, path: Path, *, mode: str = "discovery") -> None:
        self.ledger = VisitLedger.create(path, platform="weibo", mode=mode, clock=lambda: T)
        self.path = path

    def seq(self) -> int:
        return self.ledger.next_seq()

    def action(self, kind: str, target: dict | None = None, *, page_role: str = "primary") -> str:
        return self.ledger.append(
            "action", seq=self.seq(), page_role=page_role, action=kind, target=target or {},
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
    return b"weibo-image"


def asset_key() -> str:
    return "weibo:pid:pid-1"


def manifest(path: Path, *, digest: str | None = None) -> Path:
    data = image_bytes()
    write_manifest_atomic(path, [ImageManifestEntry(
        schema_version=1, platform_key="weibo", platform_post_id="525", image_role="content",
        source_index=0, source_key="image_list", source_asset_key=asset_key(),
        source_url="https://wx1.sinaimg.cn/large/pid-1.jpg", fetch_status="downloaded", attempts=1,
        http_status=200, staging_path="weibo/images/525/000.jpg", size_bytes=len(data),
        mime_type="image/jpeg", width=8, height=5,
        sha256=digest if digest is not None else sha256(data).hexdigest(), error_code=None,
    )])
    return path


def good_run(tmp_path: Path, *, pages: int = 1, click: str = "click_card", author_role: str = "detail",
             author_id: str = "88") -> tuple[Run, dict, str]:
    run = Run(tmp_path / "behavior.content_visits.jsonl")
    run.action("navigate", {"route": "/weibo", "query_keys": ["q", "nodup"]})
    search = run.visit(
        stage="search_results", source="page_state", rule="weibo.search_cards", status=None,
        subject_id=None, trigger=None, search_page=1, search_session="session-1",
        filter_signature="nodup=1", is_target=True, has_more=pages > 1,
        items=[{"id": "525", "type": "pic"}],
    )
    run.pause("search_results", continuity="completed")
    trigger = run.action(click, {"note_id": "525"})
    detail = run.visit(
        stage="note_detail", source="page_response", rule="weibo.statuses_show", status=200,
        subject_id="525", trigger=trigger, page_role="detail",
    )
    run.pause("note_detail", page_role="detail")
    author = run.visit(
        stage="creator_profile", source="page_response", rule="weibo.side_user", status=200,
        subject_id=author_id, trigger=trigger, page_role=author_role,
    )
    image = run.visit(
        stage="note_image", source="cdn_direct", rule="weibo.content_image", status=200,
        subject_id="525", trigger=trigger, asset_key=asset_key(),
        sha256=sha256(image_bytes()).hexdigest(),
    )
    last = search
    if pages > 1:
        run.pause("search_page", continuity="completed")
        run.action("scroll", {"phase": "search_page"})
        last = run.visit(
            stage="search_results", source="page_state", rule="weibo.search_cards", status=None,
            subject_id=None, trigger=None, search_page=pages, search_session="session-1",
            filter_signature="nodup=1", is_target=True, has_more=False, items=[],
        )
    record = {
        "note_id": "525",
        "author_platform_id": "88",
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


def check(run: Run, record: dict, manifest_path: Path, *, stop: dict | None, mode: str = "discovery") -> dict:
    return validate_content_visits(
        [record], [run.path], platform="weibo", mode=mode,
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
    assert "creator_profile" not in result["required_stages"]


def test_missing_author_visit_fails_r1(tmp_path):
    run, record, last = good_run(tmp_path)
    record["content_visit_refs"]["creator_profile"] = ""
    result = check(
        run, record, manifest(tmp_path / "image_manifest.jsonl"),
        stop={"stop_reason": "source_exhausted", "source_visit_id": last},
    )
    assert result["content_visit_ok"] is False
    assert result["problem_counts"].get("R1")


def test_author_on_another_tab_fails_r4(tmp_path):
    run, record, last = good_run(tmp_path, author_role="author")
    result = check(
        run, record, manifest(tmp_path / "image_manifest.jsonl"),
        stop={"stop_reason": "source_exhausted", "source_visit_id": last},
    )
    assert result["problem_counts"].get("R4")


def test_author_id_mismatch_fails_r4(tmp_path):
    run, record, last = good_run(tmp_path, author_id="99")
    result = check(
        run, record, manifest(tmp_path / "image_manifest.jsonl"),
        stop={"stop_reason": "source_exhausted", "source_visit_id": last},
    )
    assert result["problem_counts"].get("R4")


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
    missing = run.visit(
        stage="search_results", source="page_state", rule="weibo.search_cards", status=None,
        subject_id=None, trigger=None, search_page=3, search_session="session-1",
        filter_signature="nodup=1", is_target=True, has_more=False, items=[],
    )
    result = check(
        run, record, manifest(tmp_path / "image_manifest.jsonl"),
        stop={"stop_reason": "source_exhausted", "source_visit_id": missing},
    )
    assert result["problem_counts"].get("G2")


def test_repair_chain_accepts_navigate_trigger(tmp_path):
    run = Run(tmp_path / "repair.content_visits.jsonl", mode="repair")
    trigger = run.action("navigate", {"note_id": "525"}, page_role="detail")
    detail = run.visit(
        stage="note_detail", source="page_response", rule="weibo.statuses_show", status=200,
        subject_id="525", trigger=trigger, page_role="detail",
    )
    run.pause("note_detail", page_role="detail")
    author = run.visit(
        stage="creator_profile", source="page_response", rule="weibo.side_user", status=200,
        subject_id="88", trigger=trigger, page_role="detail",
    )
    image = run.visit(
        stage="note_image", source="cdn_direct", rule="weibo.content_image", status=200,
        subject_id="525", trigger=trigger, asset_key=asset_key(),
        sha256=sha256(image_bytes()).hexdigest(),
    )
    record = {
        "note_id": "525",
        "author_platform_id": "88",
        "image_assets": [{"source_index": 0, "source_asset_key": asset_key()}],
        "content_visit_refs": {
            "ledger_id": run.ledger.ledger_id,
            "note_detail": detail,
            "creator_profile": author,
            "images": [image],
        },
    }
    result = check(run, record, manifest(tmp_path / "image_manifest.jsonl"), stop=None, mode="repair")
    assert result["content_visit_ok"] is True, result
    assert "creator_profile" not in result["required_stages"]
