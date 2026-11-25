"""微博 repair 复用旧正文图：不访问平台、不打开真实库。"""

from __future__ import annotations

import errno
import io
import json
import os
import sqlite3
import sys
from hashlib import sha256
from pathlib import Path

from PIL import Image
import pytest

from support.weibo_adapter import crawler as make_crawler, settings
from trippostcollect.application.repair import (
    load_stored_content_images,
    reject_keep_stored_images_scope,
)
from trippostcollect.application.visit_gate import validate_content_visits
from trippostcollect.artifacts.image_manifest import ImageManifestEntry, write_manifest_atomic
from trippostcollect.artifacts.formal_images import rollback_newly_promoted_images
from trippostcollect.artifacts.image_materialization import MaterializedImage
from trippostcollect.artifacts.image_persistence import (
    ImagePersistenceError,
    prepare_image_rows,
)
from trippostcollect.artifacts.image_staging import ImageAsset, stage_post_images
from trippostcollect.core import paths as core_paths
from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.db.content import upsert_web_post
from trippostcollect.platforms.weibo.client import WEIBO_STORED_IMAGE_RULE
from trippostcollect.platforms.weibo.search_page import WeiboPageCapture
from trippostcollect.runtime.visit_ledger import VisitLedger
from trippostcollect.xhs.batch_checkpoint import verify_snapshot_artifacts

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import repair_post_details as repair  # noqa: E402


def png_bytes(color: tuple[int, int, int] = (10, 20, 30), size: tuple[int, int] = (4, 3)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color=color).save(output, format="PNG")
    return output.getvalue()


def digest(content: bytes) -> str:
    return sha256(content).hexdigest()


def plant_media(project: Path, relative: str, content: bytes) -> Path:
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    (project / "data" / "media").mkdir(parents=True, exist_ok=True)
    return path


def inventory_entry(post_id: str, key: str, relative: str, content: bytes) -> dict:
    return {
        "platform_post_id": post_id,
        "source_asset_key": key,
        "sha256": digest(content),
        "local_path": relative,
        "mime_type": "image/png",
        "width": 4,
        "height": 3,
    }


def write_inventory(path: Path, rows: list[dict]) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def asset(pid: str, index: int, url: str | None = None) -> dict:
    image_url = url or f"https://wx1.sinaimg.cn/large/{pid}.jpg"
    return {
        "pid": pid,
        "url": image_url,
        "urls": [image_url],
        "source_index": index,
    }


class RecordingClient:
    def __init__(self, content: bytes):
        self.content = content
        self.calls: list[list[str]] = []

    async def get_note_image(self, urls):
        self.calls.append(list(urls))
        return self.content


def manifest_rows(save_root: Path) -> list[dict]:
    path = save_root / "weibo" / "image_manifest.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def visit_lines(ledger: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("kind") == "visit"
    ]


