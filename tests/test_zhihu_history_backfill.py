"""#194：知乎历史回填的离线图片去重与全量刷新选择。临时库和假 child，不访问平台。"""

from __future__ import annotations

import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path

import pytest

from trippostcollect.db.bootstrap import bootstrap_database


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

repair = import_module("repair_post_details")
mediacrawler = import_module("mediacrawler_crawl")

HASH_A = "a" * 32
HASH_B = "b" * 32
HASH_C = "c" * 32


def zhimg(digest: str, suffix: str = "") -> str:
    return f"https://pic1.zhimg.com/v2-{digest}{suffix}.jpg"


def answer_url(post_id: str) -> str:
    return f"https://www.zhihu.com/question/1/answer/{post_id}"


def _argv(db_path: Path, *extra: str) -> list[str]:
    return ["repair_post_details.py", "--platform", "zhihu", "--db", str(db_path), *extra]


def _redirect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(repair, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_OUTPUT", tmp_path / "out")
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_RUNTIME_ROOT", tmp_path / "rt")
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_BACKUP_ROOT", tmp_path / "bk")


def _write_media(tmp_path: Path, relative: str, payload: bytes = b"img") -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _insert_post(
    conn: sqlite3.Connection,
    *,
    platform: str,
    post_id: str,
    url: str,
    raw: dict,
    images: list[dict],
    count: int,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO web_posts(
            platform_key, platform_post_id, source_type, source_url,
            canonical_url, captured_at, keyword, content_text,
            raw_sample_json, post_images_count
        ) VALUES (?, ?, 'import', ?, ?, '2026-11-01T00:00:00+08:00', '青岛旅游', '青岛正文', ?, ?)
        """,
        (platform, post_id, url, url, json.dumps(raw, ensure_ascii=False, sort_keys=True), count),
    )
    web_post_id = int(cursor.lastrowid)
    for image in images:
        conn.execute(
            """
            INSERT INTO web_post_images(
                web_post_id, image_index, image_url, image_role, local_path, sha256
            ) VALUES (?, ?, ?, ?, ?, 'abc')
            """,
            (
                web_post_id,
                image["index"],
                image["url"],
                image.get("role", "content"),
                image.get("local_path"),
            ),
        )
    return web_post_id


def _seed_dedupe_db(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    db_path = tmp_path / "dedupe.sqlite"
    bootstrap_database(db_path, sync_jobs=False)
    raw = {
        "priority": {"marker": "priority-raw", "content_detail_status": "detail_observed"},
        "mixed": {"marker": "mixed-raw", "content_detail_status": "detail_observed"},
        "keeper": {"marker": "keeper-raw", "content_detail_status": "detail_observed"},
        "clean": {"marker": "clean-raw", "author_page_badges": {}},
        "douyin": {"marker": "douyin-raw"},
    }
    shared = "data/media/zhihu/shared.jpg"
    paths = [
        "data/media/zhihu/loser-b.jpg",
        "data/media/zhihu/loser-720.jpg",
        "data/media/zhihu/loser-1440.jpg",
        "data/media/zhihu/loser-plain.jpg",
        "data/media/zhihu/keep-r.jpg",
        "data/media/zhihu/page.jpg",
        shared,
        "data/media/zhihu/keep-a-r.jpg",
        "data/media/zhihu/loser-b720.jpg",
        "data/media/zhihu/keep-b.jpg",
        "data/media/zhihu/keep-plain.png",
        "data/media/zhihu/keep-foreign.jpg",
        "data/media/zhihu/keeper.jpg",
        "data/media/zhihu/clean.jpg",
        "data/media/douyin/a.jpg",
        "data/media/douyin/b.jpg",
    ]
    for relative in paths:
        _write_media(tmp_path, relative, relative.encode())
    with sqlite3.connect(db_path) as conn:
        _insert_post(
            conn,
            platform="zhihu",
            post_id="priority",
            url=answer_url("priority"),
            raw=raw["priority"],
            count=5,
            images=[
                {"index": 4, "url": zhimg(HASH_A, "_b"), "local_path": paths[0]},
                {"index": 5, "url": zhimg(HASH_A, "_720w"), "local_path": paths[1]},
                {"index": 6, "url": zhimg(HASH_A, "_1440w"), "local_path": paths[2]},
                {"index": 7, "url": zhimg(HASH_A), "local_path": paths[3]},
                {"index": 8, "url": zhimg(HASH_A, "_r"), "local_path": paths[4]},
                {"index": 0, "url": "https://pic1.zhimg.com/page.jpg", "role": "page", "local_path": paths[5]},
            ],
        )
        _insert_post(
            conn,
            platform="zhihu",
            post_id="mixed",
            url=answer_url("mixed"),
            raw=raw["mixed"],
            count=6,
            images=[
                {"index": 0, "url": zhimg(HASH_A, "_b"), "local_path": shared},
                {"index": 1, "url": zhimg(HASH_A, "_r"), "local_path": paths[7]},
                {"index": 2, "url": zhimg(HASH_B, "_720w"), "local_path": paths[8]},
                {"index": 3, "url": zhimg(HASH_B), "local_path": paths[9]},
                {"index": 4, "url": "https://example.test/keep.png", "local_path": paths[10]},
                {
                    "index": 5,
                    "url": f"https://cdn.example.test/v2-{HASH_A}_r.jpg",
                    "local_path": paths[11],
                },
            ],
        )
        _insert_post(
            conn,
            platform="zhihu",
            post_id="keeper",
            url=answer_url("keeper"),
            raw=raw["keeper"],
            count=1,
            images=[{"index": 2, "url": zhimg(HASH_C), "local_path": shared}],
        )
        _insert_post(
            conn,
            platform="zhihu",
            post_id="clean",
            url=answer_url("clean"),
            raw=raw["clean"],
            count=1,
            images=[{"index": 0, "url": zhimg(HASH_C, "_r"), "local_path": paths[13]}],
        )
        _insert_post(
            conn,
            platform="douyin",
            post_id="dy-1",
            url="https://www.douyin.com/note/dy-1",
            raw=raw["douyin"],
            count=2,
            images=[
                {"index": 0, "url": zhimg(HASH_A, "_b"), "local_path": paths[14]},
                {"index": 1, "url": zhimg(HASH_A, "_r"), "local_path": paths[15]},
            ],
        )
        conn.commit()
    return db_path, raw


def _content_rows(conn: sqlite3.Connection, post_id: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            """
            SELECT i.image_index, i.image_url, i.local_path, i.image_role, p.post_images_count,
                   p.raw_sample_json
            FROM web_post_images AS i
            JOIN web_posts AS p ON p.id=i.web_post_id
            WHERE p.platform_post_id=? AND i.image_role='content'
            ORDER BY i.image_index, i.id
            """,
            (post_id,),
        )
    )


def _summaries(tmp_path: Path) -> list[dict]:
    paths = sorted((tmp_path / "out").glob("*/run_summary.json"))
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def test_dedupe_keeps_priority_and_moves_only_unshared_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, raw = _seed_dedupe_db(tmp_path)
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _argv(db_path, "--dedupe-images"))

    assert repair.main() == 0
    summary = _summaries(tmp_path)[0]

    with sqlite3.connect(db_path) as conn:
        priority = _content_rows(conn, "priority")
        mixed = _content_rows(conn, "mixed")
        keeper = _content_rows(conn, "keeper")
        clean = _content_rows(conn, "clean")
        douyin = _content_rows(conn, "dy-1")
        page = conn.execute(
            """
            SELECT i.image_index, i.local_path
            FROM web_post_images AS i
            JOIN web_posts AS p ON p.id=i.web_post_id
            WHERE p.platform_post_id='priority' AND i.image_role='page'
            """
        ).fetchone()
    assert [row["image_url"] for row in priority] == [zhimg(HASH_A, "_r")]
    assert [row["image_index"] for row in priority] == [4]
    assert priority[0]["post_images_count"] == 1
    assert json.loads(priority[0]["raw_sample_json"]) == raw["priority"]
    assert tuple(page) == (0, "data/media/zhihu/page.jpg")
    assert [row["image_url"] for row in mixed] == [
        zhimg(HASH_A, "_r"),
        zhimg(HASH_B),
        "https://example.test/keep.png",
        f"https://cdn.example.test/v2-{HASH_A}_r.jpg",
    ]
    assert [row["image_index"] for row in mixed] == [0, 1, 2, 3]
    assert mixed[0]["post_images_count"] == 4
    assert json.loads(mixed[0]["raw_sample_json"]) == raw["mixed"]
    assert [row["image_index"] for row in keeper] == [2]
    assert keeper[0]["local_path"] == "data/media/zhihu/shared.jpg"
    assert [row["image_url"] for row in clean] == [zhimg(HASH_C, "_r")]
    assert [row["image_url"] for row in douyin] == [zhimg(HASH_A, "_b"), zhimg(HASH_A, "_r")]

    moved = {
        "data/media/zhihu/loser-b.jpg",
        "data/media/zhihu/loser-720.jpg",
        "data/media/zhihu/loser-1440.jpg",
        "data/media/zhihu/loser-plain.jpg",
        "data/media/zhihu/loser-b720.jpg",
    }
    for relative in moved:
        assert not (tmp_path / relative).exists()
    assert (tmp_path / "data/media/zhihu/shared.jpg").is_file()
    assert (tmp_path / "data/media/zhihu/keep-r.jpg").is_file()
    backup_media = list((tmp_path / "bk").glob("*/media/data/media/zhihu/*.jpg"))
    assert {path.name for path in backup_media} == {
        "loser-b.jpg",
        "loser-720.jpg",
        "loser-1440.jpg",
        "loser-plain.jpg",
        "loser-b720.jpg",
    }
    assert summary["status"] == "completed"
    assert summary["posts_examined"] == 4
    assert summary["posts_changed"] == 2
    assert summary["content_image_rows_before"] == 13
    assert summary["content_image_rows_after"] == 7
    assert summary["deleted_row_count"] == 6
    assert summary["moved_file_count"] == 5
    assert summary["shared_path_skipped_count"] == 1
    assert summary["deletion_histogram"] == {"0": 2, "2": 1, "4": 1}
    assert summary["raw_sample_json_unchanged"] is True
    assert "raw_sample_json" in summary["raw_sample_json_note"]
    manifest = json.loads(Path(summary["moved_files_path"]).read_text(encoding="utf-8"))
    assert {item["local_path"] for item in manifest["moves"] if item["status"] == "moved"} == moved
    assert (tmp_path / "bk").glob("*/dedupe.sqlite")
    assert list((tmp_path / "bk").glob("*/dedupe.sqlite"))

    monkeypatch.setattr(sys, "argv", _argv(db_path, "--dedupe-images"))
    assert repair.main() == 0
    second = _summaries(tmp_path)[1]
    assert second["deleted_row_count"] == 0
    assert second["moved_file_count"] == 0
    assert second["posts_changed"] == 0
    assert (tmp_path / "data/media/zhihu/shared.jpg").is_file()
    assert len(list((tmp_path / "bk").glob("*/media/data/media/zhihu/*.jpg"))) == 5


def test_dedupe_dry_run_does_not_write_or_move_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, _raw = _seed_dedupe_db(tmp_path)
    before = db_path.read_bytes()
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _argv(db_path, "--dedupe-images", "--dry-run"))

    assert repair.main() == 0

    assert db_path.read_bytes() == before
    assert (tmp_path / "data/media/zhihu/loser-b.jpg").is_file()
    assert not list((tmp_path / "bk").glob("**/*"))
    summary = _summaries(tmp_path)[0]
    assert summary["status"] == "planned"
    assert summary["dry_run"] is True
    assert summary["deleted_row_count"] == 6
    assert summary["moved_file_count"] == 5
    assert summary["backup"] == {}
    assert summary["sample"]


def test_dedupe_default_database_requires_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, _raw = _seed_dedupe_db(tmp_path)
    before = db_path.read_bytes()
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(repair, "DEFAULT_DB", db_path)
    monkeypatch.setattr(sys, "argv", _argv(db_path, "--dedupe-images"))

    with pytest.raises(SystemExit, match="confirm-default-db-repair"):
        repair.main()

    assert db_path.read_bytes() == before
    assert not (tmp_path / "bk").exists()
    assert (tmp_path / "data/media/zhihu/loser-b.jpg").is_file()


def test_history_modes_reject_other_platforms_and_each_other(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "unused.sqlite"
    cases = [
        (["--platform", "douyin", "--dedupe-images"], "requires --platform zhihu"),
        (["--platform", "weibo", "--refresh-all"], "requires --platform zhihu"),
        (["--platform", "zhihu", "--dedupe-images", "--refresh-all"], "mutually exclusive"),
        (["--platform", "zhihu", "--dedupe-images", "--post-id", "1"], "does not accept"),
        (["--platform", "zhihu", "--dedupe-images", "--max-items", "1"], "does not accept"),
    ]
    for extra, message in cases:
        monkeypatch.setattr(
            sys,
            "argv",
            ["repair_post_details.py", "--db", str(db_path), *extra],
        )
        with pytest.raises(SystemExit, match=message):
            repair.main()


def _seed_refresh_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "refresh.sqlite"
    bootstrap_database(db_path, sync_jobs=False)
    with sqlite3.connect(db_path) as conn:
        old_id = _insert_post(
            conn,
            platform="zhihu",
            post_id="old-search",
            url=answer_url("old-search"),
            raw={
                "content_detail_status": "detail_observed",
                "content_detail_source": "search_content",
                "author_followers_source": "search_author",
            },
            count=0,
            images=[],
        )
        _insert_post(
            conn,
            platform="zhihu",
            post_id="new-detail",
            url=answer_url("new-detail"),
            raw={
                "content_detail_status": "detail_observed",
                "content_detail_source": "answer_detail",
                "author_followers_source": "author_page",
            },
            count=0,
            images=[],
        )
        _insert_post(
            conn,
            platform="zhihu",
            post_id="already",
            url=answer_url("already"),
            raw={
                "content_detail_status": "detail_observed",
                "content_detail_source": "answer_detail",
                "author_followers_source": "author_page",
                "author_page_badges": {},
            },
            count=0,
            images=[],
        )
        waived_id = _insert_post(
            conn,
            platform="zhihu",
            post_id="waived",
            url=answer_url("waived"),
            raw={"content_detail_status": "search_payload"},
            count=0,
            images=[],
        )
        conn.execute(
            """
            INSERT INTO post_detail_repair_waivers(
                web_post_id, reason, authorized_by, authorized_at, evidence_json
            ) VALUES (?, 'operator_waived', 'user', '2026-11-14T00:00:00+00:00', '{}')
            """,
            (waived_id,),
        )
        observed_waived = _insert_post(
            conn,
            platform="zhihu",
            post_id="observed-waived",
            url=answer_url("observed-waived"),
            raw={"content_detail_status": "detail_observed", "content_detail_source": "answer_detail"},
            count=0,
            images=[],
        )
        conn.execute(
            """
            INSERT INTO post_detail_repair_waivers(
                web_post_id, reason, authorized_by, authorized_at, evidence_json
            ) VALUES (?, 'operator_waived', 'user', '2026-11-14T00:00:00+00:00', '{}')
            """,
            (observed_waived,),
        )
        conn.commit()
        assert old_id
    return db_path


def test_refresh_all_selects_rows_without_author_page_badges(tmp_path: Path) -> None:
    db_path = _seed_refresh_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        targets, rejected, pending = repair.select_targets(
            conn,
            platform="zhihu",
            post_ids=[],
            max_items=0,
            refresh_all=True,
        )
        limited, _rejected, _pending = repair.select_targets(
            conn,
            platform="zhihu",
            post_ids=["old-search"],
            max_items=0,
            refresh_all=True,
        )
        capped, _capped_rejected, _pending = repair.select_targets(
            conn,
            platform="zhihu",
            post_ids=[],
            max_items=1,
            refresh_all=True,
        )
        default_targets, _default_rejected, default_pending = repair.select_targets(
            conn,
            platform="zhihu",
            post_ids=[],
            max_items=0,
        )
        refresh_pending = repair.pending_count(conn, "zhihu", refresh_all=True)

    assert [item["platform_post_id"] for item in targets] == ["old-search", "new-detail"]
    assert [item["platform_post_id"] for item in rejected] == ["waived", "observed-waived"]
    assert all(item["reason"] == "post_detail_repair_waived" for item in rejected)
    assert pending == 4
    assert refresh_pending == 4
    assert [item["platform_post_id"] for item in limited] == ["old-search"]
    assert [item["platform_post_id"] for item in capped] == ["old-search"]
    # 默认模式仍只看非 detail_observed。豁免行被拒绝，已观察行不进入待修复。
    assert default_pending == 1
    assert default_targets == []


def test_refresh_dry_run_lists_planned_and_rejected_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _seed_refresh_db(tmp_path)
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", _argv(db_path, "--refresh-all", "--dry-run"))

    assert repair.main() == 0
    summary = _summaries(tmp_path)[0]
    by_id = {item["platform_post_id"]: item for item in summary["target_results"]}
    assert by_id["old-search"]["outcome"] == "planned"
    assert by_id["new-detail"]["outcome"] == "planned"
    assert by_id["waived"]["outcome"] == "rejected"
    assert by_id["observed-waived"]["outcome"] == "rejected"
    assert "already" not in by_id
    assert summary["plan"]["selection"] == "refresh_all"


def test_refresh_failure_keeps_the_original_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _seed_refresh_db(tmp_path)
    _redirect(monkeypatch, tmp_path)
    child_summary = tmp_path / "child-summary.json"
    child_summary.write_text(
        json.dumps(
            {
                "import_completion_met": True,
                "formal_validation": {
                    "valid_total_count": 1,
                    "skipped_candidate_failures": [
                        {
                            "platform_post_id": "old-search",
                            "error_code": "detail_entity_missing",
                            "detail": "page state has no answer entity",
                        }
                    ],
                },
                "import_result": {},
            }
        ),
        encoding="utf-8",
    )

    def fake_child(*_args: object, **_kwargs: object) -> dict:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE web_posts
                SET content_text=?, raw_sample_json=?
                WHERE platform_post_id='new-detail'
                """,
                (
                    "刷新后的正文",
                    json.dumps(
                        {
                            "content_detail_status": "detail_observed",
                            "content_detail_source": "answer_detail",
                            "author_followers_source": "author_page",
                            "author_page_badges": {},
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
        return {
            "returncode": 0,
            "stdout": json.dumps({"summary": str(child_summary)}),
            "stderr": "",
        }

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    monkeypatch.setattr(repair, "verify_image_artifacts", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(repair, "verify_image_persistence", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(sys, "argv", _argv(db_path, "--refresh-all", "--batch-size", "10"))
    with sqlite3.connect(db_path) as conn:
        before = conn.execute(
            "SELECT content_text, raw_sample_json FROM web_posts WHERE platform_post_id='old-search'"
        ).fetchone()

    assert repair.main() == 0

    with sqlite3.connect(db_path) as conn:
        after = conn.execute(
            "SELECT content_text, raw_sample_json FROM web_posts WHERE platform_post_id='old-search'"
        ).fetchone()
        refreshed = json.loads(
            conn.execute(
                "SELECT raw_sample_json FROM web_posts WHERE platform_post_id='new-detail'"
            ).fetchone()[0]
        )
    assert after == before
    assert refreshed["author_page_badges"] == {}
    assert refreshed["author_followers_source"] == "author_page"
    summary = _summaries(tmp_path)[0]
    by_id = {item["platform_post_id"]: item for item in summary["target_results"]}
    assert by_id["new-detail"]["outcome"] == "refreshed"
    assert by_id["old-search"]["outcome"] == "candidate_failed"
    assert by_id["old-search"]["reason"] == "detail_entity_missing"
    assert by_id["waived"]["outcome"] == "rejected"
    assert summary["status"] == "completed_with_remaining"


def test_repaired_rows_accepts_zhihu_text_only_detail(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "rows.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE web_posts(
            id INTEGER PRIMARY KEY,
            platform_key TEXT,
            platform_post_id TEXT,
            content_text TEXT,
            raw_sample_json TEXT
        );
        CREATE TABLE web_post_images(
            id INTEGER PRIMARY KEY,
            web_post_id INTEGER,
            image_role TEXT,
            local_path TEXT,
            sha256 TEXT
        );
        """
    )
    rows = [
        (
            "text-only",
            "纯文字",
            {
                "content_detail_status": "detail_observed",
                "content_detail_source": "answer_detail",
                "author_followers_source": "author_page",
                "author_page_badges": {},
            },
        ),
        (
            "legacy-search",
            "旧搜索正文",
            {
                "content_detail_status": "detail_observed",
                "content_detail_source": "search_content",
                "author_followers_source": "author_page",
                "author_page_badges": {},
            },
        ),
        (
            "article-text",
            "文章正文",
            {
                "content_detail_status": "detail_observed",
                "content_detail_source": "article_detail",
                "author_followers_source": "search_author",
            },
        ),
    ]
    for index, (post_id, text, raw) in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO web_posts(id, platform_key, platform_post_id, content_text, raw_sample_json) VALUES (?, 'zhihu', ?, ?, ?)",
            (index, post_id, text, json.dumps(raw)),
        )
    conn.execute(
        """
        INSERT INTO web_posts(id, platform_key, platform_post_id, content_text, raw_sample_json)
        VALUES (4, 'zhihu', 'with-image', '有图正文', ?)
        """,
        (
            json.dumps(
                {
                    "content_detail_status": "detail_observed",
                    "content_detail_source": "answer_detail",
                    "author_followers_source": "author_page",
                    "author_page_badges": {"userType": "people"},
                }
            ),
        ),
    )
    conn.execute(
        """
        INSERT INTO web_post_images(web_post_id, image_role, local_path, sha256)
        VALUES (4, 'content', 'data/media/zhihu/body.jpg', '')
        """
    )
    conn.commit()

    plain = repair.repaired_rows(conn, "zhihu", ["text-only", "legacy-search", "article-text", "with-image"])
    refresh = repair.repaired_rows(
        conn,
        "zhihu",
        ["text-only", "legacy-search", "article-text", "with-image"],
        refresh_all=True,
    )

    assert plain["text-only"]["recovered"] is True
    assert plain["legacy-search"]["recovered"] is False
    assert plain["article-text"]["recovered"] is True
    assert plain["with-image"]["recovered"] is False
    assert refresh["text-only"]["recovered"] is True
    assert refresh["article-text"]["recovered"] is False
    assert refresh["legacy-search"]["recovered"] is False
    assert "search_content" not in repair.TRUSTED_DETAIL_SOURCES["zhihu"]
    conn.close()


def test_load_post_repair_fallbacks_omits_zhihu_author_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "fallback.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE web_posts(
                platform_key TEXT,
                platform_post_id TEXT,
                keyword TEXT,
                published_at TEXT,
                author_followers_count INTEGER,
                author_display_name TEXT,
                raw_sample_json TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO web_posts VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "zhihu",
                "answer-1",
                "青岛旅游",
                "2026-07-13T12:00:00+08:00",
                123,
                "旧作者",
                json.dumps(
                    {
                        "created_time": 1780000000,
                        "followers_count": 123,
                        "followers_observed": True,
                        "author_followers_source": "search_author",
                        "creator_hash": "old-hash",
                        "user_nickname": "旧昵称",
                    }
                ),
            ),
        )
    fallbacks = mediacrawler.load_post_repair_fallbacks(db_path, "zhihu", {"answer-1"})

    assert fallbacks["answer-1"] == {
        "keyword": "青岛旅游",
        "published_at": "2026-07-13T12:00:00+08:00",
        "created_time": 1780000000,
    }
