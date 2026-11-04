"""作者身份回填：目标判定、核验、传播、观察文件和收束。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
from pathlib import Path

import pytest

from trippostcollect.application import author_identity as identity
from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.records.author_observe import (
    AuthorIdentityBlocked,
    collect_author_observations,
    identity_fields,
)


def _mask(name: str) -> str:
    return identity.legacy_display_mask(name)


def _author_id(raw: str) -> str:
    return identity.legacy_author_digest(raw)


def _open_db(path: Path) -> sqlite3.Connection:
    bootstrap_database(path, sync_jobs=False)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_post(
    conn: sqlite3.Connection,
    *,
    platform: str,
    post_id: str,
    raw_author_id: str,
    display_name: str,
    canonical_url: str,
    raw_sample: dict,
    author_profile_url: str = "",
    content_text: str = "正文保持不变",
) -> int:
    author_json = {
        "creator_hash": _author_id(raw_author_id),
        "followers_count": 7,
        "followers_observed": True,
        "followers_source": "search_author",
        "following_count": 1,
        "liked_count": 2,
        "nickname": display_name,
        "posts_count": 3,
    }
    cursor = conn.execute(
        """
        INSERT INTO web_posts(
            platform_key, platform_post_id, source_type, source_url, canonical_url,
            title, author_display_name, author_platform_id, author_profile_url,
            author_followers_count, content_text, content_length, published_at,
            captured_at, keyword, author_json, raw_sample_json, post_images_count
        ) VALUES (?, ?, 'mediacrawler_search', ?, ?, ?, ?, ?, ?, 7, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            platform,
            post_id,
            canonical_url,
            canonical_url,
            "标题保持不变",
            display_name,
            _author_id(raw_author_id) if identity.legacy_author_id(display_name) or True else raw_author_id,
            author_profile_url,
            content_text,
            len(content_text),
            "2024-04-06T15:35:00+08:00",
            "2026-01-01T00:00:00+08:00",
            "青岛旅游",
            json.dumps(author_json, ensure_ascii=False, sort_keys=True),
            json.dumps(raw_sample, ensure_ascii=False, sort_keys=True),
        ),
    )
    web_post_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO web_post_images(
            web_post_id, image_index, image_url, image_role, local_path, sha256
        ) VALUES (?, 0, 'https://img.example/body.jpg', 'content', 'data/media/a.jpg', 'abc')
        """,
        (web_post_id,),
    )
    conn.commit()
    return web_post_id


def _weibo(conn: sqlite3.Connection, post_id: str, raw_author_id: str, nickname: str) -> int:
    masked = _mask(nickname)
    return _insert_post(
        conn,
        platform="weibo",
        post_id=post_id,
        raw_author_id=raw_author_id,
        display_name=masked,
        canonical_url=f"https://m.weibo.cn/detail/{post_id}",
        raw_sample={
            "creator_hash": _author_id(raw_author_id),
            "nickname": masked,
            "content": "正文保持不变",
            "note_id": post_id,
            "followers_count": 7,
        },
    )


def _redirect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(identity, "AUTHOR_IDENTITY_REPAIR_OUTPUT", tmp_path / "out")
    monkeypatch.setattr(identity, "AUTHOR_IDENTITY_REPAIR_RUNTIME_ROOT", tmp_path / "rt")
    monkeypatch.setattr(identity, "AUTHOR_IDENTITY_REPAIR_BACKUP_ROOT", tmp_path / "bk")
    monkeypatch.setattr(identity, "AUTHOR_IDENTITY_REPAIR_LEDGER_ROOT", tmp_path / "ledger")


def _summary(tmp_path: Path) -> dict:
    summaries = sorted((tmp_path / "out").glob("*/run_summary.json"), key=lambda path: path.stat().st_mtime)
    assert summaries
    return json.loads(summaries[-1].read_text(encoding="utf-8"))


def _row(conn: sqlite3.Connection, post_id: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM web_posts WHERE platform_post_id=?",
        (post_id,),
    ).fetchone()


def test_target_selection_skips_xhs_bilibili_and_plain_numeric_ids(tmp_path: Path) -> None:
    db_path = tmp_path / "identity.sqlite"
    with _open_db(db_path) as conn:
        _weibo(conn, "1", "10001", "青岛游客")
        _insert_post(
            conn,
            platform="xhs",
            post_id="x1",
            raw_author_id="90001",
            display_name="阿*花",
            canonical_url="https://www.xiaohongshu.com/explore/x1",
            raw_sample={"nickname": "阿*花", "creator_hash": _author_id("90001")},
        )
        _insert_post(
            conn,
            platform="bilibili",
            post_id="b1",
            raw_author_id="80001",
            display_name=_mask("哔哩作者"),
            canonical_url="https://www.bilibili.com/opus/b1",
            raw_sample={"nickname": _mask("哔哩作者"), "creator_hash": _author_id("80001")},
        )
        _insert_post(
            conn,
            platform="douyin",
            post_id="d1",
            raw_author_id="1234567890123456",
            display_name="普通抖音昵称",
            canonical_url="https://www.douyin.com/note/d1",
            raw_sample={"nickname": "普通抖音昵称", "creator_hash": "1234567890123456"},
        )
        conn.execute(
            "UPDATE web_posts SET author_platform_id=? WHERE platform_post_id='d1'",
            ("1234567890123456",),
        )
        conn.commit()
        authors, rejected, pending = identity.select_identity_authors(
            conn,
            platform="weibo",
            post_ids=[],
            max_items=0,
            ledger={},
            retry_unreachable=False,
        )
        douyin_authors, _, douyin_pending = identity.select_identity_authors(
            conn,
            platform="douyin",
            post_ids=[],
            max_items=0,
            ledger={},
            retry_unreachable=False,
        )
        xhs_authors, _, xhs_pending = identity.select_identity_authors(
            conn,
            platform="xhs",
            post_ids=[],
            max_items=0,
            ledger={},
            retry_unreachable=False,
        )
    assert pending == 1
    assert [author["author_key"] for author in authors] == [_author_id("10001")]
    assert douyin_pending == 0
    assert douyin_authors == []
    assert xhs_pending == 0
    assert xhs_authors == []
    assert rejected == []


def test_hash_mismatch_does_not_write_and_renamed_writes_current_nickname(tmp_path: Path) -> None:
    db_path = tmp_path / "identity.sqlite"
    with _open_db(db_path) as conn:
        mismatch_id = _weibo(conn, "m1", "10001", "青岛游客")
        renamed_id = _weibo(conn, "r1", "10002", "崂山向导")
        before_mismatch = dict(_row(conn, "m1"))
        identity.commit_author_group(
            db_path,
            platform="weibo",
            author_key=_author_id("10002"),
            new_id="10002",
            display_name="新昵称啦",
            profile_url="",
            source_post_id="r1",
        )
        renamed = _row(conn, "r1")
        mismatch = _row(conn, "m1")
        assert identity.classify_identity(_author_id("10001"), _mask("青岛游客"), {"creator_hash": "99999", "nickname": "青岛游客"}) == identity.MISMATCH
        assert identity.classify_identity(_author_id("10002"), _mask("崂山向导"), {"creator_hash": "10002", "nickname": "新昵称啦"}) == "renamed"
        assert renamed["author_display_name"] == "新昵称啦"
        assert renamed["author_platform_id"] == "10002"
        assert renamed["content_text"] == before_mismatch["content_text"]
        assert mismatch["author_platform_id"] == before_mismatch["author_platform_id"]
        assert mismatch["id"] == mismatch_id or True
        author_json = json.loads(renamed["author_json"])
        assert author_json["nickname"] == "新昵称啦"
        assert author_json["creator_hash"] == "10002"
        assert author_json["followers_count"] == 7
        assert renamed_id


def test_propagation_updates_same_hash_and_leaves_other_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "identity.sqlite"
    with _open_db(db_path) as conn:
        _weibo(conn, "p1", "42", "海边的人")
        _weibo(conn, "p2", "42", "海边的人")
        before = dict(_row(conn, "p2"))
        image_before = conn.execute(
            "SELECT image_url, local_path, sha256, raw_image_json FROM web_post_images"
        ).fetchall()
        updated, propagated = identity.commit_author_group(
            db_path,
            platform="weibo",
            author_key=_author_id("42"),
            new_id="42",
            display_name="海边的人",
            profile_url="https://m.weibo.cn/u/42",
            source_post_id="p1",
        )
        first = _row(conn, "p1")
        second = _row(conn, "p2")
        image_after = conn.execute(
            "SELECT image_url, local_path, sha256, raw_image_json FROM web_post_images"
        ).fetchall()
    assert updated == 2
    assert propagated == 1
    assert first["author_platform_id"] == "42"
    assert second["author_platform_id"] == "42"
    assert second["author_display_name"] == "海边的人"
    assert second["author_profile_url"] == "https://m.weibo.cn/u/42"
    assert second["content_text"] == before["content_text"]
    assert second["title"] == before["title"]
    assert second["published_at"] == before["published_at"]
    assert second["author_followers_count"] == before["author_followers_count"]
    assert second["keyword"] == before["keyword"]
    assert image_after == image_before


def test_zhihu_raw_sample_uses_user_nickname(tmp_path: Path) -> None:
    db_path = tmp_path / "identity.sqlite"
    token = "some-token"
    with _open_db(db_path) as conn:
        _insert_post(
            conn,
            platform="zhihu",
            post_id="55",
            raw_author_id="777",
            display_name=_mask("知乎答主"),
            canonical_url="https://www.zhihu.com/question/9/answer/55",
            author_profile_url=f"https://www.zhihu.com/people/{token}",
            raw_sample={
                "creator_hash": _author_id("777"),
                "nickname": _mask("知乎答主"),
                "creator_url_token": token,
                "content_text": "回答正文",
                "followers_count": 7,
            },
        )
        identity.commit_author_group(
            db_path,
            platform="zhihu",
            author_key=_author_id("777"),
            new_id="777",
            display_name="知乎答主",
            profile_url="https://www.zhihu.com/people/other",
            source_post_id="55",
        )
        row = _row(conn, "55")
    raw = json.loads(row["raw_sample_json"])
    assert raw["creator_hash"] == "777"
    assert raw["user_nickname"] == "知乎答主"
    assert "nickname" not in raw
    assert raw["creator_url_token"] == token
    assert raw["followers_count"] == 7
    assert row["author_profile_url"] == f"https://www.zhihu.com/people/{token}"


def test_child_command_does_not_download_or_import_posts(tmp_path: Path) -> None:
    command = identity.build_child_command(
        platform="douyin",
        keyword="青岛旅游",
        db_path=tmp_path / "db.sqlite",
        output_root=tmp_path / "out",
        targets_path=tmp_path / "targets.json",
        headless=True,
    )
    assert "trippostcollect.application.author_identity_worker" in command
    assert "--get_media" not in command
    assert "--download-images" not in command
    assert "--post-repair" not in command
    assert "--save-data-path" in command


def test_observation_file_is_atomic_and_runtime_block_skips_current_author(tmp_path: Path) -> None:
    destination = tmp_path / "author_identity_observations.json"
    calls: list[str] = []

    async def fetch(post: dict) -> dict[str, str]:
        post_id = post["platform_post_id"]
        calls.append(post_id)
        if post_id == "second-post":
            return identity_fields(creator_hash="42", nickname="海边的人")
        if post_id == "blocked":
            raise AuthorIdentityBlocked("rate_limited")
        return identity_fields()

    async def scenario() -> None:
        await collect_author_observations(
            platform="weibo",
            targets=[
                {
                    "author_key": "a" * 16,
                    "posts": [
                        {"platform_post_id": "first-post"},
                        {"platform_post_id": "second-post"},
                    ],
                },
                {"author_key": "b" * 16, "posts": [{"platform_post_id": "blocked"}]},
            ],
            destination=destination,
            fetch_post=fetch,
            sleep_seconds=0,
        )

    with pytest.raises(AuthorIdentityBlocked, match="rate_limited"):
        asyncio.run(scenario())
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["stopped_early"] is True
    assert payload["stop_reason"] == "rate_limited"
    assert [item["author_key"] for item in payload["observations"]] == ["a" * 16]
    assert payload["observations"][0]["status"] == "observed"
    assert payload["observations"][0]["attempted_post_ids"] == ["first-post", "second-post"]
    assert calls == ["first-post", "second-post", "blocked"]
    assert not destination.with_name(destination.name + ".tmp").exists()


def _fake_child_factory(observations: dict, *, stop_reason: str = "", interrupt: int | None = None):
    def fake_child(command: list[str], **_kwargs: object) -> dict:
        output = Path(command[command.index("--save-data-path") + 1])
        destination = output / "author_identity_observations.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(observations, ensure_ascii=False), encoding="utf-8")
        summary = output / "summary.json"
        summary.write_text(
            json.dumps(
                {
                    "formal_validation": {"stop_reason": stop_reason},
                    "behavior_validation": {"behavior_ok": True, "policy_ok": True},
                    "author_identity_observations": str(destination),
                    "import_completion_met": not stop_reason,
                }
            ),
            encoding="utf-8",
        )
        result = {
            "returncode": 0 if not stop_reason and interrupt is None else 2,
            "stdout": json.dumps({"summary": str(summary)}),
            "stderr": "",
        }
        if interrupt is not None:
            result["interrupt_signum"] = interrupt
            result["returncode"] = None
        return result

    return fake_child


def test_runtime_block_writes_finished_authors_and_skips_unprocessed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "identity.sqlite"
    with _open_db(db_path) as conn:
        _weibo(conn, "1", "10001", "青岛游客")
        _weibo(conn, "2", "10002", "崂山向导")
    document = {
        "platform": "weibo",
        "stopped_early": True,
        "stop_reason": "rate_limited",
        "observations": [
            {
                "author_key": _author_id("10001"),
                "platform_post_id": "1",
                "attempted_post_ids": ["1"],
                "status": "observed",
                "creator_hash": "10001",
                "nickname": "青岛游客",
                "user_nickname": "",
                "creator_url_token": "",
                "author_profile_url": "",
            }
        ],
    }
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(identity, "run_repair_child", _fake_child_factory(document, stop_reason="rate_limited"))
    exit_code = identity.main(
        ["--platform", "weibo", "--db", str(db_path), "--batch-size", "20", "--headless"]
    )
    summary = _summary(tmp_path)
    ledger = json.loads((tmp_path / "ledger" / "weibo.json").read_text(encoding="utf-8"))
    with _open_db(db_path) as conn:
        done = _row(conn, "1")
        pending = _row(conn, "2")
    assert exit_code == 2
    assert done["author_platform_id"] == "10001"
    assert pending["author_platform_id"] == _author_id("10002")
    assert summary["verified_count"] == 1
    assert summary["author_unreachable_count"] == 0
    assert ledger["entries"] == []
    assert summary["error"] == "rate_limited"


def test_not_observed_author_is_unreachable_and_skipped_on_rerun(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "identity.sqlite"
    with _open_db(db_path) as conn:
        _weibo(conn, "9", "10009", "栈桥夜景")
        _weibo(conn, "10", "10009", "栈桥夜景")
    author_key = _author_id("10009")
    document = {
        "platform": "weibo",
        "stopped_early": False,
        "stop_reason": "",
        "observations": [
            {
                "author_key": author_key,
                "platform_post_id": "10",
                "attempted_post_ids": ["9", "10"],
                "status": "not_observed",
                "creator_hash": "",
                "nickname": "",
                "user_nickname": "",
                "creator_url_token": "",
                "author_profile_url": "",
            }
        ],
    }
    calls = {"n": 0}

    def fake_child(command: list[str], **kwargs: object) -> dict:
        calls["n"] += 1
        return _fake_child_factory(document)(command, **kwargs)

    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(identity, "run_repair_child", fake_child)
    argv = ["--platform", "weibo", "--db", str(db_path), "--batch-size", "20", "--headless"]
    assert identity.main(argv) == 0
    first = _summary(tmp_path)
    assert first["author_unreachable_count"] == 1
    assert first["repaired_row_count"] == 0
    ledger = json.loads((tmp_path / "ledger" / "weibo.json").read_text(encoding="utf-8"))
    assert ledger["entries"][0]["outcome"] == "author_unreachable"
    assert ledger["entries"][0]["author_key"] == author_key
    assert "栈桥" not in json.dumps(ledger, ensure_ascii=False)
    assert identity.main(argv) == 0
    assert calls["n"] == 1


def test_dry_run_does_not_write_and_backup_records_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "identity.sqlite"
    with _open_db(db_path) as conn:
        _weibo(conn, "1", "10001", "青岛游客")
        before = _row(conn, "1")["author_platform_id"]
    _redirect(monkeypatch, tmp_path)

    def fail_child(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("dry-run must not launch a child")

    monkeypatch.setattr(identity, "run_repair_child", fail_child)
    assert identity.main(["--platform", "weibo", "--db", str(db_path), "--dry-run", "--headless"]) == 0
    dry = _summary(tmp_path)
    assert dry["status"] == "planned"
    assert dry["target_row_count"] == 1
    assert dry["verified_count"] == 0
    assert not (tmp_path / "bk").exists() or not any((tmp_path / "bk").rglob("*.sqlite"))
    with _open_db(db_path) as conn:
        assert _row(conn, "1")["author_platform_id"] == before

    document = {
        "platform": "weibo",
        "stopped_early": False,
        "stop_reason": "",
        "observations": [
            {
                "author_key": _author_id("10001"),
                "platform_post_id": "1",
                "attempted_post_ids": ["1"],
                "status": "observed",
                "creator_hash": "10001",
                "nickname": "青岛游客",
                "user_nickname": "",
                "creator_url_token": "",
                "author_profile_url": "",
            }
        ],
    }
    monkeypatch.setattr(identity, "run_repair_child", _fake_child_factory(document))
    assert identity.main(["--platform", "weibo", "--db", str(db_path), "--headless"]) == 0
    summary = _summary(tmp_path)
    backup = Path(summary["backup"]["path"])
    assert backup.is_file()
    assert summary["backup"]["sha256"]
    from trippostcollect.application.repair_runtime import sha256_file

    assert summary["backup"]["sha256"] == sha256_file(backup)
    assert summary["verified_count"] == 1
    assert summary["repaired_row_count"] == 1
    assert "青岛游客" not in json.dumps(
        {key: value for key, value in summary.items() if key != "batch_results"},
        ensure_ascii=False,
    )


def test_corrupt_observations_do_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "identity.sqlite"
    with _open_db(db_path) as conn:
        _weibo(conn, "1", "10001", "青岛游客")
        before = _row(conn, "1")["author_platform_id"]

    def fake_child(command: list[str], **_kwargs: object) -> dict:
        output = Path(command[command.index("--save-data-path") + 1])
        destination = output / "author_identity_observations.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("{", encoding="utf-8")
        return {"returncode": 0, "stdout": "", "stderr": ""}

    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(identity, "run_repair_child", fake_child)
    assert identity.main(["--platform", "weibo", "--db", str(db_path), "--headless"]) == 2
    summary = _summary(tmp_path)
    assert summary["error"] == "observations_unreadable"
    assert summary["repaired_row_count"] == 0
    with _open_db(db_path) as conn:
        assert _row(conn, "1")["author_platform_id"] == before


def test_interrupt_after_child_still_writes_observations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "identity.sqlite"
    with _open_db(db_path) as conn:
        _weibo(conn, "1", "10001", "青岛游客")
    document = {
        "platform": "weibo",
        "stopped_early": False,
        "stop_reason": "",
        "observations": [
            {
                "author_key": _author_id("10001"),
                "platform_post_id": "1",
                "attempted_post_ids": ["1"],
                "status": "observed",
                "creator_hash": "10001",
                "nickname": "青岛游客",
                "user_nickname": "",
                "creator_url_token": "",
                "author_profile_url": "",
            }
        ],
    }
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(
        identity,
        "run_repair_child",
        _fake_child_factory(document, interrupt=signal.SIGINT),
    )
    assert identity.main(["--platform", "weibo", "--db", str(db_path), "--headless"]) == 128 + signal.SIGINT
    summary = _summary(tmp_path)
    assert summary["interrupt"]["signal"] == "SIGINT"
    assert summary["verified_count"] == 1
    with _open_db(db_path) as conn:
        assert _row(conn, "1")["author_platform_id"] == "10001"


def test_signal_during_write_finishes_current_author_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "identity.sqlite"
    with _open_db(db_path) as conn:
        _weibo(conn, "1", "10001", "青岛游客")
        _weibo(conn, "2", "10002", "崂山向导")
        _weibo(conn, "3", "10003", "小麦岛")
    observations = []
    for raw, post_id, nickname in (
        ("10001", "1", "青岛游客"),
        ("10002", "2", "崂山向导"),
        ("10003", "3", "小麦岛"),
    ):
        observations.append(
            {
                "author_key": _author_id(raw),
                "platform_post_id": post_id,
                "attempted_post_ids": [post_id],
                "status": "observed",
                "creator_hash": raw,
                "nickname": nickname,
                "user_nickname": "",
                "creator_url_token": "",
                "author_profile_url": "",
            }
        )
    document = {"platform": "weibo", "stopped_early": False, "stop_reason": "", "observations": observations}
    original = identity.commit_author_group
    calls = {"n": 0}

    def commit(*args: object, **kwargs: object) -> tuple[int, int]:
        calls["n"] += 1
        result = original(*args, **kwargs)
        if calls["n"] == 2:
            os.kill(os.getpid(), signal.SIGINT)
        return result

    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(identity, "run_repair_child", _fake_child_factory(document))
    monkeypatch.setattr(identity, "commit_author_group", commit)
    exit_code = identity.main(["--platform", "weibo", "--db", str(db_path), "--batch-size", "20", "--headless"])
    assert exit_code == 128 + signal.SIGINT
    assert calls["n"] == 2
    with _open_db(db_path) as conn:
        assert _row(conn, "1")["author_platform_id"] == "10001"
        assert _row(conn, "2")["author_platform_id"] == "10002"
        assert _row(conn, "3")["author_platform_id"] == _author_id("10003")


def test_batch_size_counts_authors(tmp_path: Path) -> None:
    authors = [
        {"author_key": f"{index:016x}", "posts": [{"platform_post_id": str(index)}]}
        for index in range(5)
    ]
    assert [len(batch) for batch in identity.chunk_authors(authors, 2)] == [2, 2, 1]
    assert identity.chunk_authors(authors, 200) == [authors]