def _insert_post(conn: sqlite3.Connection, post_id: str) -> int:
    url = f"https://weibo.com/88/{post_id}"
    conn.execute(
        """
        INSERT INTO web_posts(
            platform_key, platform_post_id, source_type, source_url, canonical_url,
            captured_at, keyword, topic_relevant, content_text, post_likes_count,
            raw_sample_json
        ) VALUES ('weibo', ?, 'import', ?, ?, '2026-11-01T00:00:00+08:00', '青岛旅游', 1, '旧', 1, '{}')
        """,
        (post_id, url, url),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def test_load_stored_images_keeps_duplicate_keys_and_drops_conflicting_sha(tmp_path: Path) -> None:
    db_path = tmp_path / "repair.sqlite"
    bootstrap_database(db_path, sync_jobs=False)
    content = png_bytes()
    relative = "data/media/weibo/100/000-aaaaaaaaaaaaaaaa.png"
    other = png_bytes((1, 2, 3))
    with sqlite3.connect(db_path) as conn:
        post_id = _insert_post(conn, "100")
        raw = {
            "source_asset_key": "weibo:pid:keep",
            "url": "https://wx1.sinaimg.cn/large/keep.jpg",
            "local_file": {
                "source": "formal_image_materialization_v1",
                "source_url": "https://wx1.sinaimg.cn/large/old.jpg",
                "size_bytes": len(content),
                "manifest_path": "outputs/old/image_manifest.jsonl",
                "manifest_line": 4,
                "sha256_duplicate_sources": [
                    {"source_asset_key": "weibo:pid:dup", "source_url": "https://wx1.sinaimg.cn/large/dup.jpg"}
                ],
            },
        }
        conn.execute(
            """
            INSERT INTO web_post_images(
                web_post_id, image_index, image_url, image_role, local_path,
                width, height, mime_type, sha256, raw_image_json
            ) VALUES (?, 0, 'https://wx1.sinaimg.cn/large/keep.jpg', 'content', ?, 4, 3, 'image/png', ?, ?)
            """,
            (post_id, relative, digest(content), json.dumps(raw, ensure_ascii=False)),
        )
        conflict_a = {"source_asset_key": "weibo:pid:split", "url": "https://wx1.sinaimg.cn/large/a.jpg"}
        conflict_b = {"source_asset_key": "weibo:pid:split", "url": "https://wx1.sinaimg.cn/large/b.jpg"}
        conn.execute(
            """
            INSERT INTO web_post_images(
                web_post_id, image_index, image_url, image_role, local_path,
                width, height, mime_type, sha256, raw_image_json
            ) VALUES (?, 1, 'https://wx1.sinaimg.cn/large/a.jpg', 'content', ?, 4, 3, 'image/png', ?, ?)
            """,
            (post_id, relative, digest(content), json.dumps(conflict_a)),
        )
        conn.execute(
            """
            INSERT INTO web_post_images(
                web_post_id, image_index, image_url, image_role, local_path,
                width, height, mime_type, sha256, raw_image_json
            ) VALUES (?, 2, 'https://wx1.sinaimg.cn/large/b.jpg', 'content', ?, 4, 3, 'image/png', ?, ?)
            """,
            (post_id, relative + ".b", digest(other), json.dumps(conflict_b)),
        )
        conn.execute(
            """
            INSERT INTO web_post_images(
                web_post_id, image_index, image_url, image_role, local_path,
                width, height, mime_type, sha256, raw_image_json
            ) VALUES (?, 3, 'https://wx1.sinaimg.cn/large/page.jpg', 'page', ?, 4, 3, 'image/png', ?, '{}')
            """,
            (post_id, relative, digest(content)),
        )
        conn.commit()
    rows = load_stored_content_images(db_path, "weibo", {"100"})
    keys = {row["source_asset_key"] for row in rows}
    assert keys == {"weibo:pid:keep", "weibo:pid:dup"}
    assert all(row["sha256"] == digest(content) and row["local_path"] == relative for row in rows)
    assert "weibo:pid:split" not in keys


@pytest.mark.asyncio
async def test_detail_reuses_pid_images_and_still_downloads_the_rest(monkeypatch, tmp_path: Path) -> None:
    content = png_bytes()
    fresh = png_bytes((9, 9, 9))
    project_relative = "data/media/weibo/note-1/000-aaaaaaaaaaaaaaaa.png"
    kept = inventory_entry("note-1", "weibo:pid:kept", project_relative, content)
    duplicate = inventory_entry("note-1", "weibo:pid:dup", project_relative, content)
    ignored = inventory_entry("note-1", "weibo:urlsha256:" + "ab" * 32, project_relative, content)
    record = {
        "note_id": "note-1",
        "image_assets": [
            asset("new", 0),
            asset("kept", 1, "https://wx2.sinaimg.cn/large/kept-new.jpg"),
            asset("dup", 2, "https://wx3.sinaimg.cn/large/dup-new.jpg"),
            {"url": "https://wx4.sinaimg.cn/large/hashed.jpg", "urls": ["https://wx4.sinaimg.cn/large/hashed.jpg"], "source_index": 3},
        ],
        "content_visit_refs": {"images": []},
    }
    # 先把文件按清单 sha 写好。fetch_images 里的 plant 对非默认 sha 会写错字节，这里自己种。
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", project)
    monkeypatch.setattr(core_paths, "LOCAL_MEDIA_ROOT", project / "data" / "media")
    plant_media(project, project_relative, content)
    inventory_path = tmp_path / "stored.json"
    write_inventory(inventory_path, [kept, duplicate, ignored])
    monkeypatch.setenv("TRIPPOSTCOLLECT_WEIBO_STORED_IMAGES", str(inventory_path))
    evidence = tmp_path / "behavior.jsonl"
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(evidence))
    save_root = tmp_path / "save"
    crawler = make_crawler(settings(
        ENABLE_GET_MEIDAS=True,
        SAVE_DATA_PATH=str(save_root),
        CRAWLER_TYPE="detail",
        CRAWLER_MAX_SLEEP_SEC=0,
    ))
    client = RecordingClient(fresh)
    crawler.wb_client = client
    crawler._capture = WeiboPageCapture.open(object(), mode="repair")
    await crawler.get_note_images(record)

    assert [call[0] for call in client.calls] == [
        "https://wx1.sinaimg.cn/large/new.jpg",
        "https://wx4.sinaimg.cn/large/hashed.jpg",
    ]
    visits = visit_lines(evidence.with_name("behavior.content_visits.jsonl"))
    by_id = {row["id"]: row for row in visits}
    ordered = [by_id[item] for item in record["content_visit_refs"]["images"]]
    assert [(row["source"], row["status"], row["rule"], row["trigger"]) for row in ordered] == [
        ("cdn_direct", 200, "weibo.content_image", None),
        ("local_media", None, WEIBO_STORED_IMAGE_RULE, None),
        ("local_media", None, WEIBO_STORED_IMAGE_RULE, None),
        ("cdn_direct", 200, "weibo.content_image", None),
    ]
    assert [row["asset_key"] for row in ordered] == [
        "weibo:pid:new", "weibo:pid:kept", "weibo:pid:dup",
        ordered[3]["asset_key"],
    ]
    assert ordered[3]["asset_key"].startswith("weibo:urlsha256:")
    rows = manifest_rows(save_root)
    assert [(row["source_index"], row["fetch_status"], row["attempts"], row["http_status"]) for row in rows] == [
        (0, "downloaded", 1, 200),
        (1, "reused", 0, None),
        (2, "reused", 0, None),
        (3, "downloaded", 1, 200),
    ]
    assert rows[1]["source_url"] == "https://wx2.sinaimg.cn/large/kept-new.jpg"
    reused_file = save_root / rows[1]["staging_path"]
    assert reused_file.stat().st_ino == (project / project_relative).stat().st_ino


