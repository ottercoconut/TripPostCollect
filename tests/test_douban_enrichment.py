from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

ctf_resource_crawl = import_module("ctf_resource_crawl")
import_ctf_captures = import_module("import_ctf_captures")


TOPIC_URL = "https://www.douban.com/group/topic/123456/"
PROFILE_URL = "https://www.douban.com/people/author-1/"


def topic_html() -> str:
    return f"""
    <h1>青岛旅行记录</h1>
    <span class="from"><a href="{PROFILE_URL}">作者甲</a></span>
    <span class="create-time">2026-07-13 12:00:00</span>
    <a href="https://www.douban.com/group/42/">青岛旅行小组</a>
    <div id="link-report">这里是正文</div><div class="topic-opts-bar"></div>
    """


def write_profile_capture(
    batch_dir: Path,
    topic_dir: Path,
    followers: dict,
    *,
    profile_dir_name: str = "douban-profile",
) -> Path:
    profile_dir = batch_dir / profile_dir_name
    profile_dir.mkdir(parents=True)
    capture_meta = profile_dir / "capture_meta.json"
    capture_meta.write_text(
        json.dumps(
            {
                "site": "douban_group",
                "url": PROFILE_URL,
                "ok": True,
                "capture_role": "conditional_enrichment",
                "artifact_dir": str(profile_dir),
                "conditional_enrichment_for": {
                    "parent_url": TOPIC_URL,
                    "parent_artifact_dir": str(topic_dir),
                },
                "douban_people_followers": followers,
            }
        ),
        encoding="utf-8",
    )
    return capture_meta


def topic_meta(topic_dir: Path, profile_capture_meta: Path) -> dict:
    return {
        "site": "douban_group",
        "url": TOPIC_URL,
        "ok": True,
        "artifact_dir": str(topic_dir),
        "keyword": "青岛旅游",
        "conditional_enrichment": {
            "kind": "douban_author_followers",
            "triggered": True,
            "status": "observed",
            "author_profile_url": PROFILE_URL,
            "capture_meta_path": str(profile_capture_meta),
        },
    }


def test_extract_douban_people_followers_requires_visible_evidence() -> None:
    observed = ctf_resource_crawl.extract_douban_people_followers(
        '<p class="rev-link"><a href="/people/author-1/rev_contacts">作者甲被1.2万人关注</a></p>',
        "作者甲",
        profile_url=PROFILE_URL,
    )
    explicit_zero = ctf_resource_crawl.extract_douban_people_followers(
        '<a href="/people/author-1/rev_contacts">被0人关注</a>',
        "作者甲",
        profile_url=PROFILE_URL,
    )
    privacy = ctf_resource_crawl.extract_douban_people_followers(
        "<script>该账号处于异常状态</script>",
        "由于用户的设置，无法查看主页内容",
        profile_url=PROFILE_URL,
    )
    missing = ctf_resource_crawl.extract_douban_people_followers(
        "<script>该账号处于异常状态</script>",
        "作者甲的主页",
        profile_url=PROFILE_URL,
    )

    assert observed["followers_count"] == 12_000
    assert observed["followers_observed"] is True
    assert observed["followers_source"] == "people_page"
    assert explicit_zero["followers_count"] == 0
    assert explicit_zero["followers_observed"] is True
    assert privacy["status"] == "privacy_restricted"
    assert privacy["followers_source"] == "privacy_restricted"
    assert missing["status"] == "not_observed"
    assert missing["followers_source"] is None


