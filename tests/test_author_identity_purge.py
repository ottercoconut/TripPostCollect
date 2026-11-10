"""删除台账中不可达且仍脱敏的作者帖子。"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
from pathlib import Path

import pytest

from trippostcollect.application import author_identity as identity
from trippostcollect.application import author_identity_purge as purge
from trippostcollect.db.bootstrap import bootstrap_database


NICKNAME = "独特昵称甲ZZ"
BODY_MARKER = "PURGE_BODY_MARKER_8c1f0a7e6b5d4c3a9e"
CHANGED_TABLES = {
    "web_posts",
    "web_post_images",
    "post_detail_repair_waivers",
    "crawl_discovery_seen_candidates",
}


def _masked(name: str) -> str:
    return identity.legacy_display_mask(name)


def _author_hash(raw: str) -> str:
    return identity.legacy_author_digest(raw)


def _redirect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(purge, "AUTHOR_IDENTITY_REPAIR_OUTPUT", tmp_path / "out")
    monkeypatch.setattr(purge, "AUTHOR_IDENTITY_PURGE_BACKUP_ROOT", tmp_path / "backups")
    monkeypatch.setattr(purge, "AUTHOR_IDENTITY_REPAIR_LEDGER_ROOT", tmp_path / "ledger")
    monkeypatch.setattr(purge, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(purge, "LOCAL_MEDIA_ROOT", tmp_path / "data" / "media")


def _connect_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _open(path: Path) -> sqlite3.Connection:
    bootstrap_database(path, sync_jobs=False)
    return _connect_db(path)


def _write_ledger(tmp_path: Path, platform: str, entries: list[dict[str, object]]) -> None:
    path = tmp_path / "ledger" / f"{platform}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _entry(platform: str, author_key: str, post_id: str, outcome: str) -> dict[str, object]:
    return {
        "platform": platform,
        "author_key": author_key,
        "post_ids": [post_id],
        "outcome": outcome,
    }


def _insert_post(
    conn: sqlite3.Connection,
    *,
    platform: str,
    post_id: str,
    author_id: str,
    display_name: str,
    content_text: str,
    nickname: str = "",
) -> int:
    author_json = {"nickname": nickname or display_name, "creator_hash": author_id}
    cursor = conn.execute(
        """
        INSERT INTO web_posts(
            platform_key, platform_post_id, source_type, source_url, canonical_url,
            title, author_display_name, author_platform_id, content_text, content_length,
            published_at, captured_at, keyword, author_json, raw_sample_json
        ) VALUES (?, ?, 'mediacrawler_search', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
        """,
        (
            platform,
            post_id,
            f"https://example.test/{platform}/{post_id}",
            f"https://example.test/{platform}/{post_id}",
            "标题",
            display_name,
            author_id,
            content_text,
            len(content_text),
            "2024-04-06T15:35:00+08:00",
            "2026-01-01T00:00:00+08:00",
            "青岛旅游",
            json.dumps(author_json, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


def _insert_image(conn: sqlite3.Connection, web_post_id: int, index: int, local_path: str) -> None:
    conn.execute(
        """
        INSERT INTO web_post_images(
            web_post_id, image_index, image_url, image_role, local_path, sha256
        ) VALUES (?, ?, 'https://img.example/body.jpg', 'content', ?, 'abc')
        """,
        (web_post_id, index, local_path),
    )


def _insert_seen(
    conn: sqlite3.Connection,
    job_id: int,
    platform: str,
    post_id: str,
    fingerprint: str = "fp-qingdao",
) -> None:
    conn.execute(
        """
        INSERT INTO crawl_discovery_seen_candidates(
            job_id, platform_key, query_fingerprint, platform_post_id, first_run_id, last_run_id
        ) VALUES (?, ?, ?, ?, 'run-a', 'run-a')
        """,
        (job_id, platform, fingerprint, post_id),
    )


def _insert_waiver(conn: sqlite3.Connection, web_post_id: int) -> None:
    conn.execute(
        """
        INSERT INTO post_detail_repair_waivers(
            web_post_id, reason, authorized_by, authorized_at
        ) VALUES (?, 'missing_detail', 'operator', '2026-01-02T00:00:00+08:00')
        """,
        (web_post_id,),
    )


def _seed_control_plane(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        INSERT INTO crawl_jobs(
            job_key, site_key, target_url, job_kind, next_run_at
        ) VALUES ('weibo-qingdao', 'weibo', 'https://m.weibo.cn', 'mediacrawler_search', '2026-04-01T00:00:00+08:00')
        """
    )
    job_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO crawl_discovery_checkpoints(
            job_id, platform_key, keyword, query_fingerprint, resume_page, campaign_candidate_count
        ) VALUES (?, 'weibo', '青岛旅游', 'fp-qingdao', 4, 11)
        """,
        (job_id,),
    )
    conn.execute(
        """
        INSERT INTO crawl_discovery_candidate_exclusions(
            job_id, platform_key, query_fingerprint, platform_post_id, reason, authorized_run_id
        ) VALUES (?, 'weibo', 'fp-qingdao', 'kept-exclusion', 'operator_skip', 'run-a')
        """,
        (job_id,),
    )
    return job_id


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_rows(conn: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    quoted = _quote(table)
    columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({quoted})")]
    order = ", ".join(_quote(column) for column in columns)
    return [tuple(row) for row in conn.execute(f"SELECT * FROM {quoted} ORDER BY {order}")]


def _stable_dump(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    names = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: _table_rows(conn, name) for name in names if name not in CHANGED_TABLES}


def _summaries(tmp_path: Path) -> list[dict[str, object]]:
    paths = sorted((tmp_path / "out").glob("purge-*/run_summary.json"))
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def _summary_text(tmp_path: Path) -> str:
    paths = sorted((tmp_path / "out").glob("purge-*/run_summary.json"))
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def _plant(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _db_bytes(path: Path) -> bytes:
    blob = path.read_bytes()
    for suffix in ("-wal", "-shm", "-journal"):
        extra = Path(str(path) + suffix)
        if extra.is_file():
            blob += extra.read_bytes()
    return blob


def test_rows_missing_from_ledger_or_hash_mismatch_stay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "purge.sqlite"
    unreachable = _author_hash("missing-author")
    mismatch = _author_hash("other-author")
    absent = _author_hash("local-only")
    masked = _masked(NICKNAME)
    with _open(db_path) as conn:
        _insert_post(
            conn,
            platform="weibo",
            post_id="gone",
            author_id=unreachable,
            display_name=masked,
            content_text="要删除的正文",
            nickname=NICKNAME,
        )
        _insert_post(
            conn,
            platform="weibo",
            post_id="not-listed",
            author_id=absent,
            display_name=masked,
            content_text="不在台账",
            nickname=NICKNAME,
        )
        _insert_post(
            conn,
            platform="weibo",
            post_id="mismatch",
            author_id=mismatch,
            display_name=masked,
            content_text="哈希不符",
            nickname=NICKNAME,
        )
        _insert_post(
            conn,
            platform="weibo",
            post_id="renamed",
            author_id=unreachable,
            display_name="已经恢复的昵称",
            content_text="昵称已不是脱敏形态",
            nickname="已经恢复的昵称",
        )
        conn.commit()
    _redirect(monkeypatch, tmp_path)
    _write_ledger(
        tmp_path,
        "weibo",
        [
            _entry("weibo", unreachable, "gone", "author_unreachable"),
            _entry("weibo", mismatch, "mismatch", "identity_hash_mismatch"),
        ],
    )
    assert purge.main(["--platform", "weibo", "--db", str(db_path)]) == 0
    summary = _summaries(tmp_path)[-1]
    assert summary["target_count"] == 1
    assert summary["not_in_ledger"] == 1
    assert summary["identity_hash_mismatch"] == 1
    assert summary["post_ids"] == ["gone"]
    with _open(db_path) as conn:
        remaining = {
            str(row["platform_post_id"])
            for row in conn.execute("SELECT platform_post_id FROM web_posts")
        }
    assert remaining == {"not-listed", "mismatch", "renamed"}


def test_deletes_target_rows_and_leaves_shared_symlink_and_outside_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "purge.sqlite"
    author_key = _author_hash("missing-author")
    masked = _masked(NICKNAME)
    media = tmp_path / "data" / "media"
    unique = media / "weibo" / "target-only" / "0.jpg"
    shared = media / "weibo" / "shared" / "0.jpg"
    keeper_only = media / "weibo" / "keeper" / "0.jpg"
    link = media / "weibo" / "target-link" / "link.jpg"
    outside = tmp_path / "outside" / "secret.jpg"
    _plant(unique, b"unique-image")
    _plant(shared, b"shared-image")
    _plant(keeper_only, b"keeper-image")
    _plant(outside, b"outside-image")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    with _open(db_path) as conn:
        job_id = _seed_control_plane(conn)
        target_id = _insert_post(
            conn,
            platform="weibo",
            post_id="target-post",
            author_id=author_key,
            display_name=masked,
            content_text=BODY_MARKER,
            nickname=NICKNAME,
        )
        keeper_id = _insert_post(
            conn,
            platform="weibo",
            post_id="keeper-post",
            author_id=_author_hash("kept-author"),
            display_name=masked,
            content_text="保留正文",
            nickname=NICKNAME,
        )
        other_id = _insert_post(
            conn,
            platform="douyin",
            post_id="target-post",
            author_id="12345",
            display_name="普通作者",
            content_text="其他平台正文",
        )
        _insert_image(conn, target_id, 0, "data/media/weibo/target-only/0.jpg")
        _insert_image(conn, target_id, 1, "data/media/weibo/shared/0.jpg")
        _insert_image(conn, target_id, 2, str(link))
        _insert_image(conn, target_id, 3, str(outside))
        _insert_image(conn, keeper_id, 0, "data/media/weibo/shared/0.jpg")
        _insert_image(conn, keeper_id, 1, "data/media/weibo/keeper/0.jpg")
        _insert_image(conn, other_id, 0, "data/media/douyin/other/0.jpg")
        _insert_waiver(conn, target_id)
        _insert_waiver(conn, keeper_id)
        _insert_seen(conn, job_id, "weibo", "target-post")
        _insert_seen(conn, job_id, "weibo", "target-post", "fp-other")
        _insert_seen(conn, job_id, "weibo", "keeper-post")
        _insert_seen(conn, job_id, "douyin", "target-post", "fp-douyin")
        before = _stable_dump(conn)
        conn.commit()
    assert BODY_MARKER.encode() in db_path.read_bytes()
    _redirect(monkeypatch, tmp_path)
    _write_ledger(tmp_path, "weibo", [_entry("weibo", author_key, "target-post", "author_unreachable")])
    assert purge.main(["--platform", "weibo", "--db", str(db_path)]) == 0
    summary = _summaries(tmp_path)[-1]
    assert summary["status"] == "completed"
    assert summary["post_ids"] == ["target-post"]
    assert summary["deleted_posts"] == 1
    assert summary["deleted_seen"] == 2
    assert summary["deleted_waivers"] == 1
    assert summary["deleted_images"] == 4
    assert summary["media_deleted"] == 1
    assert summary["media_skipped_shared"] == 1
    assert summary["media_skipped_symlink"] == 1
    assert summary["media_skipped_outside"] == 1
    assert summary["media_missing"] == 0
    assert summary["media_failed"] == 0
    assert summary["vacuum"] is True
    assert summary["quick_check"] == ["ok"]
    assert summary["foreign_key_check"] == []
    backup = Path(str(summary["backup"]["path"]))
    assert summary["backup"]["sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
    assert BODY_MARKER.encode() in backup.read_bytes()
    assert BODY_MARKER.encode() not in _db_bytes(db_path)
    assert not unique.exists()
    assert not unique.parent.exists()
    assert shared.is_file()
    assert keeper_only.is_file()
    assert link.is_symlink()
    assert outside.is_file()
    text = _summary_text(tmp_path)
    stdout = capsys.readouterr().out
    assert NICKNAME not in text
    assert masked not in text
    assert NICKNAME not in stdout
    assert masked not in stdout
    with _connect_db(db_path) as conn:
        assert _stable_dump(conn) == before
        posts = {
            (row["platform_key"], row["platform_post_id"])
            for row in conn.execute("SELECT platform_key, platform_post_id FROM web_posts")
        }
        assert posts == {("weibo", "keeper-post"), ("douyin", "target-post")}
        seen = {
            (row["platform_key"], row["platform_post_id"])
            for row in conn.execute("SELECT platform_key, platform_post_id FROM crawl_discovery_seen_candidates")
        }
        assert seen == {("weibo", "keeper-post"), ("douyin", "target-post")}
        waiver_posts = {
            row["platform_post_id"]
            for row in conn.execute(
                """
                SELECT web_posts.platform_post_id
                FROM post_detail_repair_waivers
                JOIN web_posts ON web_posts.id = post_detail_repair_waivers.web_post_id
                """
            )
        }
        assert waiver_posts == {"keeper-post"}
        images = {
            (row["platform_post_id"], row["local_path"])
            for row in conn.execute(
                """
                SELECT web_posts.platform_post_id, web_post_images.local_path
                FROM web_post_images
                JOIN web_posts ON web_posts.id = web_post_images.web_post_id
                """
            )
        }
        assert ("target-post", "data/media/weibo/shared/0.jpg") not in images
        assert ("keeper-post", "data/media/weibo/keeper/0.jpg") in images
        assert ("keeper-post", "data/media/weibo/shared/0.jpg") in images


def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "purge.sqlite"
    author_key = _author_hash("missing-author")
    masked = _masked("青岛游客")
    media = tmp_path / "data" / "media" / "weibo" / "dry" / "0.jpg"
    _plant(media, b"keep-me")
    with _open(db_path) as conn:
        post_id = _insert_post(
            conn,
            platform="weibo",
            post_id="dry-post",
            author_id=author_key,
            display_name=masked,
            content_text=BODY_MARKER,
        )
        absent_id = _insert_post(
            conn,
            platform="weibo",
            post_id="absent-post",
            author_id=_author_hash("absent"),
            display_name=masked,
            content_text="留着",
        )
        _insert_post(
            conn,
            platform="weibo",
            post_id="mismatch-post",
            author_id=_author_hash("mismatch"),
            display_name=masked,
            content_text="不符",
        )
        _insert_image(conn, post_id, 0, "data/media/weibo/dry/0.jpg")
        _insert_image(conn, absent_id, 0, "data/media/weibo/dry/0.jpg")
        conn.commit()
    before = db_path.read_bytes()
    _redirect(monkeypatch, tmp_path)
    _write_ledger(
        tmp_path,
        "weibo",
        [
            _entry("weibo", author_key, "dry-post", "author_unreachable"),
            _entry("weibo", _author_hash("mismatch"), "mismatch-post", "identity_hash_mismatch"),
        ],
    )
    assert purge.main(["--platform", "weibo", "--db", str(db_path), "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dry_run"
    assert payload["target_count"] == 1
    assert payload["not_in_ledger"] == 1
    assert payload["identity_hash_mismatch"] == 1
    assert payload["sample_post_ids"] == ["dry-post"]
    assert db_path.read_bytes() == before
    assert media.read_bytes() == b"keep-me"
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "backups").exists()
    with _open(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 3


def test_default_database_requires_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = (tmp_path / "trippostcollect.sqlite").resolve()
    author_key = _author_hash("missing-author")
    with _open(db_path) as conn:
        _insert_post(
            conn,
            platform="weibo",
            post_id="default-post",
            author_id=author_key,
            display_name=_masked("青岛游客"),
            content_text="默认库正文",
        )
        conn.commit()
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(purge, "DEFAULT_DB", db_path)
    _write_ledger(tmp_path, "weibo", [_entry("weibo", author_key, "default-post", "author_unreachable")])
    with pytest.raises(SystemExit) as caught:
        purge.main(["--platform", "weibo", "--db", str(db_path)])
    assert "confirm-default-db-purge" in str(caught.value)
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "backups").exists()
    with _open(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 1
    assert purge.main(["--platform", "weibo", "--db", str(db_path), "--confirm-default-db-purge"]) == 0
    with _open(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 0


def test_second_run_finds_no_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "purge.sqlite"
    author_key = _author_hash("missing-author")
    with _open(db_path) as conn:
        _insert_post(
            conn,
            platform="weibo",
            post_id="once",
            author_id=author_key,
            display_name=_masked("青岛游客"),
            content_text=BODY_MARKER,
        )
        _insert_post(
            conn,
            platform="weibo",
            post_id="keeper",
            author_id=_author_hash("kept"),
            display_name=_masked("崂山向导"),
            content_text="保留正文",
        )
        conn.commit()
    _redirect(monkeypatch, tmp_path)
    _write_ledger(tmp_path, "weibo", [_entry("weibo", author_key, "once", "author_unreachable")])
    assert purge.main(["--platform", "weibo", "--db", str(db_path)]) == 0
    assert purge.main(["--platform", "weibo", "--db", str(db_path)]) == 0
    first, second = _summaries(tmp_path)
    assert first["deleted_posts"] == 1
    assert second["target_count"] == 0
    assert second["deleted_posts"] == 0
    assert second["status"] == "completed"
    with _open(db_path) as conn:
        remaining = [str(row[0]) for row in conn.execute("SELECT platform_post_id FROM web_posts ORDER BY id")]
    assert remaining == ["keeper"]
    assert BODY_MARKER.encode() not in _db_bytes(db_path)


def test_missing_media_fails_after_the_post_is_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "purge.sqlite"
    author_key = _author_hash("missing-author")
    with _open(db_path) as conn:
        post_id = _insert_post(
            conn,
            platform="weibo",
            post_id="missing-file",
            author_id=author_key,
            display_name=_masked("青岛游客"),
            content_text=BODY_MARKER,
        )
        _insert_image(conn, post_id, 0, "data/media/weibo/missing-file/0.jpg")
        conn.commit()
    _redirect(monkeypatch, tmp_path)
    _write_ledger(tmp_path, "weibo", [_entry("weibo", author_key, "missing-file", "author_unreachable")])
    assert purge.main(["--platform", "weibo", "--db", str(db_path)]) == 2
    summary = _summaries(tmp_path)[-1]
    assert summary["status"] == "failed"
    assert summary["error"] == "media_cleanup_incomplete"
    assert summary["media_missing"] == 1
    assert summary["deleted_posts"] == 1
    assert summary["vacuum"] is True
    assert BODY_MARKER.encode() not in _db_bytes(db_path)
    with _open(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 0


def test_signal_during_delete_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "purge.sqlite"
    author_key = _author_hash("missing-author")
    media = tmp_path / "data" / "media" / "weibo" / "signal" / "0.jpg"
    _plant(media, b"still-here")
    with _open(db_path) as conn:
        post_id = _insert_post(
            conn,
            platform="weibo",
            post_id="signal-post",
            author_id=author_key,
            display_name=_masked("青岛游客"),
            content_text=BODY_MARKER,
        )
        _insert_image(conn, post_id, 0, "data/media/weibo/signal/0.jpg")
        conn.commit()
    original = purge._delete_rows

    def _delete_rows(*args: object, **kwargs: object) -> purge.WriteResult:
        outcome = original(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGINT)
        return outcome

    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(purge, "_delete_rows", _delete_rows)
    _write_ledger(tmp_path, "weibo", [_entry("weibo", author_key, "signal-post", "author_unreachable")])
    assert purge.main(["--platform", "weibo", "--db", str(db_path)]) == 128 + signal.SIGINT
    summary = _summaries(tmp_path)[-1]
    assert summary["status"] == "failed"
    assert summary["deleted_posts"] == 0
    assert summary["vacuum"] is False
    assert summary["interrupt"]["signum"] == signal.SIGINT
    assert media.read_bytes() == b"still-here"
    with _open(db_path) as conn:
        assert conn.execute("SELECT platform_post_id FROM web_posts").fetchone()[0] == "signal-post"
    assert BODY_MARKER.encode() in db_path.read_bytes()