@pytest.mark.asyncio
async def test_stored_image_mismatches_fall_back_to_download(monkeypatch, tmp_path: Path, caplog) -> None:
    content = png_bytes()
    project = tmp_path / "project"
    project.mkdir()
    media = project / "data" / "media"
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", project)
    monkeypatch.setattr(core_paths, "LOCAL_MEDIA_ROOT", media)
    good = "data/media/weibo/note-1/000-aaaaaaaaaaaaaaaa.png"
    plant_media(project, good, content)
    outside = project / "outside.png"
    outside.write_bytes(content)
    linked = media / "weibo" / "link.png"
    linked.parent.mkdir(parents=True, exist_ok=True)
    linked.symlink_to(outside)
    missing = "data/media/weibo/note-1/missing.png"
    wrong = "data/media/weibo/note-1/wrong.png"
    plant_media(project, wrong, png_bytes((2, 2, 2)))
    escape = "data/media/../../outside.png"
    rows = [
        inventory_entry("note-1", "weibo:pid:missing", missing, content),
        inventory_entry("note-1", "weibo:pid:sha", wrong, content),
        {"platform_post_id": "note-1", "source_asset_key": "weibo:pid:escape", "sha256": digest(content),
         "local_path": escape, "mime_type": "image/png", "width": 4, "height": 3},
        {"platform_post_id": "note-1", "source_asset_key": "weibo:pid:link", "sha256": digest(content),
         "local_path": "data/media/weibo/link.png", "mime_type": "image/png", "width": 4, "height": 3},
        {**inventory_entry("note-1", "weibo:pid:wide", good, content), "width": 9},
        inventory_entry("note-1", "weibo:urlsha256:" + "cd" * 32, good, content),
    ]
    record = {
        "note_id": "note-1",
        "image_assets": [
            asset("missing", 0),
            asset("sha", 1),
            asset("escape", 2),
            asset("link", 3),
            {"url": "https://wx1.sinaimg.cn/large/plain.jpg", "urls": ["https://wx1.sinaimg.cn/large/plain.jpg"], "source_index": 4},
            asset("wide", 5),
        ],
        "content_visit_refs": {"images": []},
    }
    inventory_path = tmp_path / "stored.json"
    write_inventory(inventory_path, rows)
    monkeypatch.setenv("TRIPPOSTCOLLECT_WEIBO_STORED_IMAGES", str(inventory_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(tmp_path / "behavior.jsonl"))
    crawler = make_crawler(settings(
        ENABLE_GET_MEIDAS=True, SAVE_DATA_PATH=str(tmp_path / "save"),
        CRAWLER_TYPE="detail", CRAWLER_MAX_SLEEP_SEC=0,
    ))
    client = RecordingClient(content)
    crawler.wb_client = client
    crawler._capture = WeiboPageCapture.open(object(), mode="repair")
    with caplog.at_level("WARNING"):
        await crawler.get_note_images(record)
    assert len(client.calls) == 6
    assert all(row["fetch_status"] == "downloaded" for row in manifest_rows(tmp_path / "save"))
    reasons = " ".join(caplog.messages)
    assert "missing_file" in reasons
    assert "sha256_mismatch" in reasons
    assert "path_escape" in reasons
    assert "symlink" in reasons
    assert "dimension_mismatch" in reasons
    assert "http" not in reasons
    assert "sinaimg" not in reasons


@pytest.mark.asyncio
async def test_search_mode_and_missing_inventory_download_every_image(monkeypatch, tmp_path: Path) -> None:
    content = png_bytes()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", project)
    monkeypatch.setattr(core_paths, "LOCAL_MEDIA_ROOT", project / "data" / "media")
    relative = "data/media/weibo/note-1/000-aaaaaaaaaaaaaaaa.png"
    plant_media(project, relative, content)
    inventory_path = tmp_path / "stored.json"
    write_inventory(inventory_path, [inventory_entry("note-1", "weibo:pid:kept", relative, content)])
    monkeypatch.setenv("TRIPPOSTCOLLECT_WEIBO_STORED_IMAGES", str(inventory_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(tmp_path / "behavior.jsonl"))
    crawler = make_crawler(settings(
        ENABLE_GET_MEIDAS=True, SAVE_DATA_PATH=str(tmp_path / "save"),
        CRAWLER_TYPE="search", CRAWLER_MAX_SLEEP_SEC=0,
    ))
    client = RecordingClient(content)
    crawler.wb_client = client
    crawler._capture = WeiboPageCapture.open(object())
    await crawler.get_note_images({"note_id": "note-1", "image_assets": [asset("kept", 0)], "content_visit_refs": {}})
    assert len(client.calls) == 1
    assert manifest_rows(tmp_path / "save")[0]["fetch_status"] == "downloaded"


def test_stage_hardlink_falls_back_when_link_raises_exdev(monkeypatch, tmp_path: Path) -> None:
    content = png_bytes()
    source = tmp_path / "long.png"
    source.write_bytes(content)
    asset_row = ImageAsset(
        source_index=0,
        source_asset_key="weibo:pid:kept",
        source_url="https://wx1.sinaimg.cn/large/kept.jpg",
        content=content,
        attempts=0,
        http_status=None,
        stored_source_path=str(source),
    )

    def boom(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "cross")

    monkeypatch.setattr(os, "link", boom)
    rows = stage_post_images(
        save_data_root=tmp_path / "save",
        platform_storage_key="weibo",
        platform_key="weibo",
        platform_post_id="note-1",
        source_key="image_list",
        assets=[asset_row],
    )
    staged = tmp_path / "save" / rows[0]["staging_path"]
    assert rows[0]["fetch_status"] == "reused"
    assert staged.read_bytes() == content
    assert staged.stat().st_ino != source.stat().st_ino


def test_promote_reused_does_not_replace_existing_file_and_rollback_keeps_it(tmp_path: Path) -> None:
    project = tmp_path / "project"
    media = project / "data" / "media"
    relative = "data/media/weibo/note-1/kept.png"
    path = plant_media(project, relative, png_bytes())
    before = path.stat().st_ino
    image = MaterializedImage(
        platform_key="weibo",
        platform_post_id="note-1",
        image_role="content",
        source_index=0,
        source_key="image_list",
        source_asset_key="weibo:pid:kept",
        source_url="https://wx1.sinaimg.cn/large/kept.jpg",
        local_path=relative,
        size_bytes=path.stat().st_size,
        mime_type="image/png",
        width=4,
        height=3,
        sha256=digest(path.read_bytes()),
        reused=True,
        fetch_status="reused",
    )
    removed = rollback_newly_promoted_images(
        {"weibo:id:note-1": [image]},
        project_root=project,
        media_root=media,
    )
    assert removed == 0
    assert path.stat().st_ino == before
    fresh = MaterializedImage(
        platform_key="weibo",
        platform_post_id="note-1",
        image_role="content",
        source_index=1,
        source_key="image_list",
        source_asset_key="weibo:pid:new",
        source_url="https://wx1.sinaimg.cn/large/new.jpg",
        local_path="data/media/weibo/note-1/new.png",
        size_bytes=3,
        mime_type="image/png",
        width=4,
        height=3,
        sha256="ab" * 32,
        reused=False,
    )
    created = plant_media(project, fresh.local_path, b"new")
    removed = rollback_newly_promoted_images(
        {"weibo:id:note-1": [fresh]},
        project_root=project,
        media_root=media,
    )
    assert removed == 1
    assert not created.exists()
    assert path.exists()


def test_upsert_refreshes_text_metrics_and_keeps_prior_local_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    content = png_bytes()
    relative = "data/media/weibo/100/000-aaaaaaaaaaaaaaaa.png"
    plant_media(project, relative, content)
    media = project / "data" / "media"
    db_path = tmp_path / "posts.sqlite"
    bootstrap_database(db_path, sync_jobs=False)
    old_file = {
        "source": "formal_image_materialization_v1",
        "source_url": "https://wx1.sinaimg.cn/large/old.jpg",
        "size_bytes": len(content),
        "manifest_path": "outputs/old/image_manifest.jsonl",
        "manifest_line": 3,
    }
    new_file = {
        "source": "formal_image_materialization_v1",
        "source_url": "https://wx1.sinaimg.cn/large/new.jpg",
        "size_bytes": len(content),
        "manifest_path": "outputs/batch/image_manifest.jsonl",
        "manifest_line": 1,
    }

    def image_item(url: str, local_file: dict, *, reused: bool) -> dict:
        item = {
            "role": "content",
            "source_index": 0,
            "url": url,
            "source_asset_key": "weibo:pid:kept",
            "local_path": relative,
            "width": 4,
            "height": 3,
            "mime_type": "image/png",
            "sha256": digest(content),
            "local_file": local_file,
        }
        if reused:
            item["fetch_status"] = "reused"
        return item

    def post_row(text: str, likes: int, item: dict) -> dict:
        return {
            "platform_key": "weibo",
            "platform_post_id": "100",
            "source_type": "import",
            "source_url": "https://weibo.com/88/100",
            "canonical_url": "https://weibo.com/88/100",
            "captured_at": "2026-11-01T00:00:00+08:00",
            "keyword": "青岛旅游",
            "topic_relevant": 1,
            "content_text": text,
            "post_likes_count": likes,
            "post_comments_count": 4,
            "post_shares_count": 5,
            "post_images_count": 1,
            "raw_sample_json": "{}",
            "_image_items": [item],
        }

    with sqlite3.connect(db_path) as conn:
        upsert_web_post(
            conn, post_row("旧正文", 1, image_item("https://wx1.sinaimg.cn/large/old.jpg", old_file, reused=False)),
            project_root=project, media_root=media, require_local_images=True,
        )
        upsert_web_post(
            conn, post_row("新正文", 9, image_item("https://wx2.sinaimg.cn/large/new.jpg", new_file, reused=True)),
            project_root=project, media_root=media, require_local_images=True,
        )
        text, likes, image_url, image_sha, local_path, raw_json = conn.execute(
            """
            SELECT p.content_text, p.post_likes_count, i.image_url, i.sha256, i.local_path, i.raw_image_json
            FROM web_posts p JOIN web_post_images i ON i.web_post_id=p.id
            """
        ).fetchone()
    assert (text, likes, image_url, image_sha, local_path) == (
        "新正文", 9, "https://wx2.sinaimg.cn/large/new.jpg", digest(content), relative,
    )
    assert json.loads(raw_json)["local_file"]["manifest_path"] == "outputs/old/image_manifest.jsonl"
    assert json.loads(raw_json)["url"] == "https://wx2.sinaimg.cn/large/new.jpg"


def test_prepare_rows_keeps_prior_local_file_and_updates_url(tmp_path: Path) -> None:
    project = tmp_path / "project"
    content = png_bytes()
    relative = "data/media/weibo/100/000-aaaaaaaaaaaaaaaa.png"
    plant_media(project, relative, content)
    media = project / "data" / "media"
    old_file = {
        "source": "formal_image_materialization_v1",
        "source_url": "https://wx1.sinaimg.cn/large/old.jpg",
        "size_bytes": len(content),
        "manifest_path": "outputs/old/image_manifest.jsonl",
        "manifest_line": 7,
        "sha256_duplicate_sources": [{"source_asset_key": "weibo:pid:stale"}],
    }
    new_file = {
        "source": "formal_image_materialization_v1",
        "source_url": "https://wx1.sinaimg.cn/large/new.jpg",
        "size_bytes": len(content),
        "manifest_path": "outputs/batch/image_manifest.jsonl",
        "manifest_line": 1,
        "sha256_duplicate_sources": [{"source_asset_key": "weibo:pid:dup", "source_url": "https://wx1.sinaimg.cn/large/dup.jpg"}],
    }
    item = {
        "role": "content",
        "source_index": 0,
        "url": "https://wx1.sinaimg.cn/large/new.jpg",
        "source_asset_key": "weibo:pid:kept",
        "local_path": relative,
        "width": 4,
        "height": 3,
        "mime_type": "image/png",
        "sha256": digest(content),
        "local_file": new_file,
        "fetch_status": "reused",
    }
    existing = [{
        "id": 1,
        "role": "content",
        "source_index": 0,
        "url": "https://wx1.sinaimg.cn/large/old.jpg",
        "source_asset_key": "weibo:pid:kept",
        "sha256": digest(content),
        "local_path": relative,
        "local_file": old_file,
    }]
    prepared = prepare_image_rows(
        "weibo", [item], existing, project_root=project, media_root=media, require_local_images=True,
    )
    raw = json.loads(prepared[0]["raw_image_json"])
    assert prepared[0]["url"] == "https://wx1.sinaimg.cn/large/new.jpg"
    assert raw["url"] == "https://wx1.sinaimg.cn/large/new.jpg"
    assert raw["local_file"]["manifest_path"] == "outputs/old/image_manifest.jsonl"
    assert raw["local_file"]["manifest_line"] == 7
    assert raw["local_file"]["source_url"] == "https://wx1.sinaimg.cn/large/old.jpg"
    assert raw["local_file"]["size_bytes"] == len(content)
    assert raw["local_file"]["sha256_duplicate_sources"] == new_file["sha256_duplicate_sources"]
    assert "stale" not in json.dumps(raw["local_file"])

    bare = {**existing[0], "local_file": None}
    kept_new = prepare_image_rows(
        "weibo", [{**item, "local_file": dict(new_file)}], [bare],
        project_root=project, media_root=media, require_local_images=True,
    )
    assert json.loads(kept_new[0]["raw_image_json"])["local_file"]["manifest_path"] == "outputs/batch/image_manifest.jsonl"

    mismatch = {**existing[0], "sha256": "ff" * 32}
    with pytest.raises(ImagePersistenceError, match="sha256"):
        prepare_image_rows(
            "weibo", [dict(item)], [mismatch],
            project_root=project, media_root=media, require_local_images=True,
        )


def _manifest_entry(*, platform: str, status: str, attempts: int, http_status: int | None, digest_value: str) -> ImageManifestEntry:
    return ImageManifestEntry(
        schema_version=1, platform_key=platform, platform_post_id="525", image_role="content",
        source_index=0, source_key="image_list", source_asset_key=f"{platform}:pid:pid-1",
        source_url="https://wx1.sinaimg.cn/large/pid-1.jpg", fetch_status=status, attempts=attempts,
        http_status=http_status, staging_path=f"{platform}/images/525/000.jpg", size_bytes=11,
        mime_type="image/jpeg", width=8, height=5, sha256=digest_value, error_code=None,
    )


@pytest.mark.parametrize("platform", ["bilibili", "xhs", "zhihu", "douyin"])
def test_other_platforms_reject_reused_manifest_rows(platform: str) -> None:
    source_key = {"bilibili": "image_urls", "douyin": "note_download_url"}.get(platform, "image_list")
    with pytest.raises(Exception, match="reused"):
        ImageManifestEntry(
            schema_version=1, platform_key=platform, platform_post_id="525", image_role="content",
            source_index=0, source_key=source_key, source_asset_key=f"{platform}:pid:pid-1",
            source_url="https://cdn.example.test/pid-1.jpg", fetch_status="reused", attempts=0,
            http_status=None, staging_path=f"{platform}/images/525/000.jpg", size_bytes=11,
            mime_type="image/jpeg", width=8, height=5, sha256="ab" * 32, error_code=None,
        )


def test_xhs_checkpoint_rejects_parsed_reused_row(tmp_path: Path) -> None:
    manifest = tmp_path / "image_manifest.jsonl"
    write_manifest_atomic(manifest, [_manifest_entry(
        platform="weibo", status="reused", attempts=0, http_status=None, digest_value="ab" * 32,
    )])
    file_sha = sha256(manifest.read_bytes()).hexdigest()
    summary = {
        "batch_checkpoint": {"files": [{"path": str(manifest.resolve()), "sha256": file_sha}]},
        "records": [{
            "output": {
                "jsonl_files": [],
                "image_manifest_paths": [str(manifest.resolve())],
                "content_visit_paths": [],
            }
        }],
    }
    with pytest.raises(RuntimeError, match="reused"):
        verify_snapshot_artifacts(summary)


def _repair_image_gate(tmp_path: Path, *, source: str, status, rule: str, fetch_status: str) -> dict:
    ledger_path = tmp_path / "behavior.content_visits.jsonl"
    ledger = VisitLedger.create(ledger_path, platform="weibo", mode="repair", clock=lambda: "2026-11-25T00:00:00+00:00")
    trigger = ledger.append("action", seq=ledger.next_seq(), page_role="detail", action="navigate", target={"note_id": "525"})
    detail = ledger.append(
        "visit", seq=ledger.next_seq(), at="2026-11-25T00:00:00+00:00", recorded_at="2026-11-25T00:00:00+00:00",
        page_role="detail", stage="note_detail", source="page_response", rule="weibo.statuses_show",
        status=200, subject_id="525", trigger=trigger,
    )
    start = ledger.next_seq()
    ledger.append(
        "pause", seq=ledger.next_seq(), page_role="detail", stage="note_detail", start_seq=start,
        seconds=1.0, continuity="n/a",
    )
    author = ledger.append(
        "visit", seq=ledger.next_seq(), at="2026-11-25T00:00:00+00:00", recorded_at="2026-11-25T00:00:00+00:00",
        page_role="detail", stage="creator_profile", source="page_response", rule="weibo.side_user",
        status=200, subject_id="88", trigger=trigger,
    )
    image_sha = digest(b"weibo-image")
    image = ledger.append(
        "visit", seq=ledger.next_seq(), at="2026-11-25T00:00:00+00:00", recorded_at="2026-11-25T00:00:00+00:00",
        page_role="primary", stage="note_image", source=source, rule=rule, status=status,
        subject_id="525", trigger=None, asset_key="weibo:pid:pid-1", sha256=image_sha,
    )
    manifest = tmp_path / "image_manifest.jsonl"
    attempts = 0 if fetch_status == "reused" else 1
    http_status = None if fetch_status == "reused" else 200
    write_manifest_atomic(manifest, [_manifest_entry(
        platform="weibo", status=fetch_status, attempts=attempts, http_status=http_status, digest_value=image_sha,
    )])
    record = {
        "note_id": "525",
        "author_platform_id": "88",
        "image_assets": [{"source_index": 0, "source_asset_key": "weibo:pid:pid-1"}],
        "content_visit_refs": {
            "ledger_id": ledger.ledger_id,
            "note_detail": detail,
            "creator_profile": author,
            "images": [image],
        },
    }
    return validate_content_visits(
        [record], [ledger_path], platform="weibo", mode="repair", manifests=[manifest],
    )


def test_repair_r5_accepts_stored_image_and_rejects_cross_pairs(tmp_path: Path) -> None:
    ok = _repair_image_gate(
        tmp_path / "ok", source="local_media", status=None, rule=WEIBO_STORED_IMAGE_RULE, fetch_status="reused",
    )
    assert ok["content_visit_ok"] is True, ok
    crossed = _repair_image_gate(
        tmp_path / "cross", source="cdn_direct", status=200, rule="weibo.content_image", fetch_status="reused",
    )
    assert crossed["problem_counts"].get("R5")
    reverse = _repair_image_gate(
        tmp_path / "reverse", source="local_media", status=None, rule=WEIBO_STORED_IMAGE_RULE, fetch_status="downloaded",
    )
    assert reverse["problem_counts"].get("R5")
    built = tmp_path / "missing"
    ledger_path = built / "behavior.content_visits.jsonl"
    passed = _repair_image_gate(
        built, source="local_media", status=None, rule=WEIBO_STORED_IMAGE_RULE, fetch_status="reused",
    )
    assert passed["content_visit_ok"] is True
    lines = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    by_stage = {line.get("stage"): line["id"] for line in lines if line.get("kind") == "visit"}
    record = {
        "note_id": "525",
        "author_platform_id": "88",
        "image_assets": [{"source_index": 0, "source_asset_key": "weibo:pid:pid-1"}],
        "content_visit_refs": {
            "ledger_id": lines[0]["ledger_id"],
            "note_detail": by_stage["note_detail"],
            "creator_profile": by_stage["creator_profile"],
            "images": [],
        },
    }
    missing = validate_content_visits(
        [record], [ledger_path], platform="weibo", mode="repair",
        manifests=[built / "image_manifest.jsonl"],
    )
    assert missing["problem_counts"].get("R5")


def test_discovery_rejects_reused_manifest_and_local_media(tmp_path: Path) -> None:
    ledger_path = tmp_path / "behavior.content_visits.jsonl"
    ledger = VisitLedger.create(ledger_path, platform="weibo", mode="discovery", clock=lambda: "2026-11-25T00:00:00+00:00")
    ledger.append(
        "visit", seq=ledger.next_seq(), at="2026-11-25T00:00:00+00:00", recorded_at="2026-11-25T00:00:00+00:00",
        page_role="primary", stage="note_image", source="local_media", rule=WEIBO_STORED_IMAGE_RULE,
        status=None, subject_id="525", trigger=None, asset_key="weibo:pid:pid-1", sha256="ab" * 32,
    )
    manifest = tmp_path / "image_manifest.jsonl"
    write_manifest_atomic(manifest, [_manifest_entry(
        platform="weibo", status="reused", attempts=0, http_status=None, digest_value="ab" * 32,
    )])
    result = validate_content_visits(
        [], [ledger_path], platform="weibo", mode="discovery", manifests=[manifest],
    )
    assert result["content_visit_ok"] is False
    assert result["problem_counts"].get("R5")


def test_keep_stored_images_flag_is_weibo_refresh_only(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["repair_post_details.py", "--platform", "zhihu", "--keep-stored-images", "--refresh-all"])
    with pytest.raises(SystemExit, match="weibo --refresh-all"):
        repair.main()
    monkeypatch.setattr(sys, "argv", ["repair_post_details.py", "--platform", "weibo", "--keep-stored-images"])
    with pytest.raises(SystemExit, match="weibo --refresh-all"):
        repair.main()
    with pytest.raises(SystemExit, match="post-repair"):
        reject_keep_stored_images_scope(enabled=True, post_repair=False, platforms=["weibo"])
    with pytest.raises(SystemExit, match="weibo"):
        reject_keep_stored_images_scope(enabled=True, post_repair=True, platforms=["zhihu"])
    reject_keep_stored_images_scope(enabled=True, post_repair=True, platforms=["weibo"])
    command = repair.build_child_command(
        platform="weibo", keyword="青岛旅游", db_path=Path("/tmp/none.sqlite"),
        output_root=Path("/tmp/out"), targets_path=Path("/tmp/targets.json"),
        headless=True, keep_stored_images=True,
    )
    assert "--keep-stored-images" in command
    assert repair._sum_child_image_counts([
        {"image_materialization": {"images_downloaded": 2, "images_reused": 3, "downloaded_images": 2}},
        {"image_materialization": {"downloaded_images": 1}},
    ]) == {"images_downloaded": 3, "images_reused": 3}


def test_direct_visit_still_rejects_local_media(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(tmp_path / "behavior.jsonl"))
    capture = WeiboPageCapture.open(object(), mode="repair")
    with pytest.raises(ValueError, match="direct visit"):
        capture.channel.record_direct_visit(
            source="local_media", stage="note_image", rule=WEIBO_STORED_IMAGE_RULE,
            subject_id="525", trigger=None, status=None, asset_key="weibo:pid:pid-1", sha256="ab" * 32,
        )
