"""小红书历史详情修复（设计 §9）：直开保存的链接读页面状态、翻图截获图片、点击作者名；失败码与运行级阻断按类型判定。

单个目标的流程经离线站点真实执行（整页详情、图片响应、作者标签页），记录与账本再经真实访问门禁回读。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from support.xhs_fake_site import XhsFakeSite, detail_card, image_url
from trippostcollect.application.visit_gate import validate_content_visits
from trippostcollect.platforms.xhs import author as xhs_author
from trippostcollect.platforms.xhs import errors, repair
from trippostcollect.platforms.xhs import media as xhs_media
from trippostcollect.platforms.xhs.parser import update_xhs_note
from trippostcollect.runtime.visit_ledger import iter_ledger_lines

from .support import XiaoHongShuCrawler, config, make_capture
from .test_xhs_discovery_memory import write_manifest

ROOT = Path(__file__).resolve().parents[3]
KEYWORD = "青岛旅游"
OK_STATE = {"closed": False, "visible_text": "ok", "manual_markers": [], "terminal": ""}
LOGIN_STATE = {"closed": False, "visible_text": "登录", "manual_markers": ["login_required"], "terminal": ""}


def saved_link(note_id: str) -> str:
    return f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=token-{note_id}&xsec_source=pc_search"


async def _no_sleep(*_args, **_kwargs) -> None:
    return None


def prepare_repair(monkeypatch, tmp_path, note_ids, *, batch_size=2):
    """repair 模式的 crawler 接到离线站点：返回 (crawler, 站点, 报告快照列表)。

    暂存与写出替换为内存记录：`crawler.staged[note_id]` 为交给暂存的图片，`crawler.records` 为写出的记录。
    """
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_REPAIR_BATCH_SIZE", str(batch_size))
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "XHS_SPECIFIED_NOTE_URL_LIST", [saved_link(note_id) for note_id in note_ids])
    monkeypatch.setattr(repair, "XHS_REPAIR_STATE_SECONDS", 0.05)
    monkeypatch.setattr(xhs_media, "XHS_IMAGE_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(xhs_author, "XHS_AUTHOR_TAB_OPEN_SECONDS", 0.05)
    monkeypatch.setattr(xhs_author, "XHS_CREATOR_RUNTIME_READY_SECONDS", 0.05)
    monkeypatch.setattr(xhs_author, "inspect_visible_page_state", AsyncMock(return_value=("", {})))

    site = XhsFakeSite(KEYWORD, [[]])
    crawler = XiaoHongShuCrawler()
    crawler.ports.repair = True
    reports: list[dict] = []
    crawler.ports.write_repair_report = lambda report: reports.append(json.loads(json.dumps(report)))
    crawler.context_page = site.page
    crawler.browser_context = site.context
    crawler.capture = make_capture(tmp_path, context=site.context, mode="repair")
    crawler.capture.install(site.page)
    crawler._scan_sleep = _no_sleep
    crawler._popup_sleep = _no_sleep

    async def guarded_pause(stage, minimum, maximum):
        crawler.capture.pause_finished(stage, crawler.capture.pause_started(), 0.0, "n/a")
        return 0.0

    async def goto(page, url, *, stage, **_kwargs):
        assert page is site.page and stage == "repair_note_detail"
        await site.goto(url)

    crawler._guarded_pause = guarded_pause
    crawler._goto_with_deadline = goto
    crawler._wait_for_visible_page_shell = AsyncMock(return_value=True)
    crawler._popup_checkpoint_state = AsyncMock(return_value=OK_STATE)
    crawler.staged = {}
    crawler.records = []

    async def stage_images(note_id, items):
        crawler.staged[note_id] = items

    async def store(note_item):
        crawler.records.append(
            update_xhs_note(note_item, source_keyword=KEYWORD, current_timestamp=lambda: 0, save_data_option="jsonl")
        )

    crawler.update_xhs_note_images = stage_images
    crawler.record_xhs_note_image_failure = AsyncMock()
    monkeypatch.setattr(crawler, "update_xhs_note", AsyncMock(side_effect=store))
    return crawler, site, reports


def ledger_lines(crawler) -> list[dict]:
    return list(iter_ledger_lines(crawler.capture.ledger.path))


def failures_by_id(report: dict) -> dict[str, dict]:
    return {failure["platform_post_id"]: failure for failure in report["candidate_failures"]}


def gate(crawler, tmp_path) -> dict:
    return validate_content_visits(
        crawler.records,
        [crawler.capture.ledger.path],
        platform="xhs",
        mode="repair",
        manifests=[write_manifest(crawler, tmp_path / "image_manifest.jsonl")],
    )


@pytest.mark.asyncio
async def test_repair_opens_saved_links_reads_page_state_and_passes_the_visit_gate(monkeypatch, tmp_path) -> None:
    crawler, site, reports = prepare_repair(monkeypatch, tmp_path, ["note-a", "note-b", "note-c"])
    site.details["note-a"] = detail_card("note-a", images=3)
    site.details["note-c"] = detail_card("note-c", images=1, user_id="author-note-a")

    await crawler.get_specified_notes()

    report = reports[-1]
    assert report["successful_ids"] == ["note-a", "note-b", "note-c"] and report["candidate_failures"] == []
    assert [batch["successful_ids"] for batch in report["batches"]] == [["note-a", "note-b"], ["note-c"]]
    assert site.note_page_opens == ["note-a", "note-b", "note-c"] and site.feed_requests == []
    records = {record["note_id"]: record for record in crawler.records}
    assert records["note-a"]["xsec_token"] == "token-note-a" and records["note-a"]["followers_observed"] is True
    assert [item["content"] for item in crawler.staged["note-a"]] == [
        f"image-note-a-{index}".encode() for index in range(3)
    ]
    # 同一作者本轮只点一次，后一篇引用首次作者访问。
    assert len(site.author_tabs) == 2
    assert records["note-c"]["content_visit_refs"]["creator_profile"] == (
        records["note-a"]["content_visit_refs"]["creator_profile"]
    )
    lines = ledger_lines(crawler)
    assert lines[0]["mode"] == "repair"
    visits = [line for line in lines if line["kind"] == "visit" and line["stage"] == "note_detail"]
    assert {visit["source"] for visit in visits} == {"page_state"}
    assert {visit["rule"] for visit in visits} == {"note_detail_state"}
    navigates = [line for line in lines if line.get("action") == "navigate"]
    assert [line["target"] for line in navigates] == [
        {"note_id": note_id, "route": "/explore/<id>"} for note_id in ("note-a", "note-b", "note-c")
    ]
    assert "token-note" not in crawler.capture.ledger.path.read_text(encoding="utf-8")

    result = gate(crawler, tmp_path)
    assert result["problem_counts"] == {}
    assert result["content_visit_ok"] is True and result["checked_records"] == 3
    assert result["required_stages"] == ["creator_profile", "note_detail"]


@pytest.mark.asyncio
async def test_image_window_opens_before_navigate_so_first_images_are_claimed(monkeypatch, tmp_path) -> None:
    crawler, site, _reports = prepare_repair(monkeypatch, tmp_path, ["note-a"])

    await crawler.get_specified_notes()

    lines = ledger_lines(crawler)
    navigate_seq = next(line["seq"] for line in lines if line.get("action") == "navigate")
    state_seq = next(line["seq"] for line in lines if line.get("stage") == "note_detail" and line["kind"] == "visit")
    images = [line for line in lines if line["kind"] == "visit" and line["stage"] == "note_image"]
    # 首图与相邻图在直开时就到达，早于页面状态读取；窗口在 navigate 之前已打开，照样认领。
    assert len(images) == 2 and all(navigate_seq < image["seq"] < state_seq for image in images)
    assert gate(crawler, tmp_path)["content_visit_ok"] is True


@pytest.mark.asyncio
async def test_link_redirected_to_404_is_a_final_candidate_failure(monkeypatch, tmp_path) -> None:
    crawler, site, reports = prepare_repair(monkeypatch, tmp_path, ["gone", "note-b"])
    site.unavailable_notes.add("gone")

    await crawler.get_specified_notes()

    report = reports[-1]
    assert report["successful_ids"] == ["note-b"]
    failure = failures_by_id(report)["gone"]
    assert failure["error_code"] == "repair_link_unavailable" and failure["retryable"] is False
    assert failure["failure_scope"] == "detail"
    decisions = [line for line in ledger_lines(crawler) if line["kind"] == "decision"]
    assert [(line["subject_id"], line["decision"]) for line in decisions] == [("gone", "repair_link_unavailable")]
    assert gate(crawler, tmp_path)["content_visit_ok"] is True


@pytest.mark.asyncio
async def test_login_panel_recovers_and_reopens_the_same_link_reusing_captured_images(monkeypatch, tmp_path) -> None:
    crawler, site, reports = prepare_repair(monkeypatch, tmp_path, ["note-a"])
    states = iter([LOGIN_STATE])
    crawler._popup_checkpoint_state = AsyncMock(side_effect=lambda _page: next(states, OK_STATE))
    crawler._wait_for_midrun_login_recovery = AsyncMock(return_value=True)

    await crawler.get_specified_notes()

    crawler._wait_for_midrun_login_recovery.assert_awaited_once()
    assert site.note_page_opens == ["note-a", "note-a"]
    assert reports[-1]["successful_ids"] == ["note-a"]
    lines = ledger_lines(crawler)
    navigates = [line for line in lines if line.get("action") == "navigate"]
    images = [line for line in lines if line["kind"] == "visit" and line["stage"] == "note_image"]
    # 重开时图片走同一文档缓存、不再到达；记录引用第一次直开时截获的图片访问。
    assert len(navigates) == 2 and all(navigates[0]["seq"] < image["seq"] < navigates[1]["seq"] for image in images)
    assert crawler.records[0]["content_visit_refs"]["images"] == [image["id"] for image in images]
    result = gate(crawler, tmp_path)
    assert result["content_visit_ok"] is True and result["images_from_earlier_attempt"] == 2


@pytest.mark.asyncio
async def test_second_login_loss_on_the_same_link_stops_the_run(monkeypatch, tmp_path) -> None:
    crawler, _site, reports = prepare_repair(monkeypatch, tmp_path, ["note-a", "note-b"])
    crawler._popup_checkpoint_state = AsyncMock(return_value=LOGIN_STATE)
    crawler._wait_for_midrun_login_recovery = AsyncMock(return_value=True)

    with pytest.raises(errors.XHSLoginExpired):
        await crawler.get_specified_notes()

    report = reports[-1]
    assert report["runtime_blocker"] == {"error_type": "XHSLoginExpired", "error_code": "login_required"}
    assert report["candidate_failures"] == [] and report["batches"] == []


@pytest.mark.asyncio
async def test_missing_state_and_shape_problems_are_candidate_failures(monkeypatch, tmp_path) -> None:
    crawler, site, reports = prepare_repair(monkeypatch, tmp_path, ["absent", "broken", "clip", "note-d"])
    site.note_states["absent"] = {"status": "missing"}
    broken = detail_card("broken")
    broken["interact_info"].pop("share_count")
    site.details["broken"] = broken
    site.details["clip"] = detail_card("clip", note_type="video")

    await crawler.get_specified_notes()

    report = reports[-1]
    assert report["successful_ids"] == ["note-d"]
    failures = failures_by_id(report)
    assert (failures["absent"]["error_code"], failures["absent"]["retryable"]) == ("note_detail_not_observed", True)
    assert (failures["broken"]["error_code"], failures["broken"]["retryable"]) == ("detail_parse_failed", False)
    assert failures["clip"]["error_code"] == "video_skipped"
    assert failures["clip"]["failure_scope"] == "content_policy" and failures["clip"]["retryable"] is False
    assert [record["note_id"] for record in crawler.records] == ["note-d"]


@pytest.mark.asyncio
async def test_missing_image_is_retryable_and_forbidden_image_stops_the_run(monkeypatch, tmp_path) -> None:
    crawler, site, reports = prepare_repair(monkeypatch, tmp_path, ["note-a", "note-b", "note-c"], batch_size=5)
    site.image_overrides[image_url("note-a", 1)] = "missing"
    site.image_overrides[image_url("note-b", 0)] = 403

    with pytest.raises(errors.PlatformRuntimeError) as exc_info:
        await crawler.get_specified_notes()

    assert exc_info.value.code == "image_auth_required"
    report = reports[-1]
    failure = failures_by_id(report)["note-a"]
    assert (failure["failure_scope"], failure["error_code"], failure["retryable"]) == (
        "image", "image_response_not_observed", True,
    )
    assert report["runtime_blocker"]["error_code"] == "image_auth_required"
    assert site.note_page_opens == ["note-a", "note-b"]


@pytest.mark.asyncio
async def test_author_link_opening_in_the_same_page_is_read_then_navigated_back(monkeypatch, tmp_path) -> None:
    crawler, site, reports = prepare_repair(monkeypatch, tmp_path, ["note-a", "note-b"])
    site.author_same_page = True

    await crawler.get_specified_notes()

    assert reports[-1]["successful_ids"] == ["note-a", "note-b"] and site.author_tabs == []
    lines = ledger_lines(crawler)
    assert [line["target"]["note_id"] for line in lines if line.get("action") == "go_back"] == ["note-a", "note-b"]
    creators = [line for line in lines if line["kind"] == "visit" and line["stage"] == "creator_profile"]
    assert [line["page_role"] for line in creators] == ["primary", "primary"]
    assert gate(crawler, tmp_path)["content_visit_ok"] is True


@pytest.mark.asyncio
async def test_creator_mismatch_is_a_final_creator_failure(monkeypatch, tmp_path) -> None:
    crawler, site, reports = prepare_repair(monkeypatch, tmp_path, ["note-a"])
    site.author_url_ids["author-note-a"] = "someone-else"

    await crawler.get_specified_notes()

    failure = failures_by_id(reports[-1])["note-a"]
    assert failure["failure_scope"] == "creator" and failure["error_code"] == "creator_profile_unavailable"
    assert failure["detail"] == "creator_profile_failed:creator_mismatch" and failure["retryable"] is False
    assert crawler.records == []


def test_repair_blocking_uses_exception_types_not_text() -> None:
    """自 T09 迁入：阻断按根异常类型判定；普通候选失败不阻断。"""
    plain_ip = errors.IPBlockError("Network connection error, please check network settings or restart")
    plain_runtime = errors.PlatformRuntimeError(
        "XHS platform security limit, code 300011", code="platform_security_limit_300011",
    )
    assert repair._repair_exception_is_blocking(plain_ip) is True
    assert repair._repair_exception_is_blocking(plain_runtime) is True
    assert repair._repair_exception_is_blocking(errors.XHSLoginExpired("repair_note_detail")) is True
    assert repair._repair_exception_is_blocking(RuntimeError("普通失败")) is False
    assert repair._repair_exception_is_blocking(RuntimeError("captcha visible")) is True
    assert repair._xhs_repair_blocker(plain_ip) == {"error_type": "IPBlockError", "error_code": "ip_blocked"}
    assert repair._xhs_repair_failure_scope(RuntimeError()) == "candidate"
    for code in ("note_not_found", "video_skipped", "repair_link_unavailable"):
        failure = repair._xhs_repair_failure(note_id="r9", batch=2, failure_scope="detail", error_code=code)
        assert failure["retryable"] is False and failure["identity"] == "xhs:id:r9"


def test_root_repair_imports_root_errors_explicitly() -> None:
    path = ROOT / "src/trippostcollect/platforms/xhs/repair.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        and node.module == "trippostcollect.platforms.xhs.errors"
        for alias in node.names
    }
    assert {"IPBlockError", "PlatformRuntimeError"} <= imported
    assert "sys.modules" not in path.read_text(encoding="utf-8")


def test_root_repair_is_an_explicit_branch_of_the_detail_mixin() -> None:
    from trippostcollect.platforms.xhs.core import XiaoHongShuCrawler as RootCrawler

    assert RootCrawler.get_specified_notes.__module__ == "trippostcollect.platforms.xhs.detail"
    assert not hasattr(RootCrawler, "_trippostcollect_repair_resilience")