def test_conditional_enrichment_uses_current_page_evidence_chain(tmp_path: Path, monkeypatch) -> None:
    batch_dir = tmp_path / "batch"
    topic_dir = batch_dir / "douban-topic"
    profile_dir = batch_dir / "douban-profile"
    topic_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    rendered_html = topic_dir / "rendered.html"
    rendered_html.write_text(topic_html(), encoding="utf-8")
    primary = {
        "site": "douban_group",
        "url": TOPIC_URL,
        "ok": True,
        "artifact_dir": str(topic_dir),
        "artifacts": {"rendered_html": str(rendered_html)},
        "navigation": {},
    }
    calls: list[dict] = []

    async def fake_crawl_one(playwright, target, output_dir, args):
        calls.append(target)
        return {
            "site": "douban_group",
            "url": PROFILE_URL,
            "ok": True,
            "capture_role": "conditional_enrichment",
            "artifact_dir": str(profile_dir),
            "douban_people_followers": {
                "status": "observed",
                "followers_count": 321,
                "followers_observed": True,
                "followers_source": "people_page",
                "evidence": {"raw_text": "被321人关注"},
            },
        }

    monkeypatch.setattr(ctf_resource_crawl, "crawl_one", fake_crawl_one)
    result = asyncio.run(
        ctf_resource_crawl.run_douban_conditional_enrichment(
            object(),
            {"site": "douban_group", "url": TOPIC_URL, "configured": True, "mobile": False},
            primary,
            batch_dir,
            SimpleNamespace(),
        )
    )

    assert result is not None
    assert len(calls) == 1
    assert calls[0]["url"] == PROFILE_URL
    assert calls[0]["capture_role"] == "conditional_enrichment"
    assert primary["conditional_enrichment"]["followers_count"] == 321
    assert primary["navigation"]["enrichment"]["followers_observed"] is True
    persisted = json.loads((topic_dir / "capture_meta.json").read_text(encoding="utf-8"))
    assert persisted["conditional_enrichment"]["capture_meta_path"] == str(profile_dir / "capture_meta.json")


def test_import_maps_same_run_observed_followers_with_traceable_evidence(tmp_path: Path) -> None:
    batch_dir = tmp_path / "batch"
    topic_dir = batch_dir / "douban-topic"
    topic_dir.mkdir(parents=True)
    rendered_html = topic_dir / "rendered.html"
    visible_text = topic_dir / "visible_text.txt"
    rendered_html.write_text(topic_html(), encoding="utf-8")
    visible_text.write_text("这里是正文", encoding="utf-8")
    profile_capture_meta = write_profile_capture(
        batch_dir,
        topic_dir,
        {
            "status": "observed",
            "followers_count": 456,
            "followers_observed": True,
            "followers_source": "people_page",
            "evidence": {"raw_text": "被456人关注"},
        },
    )
    raw_meta = topic_meta(topic_dir, profile_capture_meta)
    row = {
        "site_key": "douban_group",
        "target_url": TOPIC_URL,
        "final_url": TOPIC_URL,
        "title": "青岛旅行记录",
        "published_at": None,
        "ok": 1,
        "content_ready": 1,
        "visible_text_path": str(visible_text),
        "rendered_html_path": str(rendered_html),
        "raw_meta_json": json.dumps(raw_meta),
        "capture_kind": "resource",
        "body_text_length": 10,
        "root_text_length": 10,
        "image_count": 0,
        "loaded_image_count": 0,
        "flag_count": 0,
        "saved_images": 0,
        "captured_at": "2026-07-14T10:00:00+00:00",
        "artifact_dir": str(topic_dir),
    }

    post = import_ctf_captures.web_post_for_capture(row, capture_id=1)

    assert post is not None
    assert post["author_followers_count"] == 456
    author = json.loads(post["author_json"])
    metrics = json.loads(post["metrics_json"])
    assert author["followers_observed"] is True
    assert author["followers_source"] == "people_page"
    assert author["followers_evidence"]["same_run"] is True
    assert metrics["followers_source"] == "people_page"

    db_path = tmp_path / "douban-enrichment.sqlite"
    with sqlite3.connect(db_path) as conn:
        import_ctf_captures.ensure_schema(conn)
        import_ctf_captures.upsert_web_post_from_capture(conn, {**post, "source_capture_id": None})
        stored = conn.execute(
            """
            SELECT author_followers_count,
                   json_extract(author_json, '$.followers_observed'),
                   json_extract(author_json, '$.followers_source')
            FROM web_posts
            WHERE platform_key='douban_group'
            """
        ).fetchone()
    assert stored == (456, 1, "people_page")


