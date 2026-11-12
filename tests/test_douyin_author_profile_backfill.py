"""#178：抖音作者资料回填只填空列，运行级阻断保留已写行。"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from test_author_profile_backfill import (
    AVATAR_URL,
    COOKIE_VALUE,
    _db,
    _insert,
    _run,
    _summary,
)
from trippostcollect.application import author_profile_backfill as backfill
from trippostcollect.platforms.douyin.profile_backfill import (
    author_profile_fields,
    backfill_worker_config,
    collect_author_profiles,
    validate_author_works,
)


def test_douyin_fields_come_from_parser_and_drop_avatar() -> None:
    author = {"uid": "9", "sec_uid": "MS4wLjABAAAAabc", "avatar_url": AVATAR_URL}
    profile = {
        "user": {
            "signature": "红瓦绿树",
            "avatar_url": AVATAR_URL,
            "sec_uid": "MS4wLjABAAAAabc",
            "enterprise_verify_reason": "",
            "custom_verify": "",
            "account_cert_info": "{}",
        }
    }
    fields = author_profile_fields(author, profile)
    assert fields["author_description"] == "红瓦绿树"
    assert fields["author_verified"] == 0
    assert fields["author_profile_url"] == "https://www.douyin.com/user/MS4wLjABAAAAabc"
    assert "sec_uid" not in fields
    assert AVATAR_URL not in json.dumps(fields, ensure_ascii=False)
    with pytest.raises(ValueError, match="aweme_id"):
        validate_author_works([
            {"author_platform_id": "9", "works": [{"aweme_id": "10", "platform_post_id": "11"}]},
        ])
    config = backfill_worker_config("青岛旅游", headless=True, save_path=Path("/tmp/unused"))
    assert config.ENABLE_GET_MEIDAS is False
    assert config.DY_SPECIFIED_ID_LIST == []
    assert config.CRAWLER_TYPE == "detail"


@pytest.mark.asyncio
async def test_douyin_observation_mismatch_deleted_and_runtime_stop() -> None:
    class Fake:
        def __init__(self) -> None:
            self.enriched: list[str] = []
            self.details = {
                "10": {"aweme_id": "10", "author": {"uid": "other", "sec_uid": "MS4wLjABAAAAabc"}},
                "11": {"aweme_id": "11", "is_delete": 1, "author": {"uid": "1"}},
                "12": None,
                "13": {
                    "aweme_id": "13",
                    "author": {"uid": "1", "sec_uid": "MS4wLjABAAAAok"},
                },
                "14": {
                    "aweme_id": "14",
                    "author": {"uid": "2", "sec_uid": "MS4wLjABAAAAnext"},
                },
            }

        async def get_aweme_detail(self, aweme_id: str, semaphore: object) -> dict | None:
            del semaphore
            return self.details[aweme_id]

        async def enrich_aweme_creator(self, aweme: dict) -> dict:
            self.enriched.append(str(aweme["aweme_id"]))
            if aweme["aweme_id"] == "14":
                aweme["creator_profile_runtime_error"] = "rate_limited"
                return aweme
            aweme["creator_profile"] = {
                "user": {
                    "signature": "简介",
                    "sec_uid": aweme["author"]["sec_uid"],
                    "enterprise_verify_reason": "官方",
                    "custom_verify": "",
                    "account_cert_info": json.dumps({"label_text": "官方"}),
                }
            }
            return aweme

    fake = Fake()
    mismatch = await collect_author_profiles(
        fake,
        [{"author_platform_id": "1", "works": [{"aweme_id": "10", "platform_post_id": "10"}]}],
    )
    assert mismatch["unrecovered"] == [{"author_platform_id": "1", "reason": "author_mismatch"}]
    assert fake.enriched == []
    deleted = await collect_author_profiles(
        fake,
        [{"author_platform_id": "1", "works": [
            {"aweme_id": "11", "platform_post_id": "11"},
            {"aweme_id": "12", "platform_post_id": "12"},
            {"aweme_id": "13", "platform_post_id": "13"},
        ]}],
    )
    assert deleted["observations"][0]["source_aweme_id"] == "13"
    assert fake.enriched == ["13"]
    stopped = await collect_author_profiles(
        fake,
        [
            {"author_platform_id": "1", "works": [{"aweme_id": "13", "platform_post_id": "13"}]},
            {"author_platform_id": "2", "works": [{"aweme_id": "14", "platform_post_id": "14"}]},
            {"author_platform_id": "3", "works": [{"aweme_id": "13", "platform_post_id": "13"}]},
        ],
    )
    assert stopped["runtime_blocker"] == "rate_limited"
    assert [item["author_platform_id"] for item in stopped["observations"]] == ["1"]
    assert "3" not in {item["author_platform_id"] for item in stopped["unrecovered"]}


def _douyin_row(connection: sqlite3.Connection, **overrides: object) -> int:
    values = {
        "platform_key": "douyin",
        "platform_post_id": "100",
        "source_type": "search",
        "source_url": "https://www.douyin.com/video/100",
        "canonical_url": "https://www.douyin.com/video/100",
        "author_platform_id": "author-1",
    }
    values.update(overrides)
    return _insert(connection, **values)


def _scripted_child(responses: list[dict]) -> object:
    pending = list(responses)

    def run(command: list[str], **_kwargs: object) -> dict:
        output = Path(command[command.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = pending.pop(0)
        output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return {
            "returncode": 0,
            "stdout": json.dumps({"child_summary": str(output.resolve())}),
            "stderr": "",
        }

    return run


def test_douyin_fills_nulls_keeps_existing_and_does_not_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        _douyin_row(connection, platform_post_id="100", created_at="2026-03-03 00:00:00")
        _douyin_row(
            connection,
            platform_post_id="101",
            author_platform_id="author-2",
            created_at="2026-03-02 00:00:00",
            author_description="原简介",
            content_text="原正文",
            author_followers_count=8,
        )
        connection.commit()
    observation = {
        "observations": [
            {
                "author_platform_id": "author-1",
                "source_aweme_id": "100",
                "fields": {
                    "author_profile_url": "https://www.douyin.com/user/MS4wLjABAAAAabc",
                    "author_description": "新简介",
                    "author_verified": 1,
                    "author_verified_text": "官方抖音号",
                },
                "author_verify_evidence": {"label_text": "官方抖音号", "avatar_url": AVATAR_URL},
                "cookie": COOKIE_VALUE,
            },
            {
                "author_platform_id": "author-2",
                "source_aweme_id": "101",
                "fields": {
                    "author_profile_url": "https://www.douyin.com/user/MS4wLjABAAAAdef",
                    "author_description": "不该覆盖",
                    "author_verified": 0,
                    "author_verified_text": None,
                },
                "author_verify_evidence": {"enterprise_verify_reason": ""},
            },
        ],
        "unrecovered": [],
        "runtime_blocker": "",
    }
    monkeypatch.setattr(backfill, "run_repair_child", _scripted_child([observation]))
    exit_code = _run(
        tmp_path,
        monkeypatch,
        ["--platform", "douyin", "--db", str(db_path), "--batch-size", "10", "--keyword", "青岛旅游"],
    )
    report = _summary(tmp_path)
    report_text = json.dumps(report, ensure_ascii=False)
    assert exit_code == 0
    assert report["changed_row_count"] == 2
    assert report["success_count"] == 2
    assert report["values_are_backfill_time"] is True
    assert AVATAR_URL not in report_text
    assert COOKIE_VALUE not in report_text
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 2
        first = connection.execute("SELECT * FROM web_posts WHERE platform_post_id='100'").fetchone()
        second = connection.execute("SELECT * FROM web_posts WHERE platform_post_id='101'").fetchone()
    assert first["author_description"] == "新简介"
    assert first["author_verified"] == 1
    assert first["author_profile_url"].endswith("MS4wLjABAAAAabc")
    assert first["content_text"] == "正文"
    assert first["author_followers_count"] == 3
    assert first["published_at"] == "2026-01-01T08:00:00+08:00"
    stored = json.loads(first["author_json"])
    assert stored["profile_backfill"]["source_aweme_id"] == "100"
    assert stored["profile_backfill"]["run_id"]
    assert "avatar_url" not in stored["profile_backfill"]["author_verify_evidence"]
    assert AVATAR_URL not in first["author_json"]
    assert COOKIE_VALUE not in first["author_json"]
    assert second["author_description"] == "原简介"
    assert second["author_verified"] == 0
    assert second["content_text"] == "原正文"
    assert second["author_followers_count"] == 8


def test_douyin_mismatch_keeps_row_and_runtime_block_keeps_earlier_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        _douyin_row(connection, platform_post_id="100", author_platform_id="author-1", created_at="2026-03-03 00:00:00")
        _douyin_row(connection, platform_post_id="200", author_platform_id="author-2", created_at="2026-03-02 00:00:00")
        _douyin_row(connection, platform_post_id="300", author_platform_id="author-3", created_at="2026-03-01 00:00:00")
        connection.commit()
    first = {
        "observations": [{
            "author_platform_id": "author-1",
            "source_aweme_id": "100",
            "fields": {
                "author_profile_url": "https://www.douyin.com/user/MS4wLjABAAAAabc",
                "author_description": "简介",
                "author_verified": 0,
            },
            "author_verify_evidence": {},
        }],
        "unrecovered": [],
        "runtime_blocker": "",
    }
    blocked = {
        "observations": [],
        "unrecovered": [{"author_platform_id": "author-2", "reason": "author_mismatch"}],
        "runtime_blocker": "rate_limited",
    }
    monkeypatch.setattr(backfill, "run_repair_child", _scripted_child([first, blocked]))
    exit_code = _run(
        tmp_path,
        monkeypatch,
        ["--platform", "douyin", "--db", str(db_path), "--batch-size", "1", "--keyword", "青岛旅游"],
    )
    report = _summary(tmp_path)
    assert exit_code == 2
    assert report["runtime_blocker"] == "rate_limited"
    assert report["changed_row_count"] == 1
    assert "author-2" in report["unrecovered_author_ids"]
    assert "author-3" not in report["unrecovered_author_ids"]
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 3
        assert connection.execute("SELECT author_description FROM web_posts WHERE platform_post_id='100'").fetchone()[0] == "简介"
        assert connection.execute("SELECT author_profile_url FROM web_posts WHERE platform_post_id='200'").fetchone()[0] is None
        assert connection.execute("SELECT content_text FROM web_posts WHERE platform_post_id='200'").fetchone()[0] == "正文"
        assert connection.execute("SELECT author_verified FROM web_posts WHERE platform_post_id='300'").fetchone()[0] is None


def test_douyin_dry_run_lists_batches_without_child_or_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        _douyin_row(connection, platform_post_id="100")
        _douyin_row(connection, platform_post_id="not-an-id", author_platform_id="author-2")
        connection.commit()

    def fail_child(*_args, **_kwargs):
        raise AssertionError("dry-run 不应启动 child")

    monkeypatch.setattr(backfill, "run_repair_child", fail_child)
    assert _run(
        tmp_path,
        monkeypatch,
        ["--platform", "douyin", "--db", str(db_path), "--batch-size", "1", "--dry-run"],
    ) == 0
    report = _summary(tmp_path)
    assert report["dry_run"] is True
    assert report["target_author_count"] == 2
    assert report["target_row_count"] == 2
    assert report["batch_plan"][0]["command"][2] == "trippostcollect.platforms.douyin.profile_backfill"
    assert "青岛旅游" in report["batch_plan"][0]["command"]
    assert report["unrecovered_by_reason"]["invalid_aweme_id"] == 1
    assert not list((tmp_path / "bk").glob("**/*"))
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(author_profile_url) FROM web_posts").fetchone()[0] == 0


def test_douyin_interrupt_keeps_rows_and_closes_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        _douyin_row(connection, platform_post_id="100")
        connection.commit()
    ready = tmp_path / "child-ready"
    child_code = (
        "import time; from pathlib import Path; "
        f"Path({str(ready)!r}).write_text('1'); time.sleep(30)"
    )
    monkeypatch.setattr(backfill, "CHILD_INTERRUPT_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(
        backfill,
        "build_child_command",
        lambda **_kwargs: [sys.executable, "-c", child_code],
    )

    def interrupt_after_ready() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=interrupt_after_ready, daemon=True)
    thread.start()
    try:
        exit_code = _run(
            tmp_path,
            monkeypatch,
            ["--platform", "douyin", "--db", str(db_path), "--batch-size", "1", "--keyword", "青岛旅游"],
        )
    finally:
        thread.join(timeout=2)
    report = _summary(tmp_path)
    state_path = next((tmp_path / "rt").glob("*/execution_state.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert exit_code == 128 + signal.SIGINT
    assert report["interrupt"]["signal"] == "SIGINT"
    assert state["status"] == "failed"
    assert state["steps"]["task_finalized"]["status"] == "failed"
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT COUNT(*) AS n, content_text, author_description FROM web_posts").fetchone()
    assert row["n"] == 1
    assert row["content_text"] == "正文"
    assert row["author_description"] is None
