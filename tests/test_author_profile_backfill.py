"""#178：作者资料回填只填空列，取不回的作者保留原行。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from trippostcollect.application import author_profile_backfill as backfill
from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.platforms.xhs import parser as xhs_parser


AVATAR_URL = "https://img.example/avatar-secret.jpg"
COOKIE_VALUE = "session=super-secret-cookie"


def _redirect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(backfill, "AUTHOR_PROFILE_BACKFILL_OUTPUT", tmp_path / "out")
    monkeypatch.setattr(backfill, "AUTHOR_PROFILE_BACKFILL_RUNTIME_ROOT", tmp_path / "rt")
    monkeypatch.setattr(backfill, "AUTHOR_PROFILE_BACKFILL_BACKUP_ROOT", tmp_path / "bk")


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "posts.sqlite"
    bootstrap_database(path, sync_jobs=False)
    return path


def _insert(connection: sqlite3.Connection, **overrides: object) -> int:
    row = {
        "platform_key": "xhs",
        "platform_post_id": "1",
        "source_type": "search",
        "source_url": "https://www.xiaohongshu.com/explore/1",
        "canonical_url": "https://www.xiaohongshu.com/explore/1",
        "keyword": "青岛旅游",
        "captured_at": "2026-01-01T00:00:00+08:00",
        "created_at": "2026-01-01 00:00:00",
        "raw_sample_json": "{}",
        "author_json": "{}",
        "content_text": "正文",
        "author_display_name": "昵称",
        "author_followers_count": 3,
        "author_following_count": 2,
        "published_at": "2026-01-01T08:00:00+08:00",
        "author_platform_id": "author-1",
    }
    row.update(overrides)
    post_id = str(row["platform_post_id"])
    platform = str(row["platform_key"])
    if platform == "douyin":
        row["source_url"] = f"https://www.douyin.com/video/{post_id}"
        row["canonical_url"] = row["source_url"]
    else:
        row["source_url"] = f"https://www.xiaohongshu.com/explore/{post_id}"
        row["canonical_url"] = row["source_url"]
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    cursor = connection.execute(
        f"INSERT INTO web_posts ({columns}) VALUES ({placeholders})",
        list(row.values()),
    )
    return int(cursor.lastrowid)


def _summary(tmp_path: Path) -> dict:
    reports = list((tmp_path / "out").glob("*/run_summary.json"))
    assert len(reports) == 1
    return json.loads(reports[0].read_text(encoding="utf-8"))


def _xhs_raw(profile: dict | None) -> str:
    if profile is None:
        return "{}"
    return json.dumps({"creator_profile_json": json.dumps(profile, ensure_ascii=False)}, ensure_ascii=False)


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    _redirect(monkeypatch, tmp_path)
    return backfill.main(argv)


def test_xhs_offline_matches_ingest_function_and_fills_only_nulls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = {"verifyInfo": {"redOfficialVerifyType": 2}, "tags": [{"name": "美食博主", "tagType": "profession"}]}
    expected = xhs_parser.creator_verified_fields(profile)
    record = xhs_parser.update_xhs_note(
        {
            "note_id": "64b95d01000000000c034587",
            "type": "normal",
            "title": "青岛海边",
            "desc": "青岛海边正文",
            "time": 1_700_000_000_000,
            "user": {"user_id": "author-1", "nickname": "海边的猫"},
            "interact_info": {},
            "image_list": [],
            "creator_profile": profile,
        },
        source_keyword="青岛旅游",
        current_timestamp=lambda: 1,
        save_data_option="jsonl",
    )
    assert record["author_verified"] == expected["author_verified"]
    assert record["author_verified_text"] == expected["author_verified_text"]
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        _insert(
            connection,
            platform_post_id="fill",
            raw_sample_json=_xhs_raw(profile),
            author_verified_text="已有文字",
            content_text="不要改",
            author_followers_count=9,
        )
        _insert(
            connection,
            platform_post_id="keep",
            author_platform_id="author-keep",
            raw_sample_json=_xhs_raw({"verifyInfo": {"redOfficialVerifyType": 0}}),
            author_verified=1,
            author_verified_text="保持",
        )
        connection.commit()
    exit_code = _run(tmp_path, monkeypatch, ["--platform", "xhs", "--db", str(db_path)])
    report = _summary(tmp_path)
    assert exit_code == 0
    assert report["changed_row_count"] == 1
    assert report["column_counts"]["author_verified"]["before"] == 1
    assert report["column_counts"]["author_verified"]["after"] == 2
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        filled = connection.execute("SELECT * FROM web_posts WHERE platform_post_id='fill'").fetchone()
        kept = connection.execute("SELECT * FROM web_posts WHERE platform_post_id='keep'").fetchone()
        assert connection.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 2
    assert filled["author_verified"] == expected["author_verified"]
    assert filled["author_verified_text"] == "已有文字"
    assert filled["content_text"] == "不要改"
    assert filled["author_followers_count"] == 9
    assert filled["author_display_name"] == "昵称"
    assert filled["published_at"] == "2026-01-01T08:00:00+08:00"
    assert kept["author_verified"] == 1
    assert kept["author_verified_text"] == "保持"
    assert report["backup"]["sha256"]


def test_xhs_parse_failure_keeps_row_and_dry_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        _insert(connection, platform_post_id="bad", raw_sample_json="not-json")
        _insert(connection, platform_post_id="empty", author_platform_id="author-2", raw_sample_json="{}")
        _insert(
            connection,
            platform_post_id="unknown",
            author_platform_id="author-3",
            raw_sample_json=_xhs_raw({"verifyInfo": {"redOfficialVerifyType": "2"}}),
        )
        connection.commit()

    def fail_child(*_args, **_kwargs):
        raise AssertionError("dry-run 不应启动 child")

    monkeypatch.setattr(backfill, "run_repair_child", fail_child)
    assert _run(tmp_path, monkeypatch, ["--platform", "xhs", "--db", str(db_path), "--dry-run"]) == 0
    dry = _summary(tmp_path)
    assert dry["dry_run"] is True
    assert dry["target_author_count"] == 3
    assert dry["target_row_count"] == 3
    assert dry["batch_plan"][0]["mode"] == "offline"
    assert not list((tmp_path / "bk").glob("**/*"))
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(author_verified) FROM web_posts").fetchone()[0] == 0
    for name in ("out", "rt"):
        target = tmp_path / name
        if target.exists():
            shutil.rmtree(target)

    assert _run(tmp_path, monkeypatch, ["--platform", "xhs", "--db", str(db_path)]) == 0
    report = _summary(tmp_path)
    assert report["changed_row_count"] == 0
    assert report["unrecovered_by_reason"]["parse_failed"] == 1
    assert report["unrecovered_by_reason"]["empty_projection"] == 1
    assert report["unrecovered_by_reason"]["verification_unknown"] == 1
    assert set(report["unrecovered_author_ids"]) == {"author-1", "author-2", "author-3"}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(author_verified) FROM web_posts").fetchone()[0] == 0


def test_xhs_max_authors_and_author_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _db(tmp_path)
    official = {"verifyInfo": {"redOfficialVerifyType": 1}}
    with sqlite3.connect(db_path) as connection:
        _insert(connection, author_platform_id="new", created_at="2026-02-02 00:00:00", raw_sample_json=_xhs_raw(official))
        _insert(connection, author_platform_id="old", platform_post_id="2", created_at="2026-01-01 00:00:00", raw_sample_json=_xhs_raw(official))
        connection.commit()
    assert _run(
        tmp_path,
        monkeypatch,
        ["--platform", "xhs", "--db", str(db_path), "--max-authors", "1"],
    ) == 0
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("SELECT author_verified FROM web_posts WHERE author_platform_id='new'").fetchone()[0] == 1
        assert connection.execute("SELECT author_verified FROM web_posts WHERE author_platform_id='old'").fetchone()[0] is None
    assert _run(
        tmp_path,
        monkeypatch,
        ["--platform", "xhs", "--db", str(db_path), "--author-id", "old"],
    ) == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT author_verified FROM web_posts WHERE author_platform_id='old'").fetchone()[0] == 1


def test_default_db_write_requires_confirmation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _redirect(monkeypatch, tmp_path)
    with pytest.raises(SystemExit, match="confirm-default-db-backfill"):
        backfill.main(["--platform", "xhs", "--db", str(backfill.DEFAULT_DB)])
