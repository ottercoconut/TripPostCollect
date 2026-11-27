"""#223：抖音历史行只补缺。假页面、临时 SQLite，不启动浏览器。"""

from __future__ import annotations

import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from trippostcollect.application.visit_gate import validate_content_visits
from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.platforms.douyin.detail_page import (
    DouyinDetailFetchError,
    DouyinDetailPages,
    FILL_TARGET_NOT_IN_POST_LIST,
    FILL_TARGET_REDIRECTED,
)
from trippostcollect.platforms.douyin.models import PlatformRuntimeError
from trippostcollect.platforms.douyin.search_page import DouyinPageCapture
from trippostcollect.runtime.visit_ledger import content_visits_path


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

repair = import_module("repair_post_details")

AWEME = "7123456789012345678"
SEC = "MS4wLjABAAAAfilltarget"
OTHER_SEC = "MS4wLjABAAAAotheruser"
UID = "9001"
AVATAR = "https://p3.douyinpic.com/aweme/100x100/avatar-fill.jpeg"
NOTE = f"https://www.douyin.com/note/{AWEME}"


def _redirect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(repair, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_OUTPUT", tmp_path / "out")
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_RUNTIME_ROOT", tmp_path / "rt")
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_BACKUP_ROOT", tmp_path / "bk")
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_PAUSE_ROOT", tmp_path / "pause")


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "fill.sqlite"
    bootstrap_database(path, sync_jobs=False)
    return path


def _insert(
    conn: sqlite3.Connection,
    post_id: str,
    *,
    profile: str | None = "https://www.douyin.com/user/kept",
    verified: int | None = 1,
    verified_text: str | None = "已有认证",
    raw: dict | None = None,
    content: str = "青岛正文",
    description: str = "简介",
    followers: int = 12,
) -> int:
    payload = {"content_detail_status": "detail_observed", "content_detail_source": "legacy"}
    if raw is not None:
        payload = raw
    cursor = conn.execute(
        """
        INSERT INTO web_posts(
            platform_key, platform_post_id, source_type, source_url, canonical_url,
            title, content_text, captured_at, published_at, keyword, topic_relevant,
            author_profile_url, author_verified, author_verified_text,
            author_description, author_followers_count, raw_sample_json
        ) VALUES (
            'douyin', ?, 'import', ?, ?,
            '原标题', ?, '2026-01-01T00:00:00+08:00', '2024-05-01T08:00:00+08:00',
            '青岛旅游', 1,
            ?, ?, ?,
            ?, ?, ?
        )
        """,
        (
            post_id,
            f"https://www.douyin.com/note/{post_id}",
            f"https://www.douyin.com/note/{post_id}",
            content,
            profile,
            verified,
            verified_text,
            description,
            followers,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


class Mouse:
    def __init__(self, page: "Page") -> None:
        self.page = page

    async def move(self, x: float, y: float) -> None:
        self.page.moves.append((x, y))

    async def down(self) -> None:
        return None

    async def up(self) -> None:
        self.page.clicks += 1
        context = self.page.context
        if context is None or context.pending is None or not self.page.profiles:
            return
        author = Page(self.page.author_url)
        author.context = context
        context.pages.append(author)
        if context.on_page is not None:
            context.on_page(author)
        for status, payload, url in self.page.profiles:
            author.emit(Response(author, payload, status=status, url=url))
        context.pending.page = author

    async def wheel(self, dx: float, dy: float) -> None:
        self.page.wheels.append((dx, dy))


class Response:
    def __init__(self, page: "Page", payload: dict, *, status: int = 200, url: str) -> None:
        self.url = url
        self.status = status
        self.headers: dict[str, str] = {}
        self.request = SimpleNamespace(
            method="GET",
            resource_type="fetch",
            url=url,
            frame=SimpleNamespace(page=page),
            post_data=None,
        )
        self._body = json.dumps(payload).encode()

    async def body(self) -> bytes:
        return self._body


class Link:
    def __init__(self, page: "Page", spec: dict) -> None:
        self.page = page
        self.spec = spec

    async def is_visible(self) -> bool:
        return bool(self.spec.get("visible", True))

    async def get_attribute(self, name: str) -> str:
        return str(self.spec.get(name) or "")

    async def bounding_box(self) -> dict[str, float]:
        return {"x": 20.0, "y": 40.0, "width": 80.0, "height": 24.0}

    async def click(self, timeout: int = 0) -> None:
        del timeout
        await self.page.mouse.up()

    async def evaluate(self, script: str) -> dict:
        del script
        if self.spec.get("occluded"):
            return {
                "left": 20, "top": 40, "width": 0, "height": 0,
                "clip": {"left": 0, "top": 0, "right": 1200, "bottom": 800},
            }
        return {
            "left": 20, "top": 40, "width": 80, "height": 24,
            "clip": {"left": 0, "top": 0, "right": 1200, "bottom": 800},
        }


class Locator:
    def __init__(self, page: "Page") -> None:
        self.page = page

    async def count(self) -> int:
        return len(self.page.links)

    def nth(self, index: int) -> Link:
        return Link(self.page, self.page.links[index])


class ExpectPage:
    def __init__(self, context: "Context") -> None:
        self.context = context
        self.page: Page | None = None

    async def __aenter__(self) -> "ExpectPage":
        self.context.pending = self
        return self

    async def __aexit__(self, *_args: object) -> bool:
        self.context.pending = None
        return False

    @property
    def value(self):
        async def get() -> Page:
            if self.page is None:
                raise TimeoutError("author tab was not opened")
            return self.page

        return get()


class Context:
    def __init__(self, page: "Page") -> None:
        self.page = page
        self.pages = [page]
        self.pending: ExpectPage | None = None
        self.on_page = None
        page.context = self

    def on(self, event: str, callback) -> None:
        assert event == "page"
        self.on_page = callback

    def expect_page(self, timeout: int | None = None) -> ExpectPage:
        del timeout
        return ExpectPage(self)


class Page:
    def __init__(self, url: str) -> None:
        self.url = url
        self.context: Context | None = None
        self.listeners: dict[str, list] = {}
        self.viewport_size = {"width": 1280, "height": 800}
        self.mouse = Mouse(self)
        self.risk = {
            "hits": [],
            "verify_host": False,
            "captcha_visible": False,
            "login_panel_visible": False,
            "hidden_captcha_count": 0,
        }
        self.links: list[dict] = []
        self.profiles: list[tuple[int, dict, str]] = []
        self.post_payload: dict | None = None
        self.post_status = 200
        self.redirect_to = ""
        self.author_url = f"https://www.douyin.com/user/{SEC}?from_tab_name=main"
        self.gotos: list[str] = []
        self.wheels: list[tuple[float, float]] = []
        self.moves: list[tuple[float, float]] = []
        self.clicks = 0
        self.closed = False

    def on(self, event: str, callback) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def locator(self, selector: str) -> Locator:
        assert "/user/" in selector
        return Locator(self)

    def is_closed(self) -> bool:
        return self.closed

    async def evaluate(self, script: str) -> dict:
        if "验证码" in script:
            return dict(self.risk)
        raise AssertionError(script[:60])

    async def wait_for_timeout(self, timeout: float) -> None:
        del timeout

    async def bring_to_front(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def goto(self, url: str, **_kwargs: object) -> None:
        self.gotos.append(url)
        self.url = self.redirect_to or url
        if self.post_payload is not None and not self.redirect_to:
            self.emit(Response(
                self,
                self.post_payload,
                status=self.post_status,
                url="https://www.douyin.com/aweme/v1/web/aweme/post/",
            ))
        return None

    def emit(self, response: Response) -> None:
        for callback in list(self.listeners.get("response", [])):
            callback(response)


def _aweme(aweme_id: str, sec_uid: str = SEC) -> dict:
    return {
        "aweme_id": aweme_id,
        "desc": "不应落盘的正文",
        "author": {
            "sec_uid": sec_uid,
            "uid": UID,
            "nickname": "不应落盘的昵称",
            "avatar_thumb": {"url_list": [AVATAR]},
        },
    }


def _profile(sec_uid: str, *, text: str | None, uid: str = UID) -> dict:
    user: dict = {
        "sec_uid": sec_uid,
        "uid": uid,
        "nickname": "不应落盘的昵称",
        "avatar_thumb": {"url_list": [AVATAR]},
        "enterprise_verify_reason": "",
        "custom_verify": "",
    }
    if text is None:
        user.pop("enterprise_verify_reason")
        user.pop("custom_verify")
    elif text:
        user["custom_verify"] = text
    else:
        user["account_cert_info"] = "{}"
    return {"status_code": 0, "user": user}


def _harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: Page | None = None):
    evidence = tmp_path / "behavior.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(evidence))
    page = page or Page("https://www.douyin.com/")
    context = page.context or Context(page)
    capture = DouyinPageCapture.open(context, mode="repair")
    capture.install(page)
    return DouyinDetailPages(capture, page), content_visits_path(evidence)


def _post_list(*ids: str, target_index: int | None = None) -> dict:
    items = []
    for index, aweme_id in enumerate(ids):
        sec = SEC if target_index is None or index == target_index else OTHER_SEC
        items.append(_aweme(aweme_id, sec))
    return {"status_code": 0, "aweme_list": items, "has_more": 1}


@pytest.mark.asyncio
async def test_target_in_first_and_later_slots(tmp_path, monkeypatch):
    pages, ledger = _harness(tmp_path, monkeypatch)
    page = pages.search_page
    page.post_payload = _post_list("111", AWEME, target_index=1)
    page.links = [{"href": f"/user/{SEC}", "visible": True}]
    page.profiles = [(
        200,
        _profile(SEC, text="青岛创作者"),
        "https://www.douyin.com/aweme/v1/web/user/profile/other/",
    )]
    observed = await pages.open_note_for_fill(AWEME, need_author=True)
    assert observed.kind == "observed"
    assert observed.author_verified == 1
    assert observed.author_verified_text == "青岛创作者"
    assert observed.author_profile_url == f"https://www.douyin.com/user/{SEC}"
    assert "?" not in observed.author_profile_url
    assert page.gotos == [NOTE]
    assert page.wheels == []
    assert page.clicks == 1
    assert repair.fill_visit_ok(observed.as_dict(), [str(ledger)]) is True

    fresh, second_ledger = _harness(tmp_path / "slot0", monkeypatch)
    fresh.search_page.post_payload = _post_list(AWEME, target_index=0)
    plain = await fresh.open_note_for_fill(AWEME, need_author=False)
    assert plain.kind == "observed" and plain.content_detail_observed is True
    assert fresh.search_page.clicks == 0
    assert repair.fill_visit_ok(plain.as_dict(), [str(second_ledger)]) is True
    rejected = dict(plain.as_dict())
    rejected["image_assets"] = [{"source_index": 0}]
    gate = validate_content_visits(
        [repair.douyin_fill_gate_record(rejected) | {"image_assets": rejected["image_assets"]}],
        [str(second_ledger)],
        platform="douyin",
        mode="repair",
        manifests=[],
    )
    assert gate["content_visit_ok"] is False and "R5" in gate["problem_counts"]


@pytest.mark.asyncio
async def test_missing_list_does_not_page_or_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "trippostcollect.platforms.douyin.detail_page.MODAL_WAIT_SECONDS",
        0.05,
    )
    pages, _ledger = _harness(tmp_path, monkeypatch)
    page = pages.search_page
    page.post_payload = _post_list("111", "222")
    observed = await pages.open_note_for_fill(AWEME, need_author=True)
    assert observed.kind == "unavailable"
    assert observed.error_code == FILL_TARGET_NOT_IN_POST_LIST
    assert page.gotos == [NOTE]
    assert page.wheels == []
    assert page.clicks == 0


@pytest.mark.asyncio
async def test_redirect_is_unavailable_and_logs_shape(tmp_path, monkeypatch, caplog):
    pages, _ledger = _harness(tmp_path, monkeypatch)
    page = pages.search_page
    page.redirect_to = f"https://www.douyin.com/video/{AWEME}?from=note"
    page.post_payload = _post_list(AWEME, target_index=0)
    with caplog.at_level("INFO"):
        observed = await pages.open_note_for_fill(AWEME, need_author=False)
    assert observed.kind == "unavailable"
    assert observed.error_code == FILL_TARGET_REDIRECTED
    assert observed.landing_shape == "www.douyin.com/video/{id}"
    assert AWEME not in observed.landing_shape
    assert page.gotos == [NOTE]
    assert NOTE not in caplog.text
    assert "www.douyin.com/video/{id}" in caplog.text


@pytest.mark.asyncio
async def test_author_click_requires_matching_sec_uid(tmp_path, monkeypatch):
    pages, _ledger = _harness(tmp_path, monkeypatch)
    page = pages.search_page
    page.post_payload = _post_list(AWEME, target_index=0)
    page.links = [
        {"href": f"/user/{OTHER_SEC}", "visible": True},
        {"href": f"/user/{SEC}", "visible": False},
    ]
    observed = await pages.open_note_for_fill(AWEME, need_author=True)
    assert observed.kind == "candidate_failed"
    assert observed.error_code == "author_link_missing"
    assert page.clicks == 0
    assert page.gotos == [NOTE]


@pytest.mark.asyncio
async def test_profile_response_is_chosen_by_sec_uid(tmp_path, monkeypatch):
    pages, ledger = _harness(tmp_path, monkeypatch)
    page = pages.search_page
    page.post_payload = _post_list(AWEME, target_index=0)
    page.links = [{"href": f"https://www.douyin.com/user/{SEC}", "visible": True}]
    page.profiles = [
        (
            200,
            _profile(OTHER_SEC, text="登录账号", uid="1"),
            "https://www.douyin.com/aweme/v1/web/user/profile/other/?sec_user_id=self",
        ),
        (
            200,
            _profile(SEC, text="青岛创作者"),
            "https://www.douyin.com/aweme/v1/web/user/profile/other/?sec_user_id=target",
        ),
    ]
    observed = await pages.open_note_for_fill(AWEME, need_author=True)
    assert observed.author_verified_text == "青岛创作者"
    assert observed.author_uid == UID
    blob = ledger.read_text(encoding="utf-8") + json.dumps(observed.as_dict(), ensure_ascii=False)
    assert AVATAR not in blob
    assert "avatar_thumb" not in blob
    assert "不应落盘" not in blob
    assert "sec_user_id=" not in blob


@pytest.mark.asyncio
async def test_unresolved_verification_is_not_a_failure(tmp_path, monkeypatch):
    pages, _ledger = _harness(tmp_path, monkeypatch)
    page = pages.search_page
    page.post_payload = _post_list(AWEME, target_index=0)
    page.links = [{"href": f"/user/{SEC}", "visible": True}]
    page.profiles = [(
        200,
        _profile(SEC, text=None),
        "https://www.douyin.com/aweme/v1/web/user/profile/other/",
    )]
    observed = await pages.open_note_for_fill(AWEME, need_author=True)
    assert observed.kind == "observed"
    assert observed.author_verified is None
    assert "author_verified" in observed.unresolved_fields


@pytest.mark.asyncio
async def test_visible_captcha_stops_before_navigation(tmp_path, monkeypatch):
    pages, _ledger = _harness(tmp_path, monkeypatch)
    pages.search_page.risk["captcha_visible"] = True
    with pytest.raises(DouyinDetailFetchError) as caught:
        await pages.open_note_for_fill(AWEME, need_author=False)
    assert caught.value.runtime_blocking is True
    assert caught.value.code == "captcha_detected"
    assert pages.search_page.gotos == []


@pytest.mark.asyncio
async def test_verify_check_stops_the_round(tmp_path, monkeypatch):
    pages, _ledger = _harness(tmp_path, monkeypatch)
    pages.search_page.post_payload = {"status_code": 0, "search_nil_type": "verify_check", "aweme_list": []}
    with pytest.raises(DouyinDetailFetchError) as caught:
        await pages.open_note_for_fill(AWEME, need_author=False)
    assert caught.value.runtime_blocking is True
    assert caught.value.code == "captcha_detected"
    assert pages.search_page.gotos == [NOTE]


@pytest.mark.asyncio
async def test_runtime_block_does_not_open_the_next_target(tmp_path, monkeypatch):
    from trippostcollect.platforms.douyin.core import DouYinCrawler

    path = tmp_path / "targets.json"
    path.write_text(json.dumps([
        {
            "platform_post_id": "1",
            "detail_target": "https://www.douyin.com/note/1",
            "missing_fields": ["author_verified"],
        },
        {
            "platform_post_id": "2",
            "detail_target": "https://www.douyin.com/note/2",
            "missing_fields": ["author_verified"],
        },
    ]), encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_FILL_TARGETS", str(path))
    calls: list[str] = []

    class Pages:
        async def open_note_for_fill(self, aweme_id: str, *, need_author: bool) -> object:
            del need_author
            calls.append(aweme_id)
            raise DouyinDetailFetchError(aweme_id, "captcha_detected", runtime_blocking=True, retryable=False)

    saved: list[list[dict]] = []
    crawler = DouYinCrawler.__new__(DouYinCrawler)
    crawler.detail_pages = Pages()
    crawler.ports = SimpleNamespace(write_fill_observations=saved.append)
    with pytest.raises(PlatformRuntimeError) as caught:
        await crawler.fill_missing()
    assert caught.value.code == "captcha_detected"
    assert calls == ["1"]
    assert saved == [[]]


def test_selection_covers_three_gaps_waiver_and_post_id(tmp_path):
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        verified_only = _insert(conn, "101", verified=None, verified_text=None)
        profile_and_verified = _insert(conn, "102", profile="", verified=None, verified_text=None)
        _insert(conn, "103", raw={"kept": True})
        _insert(
            conn, "104",
            profile="https://www.douyin.com/user/full",
            verified=0,
            verified_text=None,
            description="",
            followers=0,
            raw={"content_detail_status": "detail_observed"},
        )
        conn.execute(
            """
            INSERT INTO post_detail_repair_waivers(
                web_post_id, reason, authorized_by, authorized_at
            ) VALUES (?, 'operator', 'owner', '2026-11-26T00:00:00Z')
            """,
            (verified_only,),
        )
        targets, rejected, pending = repair.select_fill_missing_targets(conn, post_ids=[], max_items=0)
        limited, _rejected, _pending = repair.select_fill_missing_targets(
            conn, post_ids=["103"], max_items=0,
        )
    by_id = {item["platform_post_id"]: item["missing_fields"] for item in targets}
    assert pending == 3
    assert by_id["102"] == ["author_profile_url", "author_verified"]
    assert by_id["103"] == ["content_detail_status"]
    assert "104" not in by_id
    assert "101" not in by_id
    assert rejected[0]["reason"] == "post_detail_repair_waived"
    assert [item["platform_post_id"] for item in limited] == ["103"]
    assert profile_and_verified


def test_douyin_flag_matrix(tmp_path, monkeypatch):
    db_path = _db(tmp_path)
    _redirect(monkeypatch, tmp_path)
    cases = [
        (["--platform", "douyin", "--db", str(db_path)], "requires --fill-missing"),
        (["--platform", "douyin", "--fill-missing", "--refresh-all", "--db", str(db_path)], "cannot be combined"),
        (["--platform", "douyin", "--fill-missing", "--dedupe-images", "--db", str(db_path)], "cannot be combined"),
        (["--platform", "douyin", "--fill-missing", "--admit-seen", "--db", str(db_path)], "cannot be combined"),
        (["--platform", "douyin", "--fill-missing", "--keep-stored-images", "--db", str(db_path)], "cannot be combined"),
        (["--platform", "weibo", "--fill-missing", "--db", str(db_path)], "requires --platform douyin"),
    ]
    for argv, message in cases:
        monkeypatch.setattr(sys, "argv", ["repair_post_details.py", *argv])
        with pytest.raises(SystemExit, match=message):
            repair.main()


def test_apply_fills_only_empty_fields_and_keeps_concurrent_values(tmp_path, monkeypatch):
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        post_id = _insert(
            conn,
            AWEME,
            profile="",
            verified=None,
            verified_text="",
            raw={"kept": "原证据", "nickname": "不要靠补缺改掉"},
        )
        conn.execute(
            "INSERT INTO web_post_images(web_post_id, image_index, image_url, image_role) VALUES (?, 0, ?, 'content')",
            (post_id, "https://example.test/a.jpg"),
        )
        conn.commit()
        before = conn.execute("SELECT * FROM web_posts WHERE id=?", (post_id,)).fetchone()
        observation = {
            "platform_post_id": AWEME,
            "kind": "observed",
            "note_visit_id": "ledger:v000001",
            "creator_visit_id": "ledger:v000002",
            "ledger_id": "ledger",
            "author_profile_url": f"https://www.douyin.com/user/{SEC}",
            "author_uid": UID,
            "author_verified": 0,
            "content_detail_observed": True,
            "unresolved_fields": [],
        }

        def allow(*_args, **_kwargs):
            return True

        monkeypatch.setattr(repair, "fill_visit_ok", allow)
        conn.execute("UPDATE web_posts SET author_verified=1 WHERE id=?", (post_id,))
        conn.commit()
        result = repair.apply_fill_missing_row(
            conn,
            web_post_id=post_id,
            missing_fields=["author_profile_url", "author_verified", "content_detail_status"],
            observation=observation,
            run_id="douyin-test",
            observed_at="2026-11-26T12:00:00+08:00",
            ledger_paths=["ledger.jsonl"],
        )
        after = conn.execute("SELECT * FROM web_posts WHERE id=?", (post_id,)).fetchone()
        images = conn.execute("SELECT COUNT(*) FROM web_post_images").fetchone()[0]
    assert result["filled_fields"] == ["author_profile_url", "content_detail_status", "content_detail_source"]
    assert result["already_filled_fields"] == ["author_verified"]
    assert after["author_verified"] == 1
    assert after["author_profile_url"] == f"https://www.douyin.com/user/{SEC}"
    assert after["content_text"] == before["content_text"]
    assert after["title"] == before["title"]
    assert after["published_at"] == before["published_at"]
    assert after["author_followers_count"] == before["author_followers_count"]
    assert after["author_description"] == before["author_description"]
    assert after["keyword"] == before["keyword"]
    assert after["captured_at"] == before["captured_at"]
    assert after["topic_relevant"] == before["topic_relevant"]
    raw = json.loads(after["raw_sample_json"])
    assert raw["kept"] == "原证据"
    assert raw["content_detail_status"] == "detail_observed"
    assert raw["content_detail_source"] == "note_post_list"
    assert raw["fill_missing"]["run_id"] == "douyin-test"
    assert "author_profile_url" not in raw["fill_missing"]
    assert AVATAR not in after["raw_sample_json"]
    assert images == 1


def _bare_row(conn, **values):
    return _insert(conn, AWEME, **values)


def test_author_fields_need_an_author_visit(tmp_path, monkeypatch):
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        post_id = _bare_row(conn, profile="", verified=None, verified_text="")
        conn.commit()
        monkeypatch.setattr(repair, "fill_visit_ok", lambda *_a, **_k: True)
        result = repair.apply_fill_missing_row(
            conn,
            web_post_id=post_id,
            missing_fields=["author_profile_url", "author_verified"],
            observation={
                "kind": "observed",
                "platform_post_id": AWEME,
                "note_visit_id": "v1",
                "ledger_id": "ledger",
                "author_uid": UID,
                "author_profile_url": f"https://www.douyin.com/user/{SEC}",
                "author_verified": 1,
                "author_verified_text": "认证文字",
                "content_detail_observed": True,
                "unresolved_fields": [],
            },
            run_id="douyin-test",
            observed_at="2026-11-26T12:00:00+08:00",
            ledger_paths=["ledger.jsonl"],
        )
        stored = conn.execute(
            "SELECT author_profile_url, author_verified, author_verified_text FROM web_posts WHERE id=?",
            (post_id,),
        ).fetchone()
    assert result["filled_fields"] == []
    assert sorted(result["unresolved_fields"]) == ["author_profile_url", "author_verified"]
    assert stored["author_profile_url"] == ""
    assert stored["author_verified"] is None
    assert stored["author_verified_text"] == ""


def test_verified_text_is_not_added_to_a_row_stored_as_unverified(tmp_path, monkeypatch):
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        post_id = _bare_row(conn, profile="", verified=0, verified_text="")
        conn.commit()
        monkeypatch.setattr(repair, "fill_visit_ok", lambda *_a, **_k: True)
        result = repair.apply_fill_missing_row(
            conn,
            web_post_id=post_id,
            missing_fields=["author_profile_url"],
            observation={
                "kind": "observed",
                "platform_post_id": AWEME,
                "note_visit_id": "v1",
                "creator_visit_id": "v2",
                "ledger_id": "ledger",
                "author_uid": UID,
                "author_profile_url": f"https://www.douyin.com/user/{SEC}",
                "author_verified": 1,
                "author_verified_text": "认证文字",
                "content_detail_observed": True,
                "unresolved_fields": [],
            },
            run_id="douyin-test",
            observed_at="2026-11-26T12:00:00+08:00",
            ledger_paths=["ledger.jsonl"],
        )
        stored = conn.execute(
            "SELECT author_verified, author_verified_text FROM web_posts WHERE id=?", (post_id,),
        ).fetchone()
    assert result["filled_fields"] == ["author_profile_url"]
    assert stored["author_verified"] == 0
    assert stored["author_verified_text"] == ""


def test_null_verification_stays_unresolved(tmp_path, monkeypatch):
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        post_id = _insert(conn, AWEME, verified=None, verified_text=None, profile="https://www.douyin.com/user/kept")
        conn.commit()
        monkeypatch.setattr(repair, "fill_visit_ok", lambda *_a, **_k: True)
        result = repair.apply_fill_missing_row(
            conn,
            web_post_id=post_id,
            missing_fields=["author_verified"],
            observation={
                "kind": "observed",
                "platform_post_id": AWEME,
                "note_visit_id": "v1",
                "creator_visit_id": "v2",
                "ledger_id": "ledger",
                "author_uid": UID,
                "content_detail_observed": True,
                "unresolved_fields": ["author_verified"],
            },
            run_id="douyin-test",
            observed_at="2026-11-26T12:00:00+08:00",
            ledger_paths=["ledger.jsonl"],
        )
        stored = conn.execute("SELECT author_verified FROM web_posts WHERE id=?", (post_id,)).fetchone()
    assert result["filled_fields"] == []
    assert result["unresolved_fields"] == ["author_verified"]
    assert stored["author_verified"] is None
    assert repair.fill_outcome_from_write(result) == ("unresolved", "")


def _child_summary(path: Path, observations: list[dict], *, stop_reason: str = "repair_no_valid_detail") -> dict:
    path.write_text(json.dumps({
        "import_completion_met": False,
        "formal_validation": {"stop_reason": stop_reason, "skipped_candidate_failures": []},
        "records": [{
            "platform": "douyin",
            "fill_observations": observations,
            "output": {"content_visit_paths": []},
        }],
    }), encoding="utf-8")
    return {
        "returncode": 0,
        "stdout": json.dumps({"summary": str(path)}),
        "stderr": "",
        "interrupt_signum": None,
    }


def test_all_unavailable_does_not_report_no_progress(tmp_path, monkeypatch):
    db_path = _db(tmp_path)
    _redirect(monkeypatch, tmp_path)
    with sqlite3.connect(db_path) as conn:
        _insert(conn, "201", verified=None, verified_text=None)
        _insert(conn, "202", raw={})
    summary_path = tmp_path / "child.json"
    child = _child_summary(summary_path, [
        {"platform_post_id": "201", "kind": "unavailable", "error_code": FILL_TARGET_NOT_IN_POST_LIST},
        {"platform_post_id": "202", "kind": "unavailable", "error_code": FILL_TARGET_REDIRECTED,
         "landing_shape": "www.douyin.com/"},
    ])
    monkeypatch.setattr(repair, "run_repair_child", lambda *_a, **_k: child)
    monkeypatch.setattr(
        sys, "argv",
        ["repair_post_details.py", "--platform", "douyin", "--fill-missing", "--db", str(db_path)],
    )
    assert repair.main() == 0
    summaries = list((tmp_path / "out").glob("*/run_summary.json"))
    summary = json.loads(summaries[0].read_text(encoding="utf-8"))
    assert summary["status"] == "completed"
    assert summary["error"] == ""
    assert "post_detail_repair_no_progress" not in json.dumps(summary)
    listed = json.loads(Path(summary["unavailable_ids_path"]).read_text(encoding="utf-8"))
    assert listed == ["201", "202"]


def test_runtime_block_stops_later_batches(tmp_path, monkeypatch):
    db_path = _db(tmp_path)
    _redirect(monkeypatch, tmp_path)
    with sqlite3.connect(db_path) as conn:
        _insert(conn, "301", verified=None, verified_text=None)
        _insert(conn, "302", verified=None, verified_text=None)
    calls = {"n": 0}

    def fake_child(*_args, **_kwargs):
        calls["n"] += 1
        path = tmp_path / f"child-{calls['n']}.json"
        return _child_summary(path, [], stop_reason="captcha_detected")

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    monkeypatch.setattr(
        sys, "argv",
        [
            "repair_post_details.py", "--platform", "douyin", "--fill-missing",
            "--db", str(db_path), "--batch-size", "1",
        ],
    )
    assert repair.main() == 2
    assert calls["n"] == 1
    with sqlite3.connect(db_path) as conn:
        verified = [row[0] for row in conn.execute("SELECT author_verified FROM web_posts ORDER BY id")]
    assert verified == [None, None]


def test_fill_observations_are_really_written_and_read_back(tmp_path, monkeypatch):
    """#223 首次正式运行时写盘崩溃（头像清除的结果对象未取 .value）；这里走真实写盘，不替换写函数。"""

    from trippostcollect.application import repair as app_repair

    observations_path = tmp_path / "logs" / "douyin_fill_observations.json"
    outcomes_path = tmp_path / "logs" / "douyin_repair_outcomes.json"
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_FILL_OBSERVATIONS", str(observations_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_REPAIR_OUTCOMES_PATH", str(outcomes_path))
    app_repair.write_douyin_fill_observations(
        [
            {
                "platform_post_id": AWEME,
                "kind": "observed",
                "note_visit_id": "ledger:v000001",
                "creator_visit_id": "ledger:v000002",
                "ledger_id": "ledger",
                "author_profile_url": f"https://www.douyin.com/user/{SEC}",
                "author_uid": UID,
                "author_verified": 1,
                "author_verified_text": "认证文字",
                "content_detail_observed": True,
                "unresolved_fields": [],
                "avatar_thumb": {"url_list": [AVATAR]},
            },
            {
                "platform_post_id": "7123456789012345679",
                "kind": "unavailable",
                "error_code": "fill_target_not_in_post_list",
            },
        ]
    )
    written = observations_path.read_text(encoding="utf-8")
    assert AVATAR not in written
    loaded = app_repair.load_douyin_fill_observations(observations_path)
    by_id = {item["platform_post_id"]: item for item in loaded}
    assert by_id[AWEME]["author_verified"] == 1
    assert by_id[AWEME]["author_profile_url"] == f"https://www.douyin.com/user/{SEC}"
    outcomes = json.loads(outcomes_path.read_text(encoding="utf-8"))["outcomes"]
    assert [item["error_code"] for item in outcomes] == ["fill_target_not_in_post_list"]


@pytest.mark.asyncio
async def test_fill_round_passes_the_summary_visit_gate(tmp_path, monkeypatch):
    """#223 第二次正式运行：补缺没有正式入库记录，门禁无人引用账本，节奏判失败。

    这里走真实账本与 collection 的门禁入口，不替换门禁或行为校验。
    """

    from trippostcollect.application.collection import summary_content_visits
    from trippostcollect.application.repair import load_douyin_fill_observations, write_douyin_fill_observations

    pages, ledger = _harness(tmp_path, monkeypatch)
    page = pages.search_page
    page.post_payload = _post_list(AWEME, target_index=0)
    page.links = [{"href": f"/user/{SEC}", "visible": True}]
    page.profiles = [(
        200,
        _profile(SEC, text="青岛创作者"),
        "https://www.douyin.com/aweme/v1/web/user/profile/other/",
    )]
    observed = await pages.open_note_for_fill(AWEME, need_author=True)
    assert observed.kind == "observed" and observed.creator_visit_id

    observations_path = tmp_path / "logs" / "douyin_fill_observations.json"
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_FILL_OBSERVATIONS", str(observations_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_REPAIR_OUTCOMES_PATH", str(tmp_path / "logs" / "outcomes.json"))
    write_douyin_fill_observations([
        observed.as_dict(),
        {"platform_post_id": "7123456789012345679", "kind": "unavailable", "error_code": FILL_TARGET_NOT_IN_POST_LIST},
    ])
    summary = {
        "records": [{
            "platform": "douyin",
            "output": {"content_visit_paths": [str(ledger)]},
            "fill_observations": load_douyin_fill_observations(observations_path),
        }]
    }

    gate = summary_content_visits(
        summary, [], ["douyin"], repair_mode=True, stop_event=None, douyin_fill_missing=True
    )["douyin"]
    assert gate["content_visit_ok"] is True
    assert gate["checked_records"] == 1
    assert gate["request_pacing_ok"] is True
    assert gate["continuity_ok"] is True

    # 不带补缺观察时没有门禁记录，不会核验到这次访问。
    before = summary_content_visits(summary, [], ["douyin"], repair_mode=True, stop_event=None)["douyin"]
    assert before["checked_records"] == 0 and before["checked_ledger_count"] == 0

    # 全部不可得时没有写库对象：没有需要追溯的访问，节奏不判失败（#209，操作人 2026-11-27）。
    summary["records"][0]["fill_observations"] = [
        item for item in summary["records"][0]["fill_observations"] if item["kind"] != "observed"
    ]
    empty = summary_content_visits(
        summary, [], ["douyin"], repair_mode=True, stop_event=None, douyin_fill_missing=True
    )["douyin"]
    assert empty["checked_records"] == 0 and empty["request_pacing_ok"] is True
    assert empty["content_visit_ok"] is True