def test_import_reports_inserted_and_updated_rows(tmp_path: Path, monkeypatch, capsys) -> None:
    batch_dir = tmp_path / "batch"
    topic_dir = batch_dir / "douban-topic"
    topic_dir.mkdir(parents=True)
    rendered_html = topic_dir / "rendered.html"
    visible_text = topic_dir / "visible_text.txt"
    images_json = topic_dir / "images.json"
    failed_images_json = topic_dir / "failed_images.json"
    rendered_html.write_text(topic_html(), encoding="utf-8")
    visible_text.write_text("这里是正文", encoding="utf-8")
    images_json.write_text("[]", encoding="utf-8")
    failed_images_json.write_text("[]", encoding="utf-8")
    profile_capture_meta = write_profile_capture(
        batch_dir,
        topic_dir,
        {
            "status": "observed",
            "followers_count": 12,
            "followers_observed": True,
            "followers_source": "people_page",
            "evidence": {"raw_text": "被12人关注"},
        },
    )
    capture_meta = topic_dir / "capture_meta.json"
    capture_meta.write_text(
        json.dumps(
            {
                **topic_meta(topic_dir, profile_capture_meta),
                "navigation": {"content_ready": True, "final_url": TOPIC_URL},
                "image_summary": {
                    "total_requests": 0,
                    "successful_responses": 0,
                    "http_failed_responses": 0,
                    "request_failed": 0,
                    "saved_images": 0,
                },
                "artifacts": {
                    "rendered_html": str(rendered_html),
                    "visible_text": str(visible_text),
                    "images_json": str(images_json),
                    "failed_images_json": str(failed_images_json),
                },
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "import.sqlite"
    argv = [
        "import_ctf_captures.py",
        "--db",
        str(db_path),
        "--capture-meta",
        str(capture_meta),
    ]

    monkeypatch.setattr(sys, "argv", argv)
    assert import_ctf_captures.main() == 0
    first = json.loads(capsys.readouterr().out)
    assert first["inserted_rows"] == 1
    assert first["updated_rows"] == 0

    monkeypatch.setattr(sys, "argv", argv)
    assert import_ctf_captures.main() == 0
    second = json.loads(capsys.readouterr().out)
    assert second["inserted_rows"] == 0
    assert second["updated_rows"] == 1


def test_import_does_not_mislabel_unproven_privacy_or_reuse_old_capture(tmp_path: Path) -> None:
    batch_dir = tmp_path / "batch"
    topic_dir = batch_dir / "douban-topic"
    topic_dir.mkdir(parents=True)
    old_capture = write_profile_capture(
        tmp_path / "old-batch",
        topic_dir,
        {
            "status": "privacy_restricted",
            "followers_count": None,
            "followers_observed": False,
            "followers_source": "privacy_restricted",
            "evidence": {"privacy_marker": "不是当前页面可见的隐私文案"},
        },
    )
    old_result = import_ctf_captures.extract_douban_followers_enrichment(
        topic_meta(topic_dir, old_capture),
        str(topic_dir),
    )

    current_capture = write_profile_capture(
        batch_dir,
        topic_dir,
        {
            "status": "privacy_restricted",
            "followers_count": None,
            "followers_observed": False,
            "followers_source": "privacy_restricted",
            "evidence": {"privacy_marker": "不是当前页面可见的隐私文案"},
        },
        profile_dir_name="douban-current-profile",
    )
    current_result = import_ctf_captures.extract_douban_followers_enrichment(
        topic_meta(topic_dir, current_capture),
        str(topic_dir),
    )

    assert old_result["followers_count"] is None
    assert old_result["followers_source"] is None
    assert old_result["evidence"]["reason"] == "conditional_enrichment_capture_not_from_same_run"
    assert current_result["followers_count"] is None
    assert current_result["followers_source"] is None
