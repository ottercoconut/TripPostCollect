from __future__ import annotations

import json
import sqlite3
import signal
import subprocess
import sys
from importlib import import_module
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

repair = import_module("repair_post_details")
mediacrawler = import_module("mediacrawler_crawl")


@pytest.mark.parametrize(
    ("platform", "post_id", "url", "expected"),
    [
        (
            "douyin",
            "123",
            "https://www.douyin.com/video/123?previous_page=search_result",
            "https://www.douyin.com/video/123",
        ),
        (
            "weibo",
            "456",
            "https://m.weibo.cn/detail/456",
            "456",
        ),
        (
            "zhihu",
            "789",
            "https://www.zhihu.com/question/12/answer/789?utm_source=test",
            "https://www.zhihu.com/question/12/answer/789",
        ),
        (
            "zhihu",
            "321",
            "https://zhuanlan.zhihu.com/p/321",
            "https://zhuanlan.zhihu.com/p/321",
        ),
    ],
)
def test_detail_target_is_strictly_bound_to_platform_id(
    platform: str,
    post_id: str,
    url: str,
    expected: str,
) -> None:
    assert repair._detail_target(platform, post_id, url) == (expected, "")
    target, reason = repair._detail_target(platform, "different", url)
    assert target is None
    assert reason.startswith("invalid_")


def test_select_targets_excludes_observed_and_reports_bad_urls(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "repair.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE web_posts(
            id INTEGER PRIMARY KEY,
            platform_key TEXT,
            platform_post_id TEXT,
            canonical_url TEXT,
            keyword TEXT,
            raw_sample_json TEXT,
            artifact_dir TEXT,
            published_at TEXT,
            author_followers_count INTEGER,
            author_display_name TEXT,
            author_platform_id TEXT,
            author_profile_url TEXT,
            author_description TEXT
        )
        """
    )
    rows = [
        (1, "1", "https://www.douyin.com/video/1", "青岛旅游", "{}"),
        (
            2,
            "2",
            "https://www.douyin.com/video/2",
            "青岛旅游",
            json.dumps({"content_detail_status": "detail_observed"}),
        ),
        (3, "3", "https://example.test/video/3", "青岛旅游", "not-json"),
    ]
    conn.executemany(
        """
        INSERT INTO web_posts(
            id, platform_key, platform_post_id, canonical_url, keyword,
            raw_sample_json, artifact_dir
        ) VALUES (?, 'douyin', ?, ?, ?, ?, '')
        """,
        rows,
    )
    conn.commit()

    targets, rejected, pending = repair.select_targets(
        conn,
        platform="douyin",
        post_ids=[],
        max_items=0,
    )

    assert pending == 2
    assert [item["platform_post_id"] for item in targets] == ["1"]
    assert rejected[0]["platform_post_id"] == "3"
    assert rejected[0]["reason"] == "invalid_douyin_canonical_url"
    conn.close()


def test_select_targets_carries_existing_repair_metadata(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "repair.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE web_posts(
            id INTEGER PRIMARY KEY,
            platform_key TEXT,
            platform_post_id TEXT,
            canonical_url TEXT,
            keyword TEXT,
            raw_sample_json TEXT,
            artifact_dir TEXT,
            published_at TEXT,
            author_followers_count INTEGER,
            author_display_name TEXT,
            author_platform_id TEXT,
            author_profile_url TEXT,
            author_description TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO web_posts(
            id, platform_key, platform_post_id, canonical_url, keyword,
            raw_sample_json, artifact_dir, published_at, author_followers_count,
            author_display_name, author_platform_id, author_profile_url,
            author_description
        ) VALUES (1, 'zhihu', 'answer-1',
            'https://www.zhihu.com/question/1/answer/answer-1', '青岛旅游', ?, '',
            '2026-07-13T12:00:00+08:00', 123, '作者', 'author-1',
            'https://www.zhihu.com/people/author-1', '简介')
        """,
        (
            json.dumps(
                {
                    "content_type": "answer",
                    "created_time": 1780000000,
                    "followers_count": 123,
                    "followers_observed": True,
                    "author_followers_source": "search_author",
                    "creator_hash": "hash-1",
                    "user_nickname": "作***",
                }
            ),
        ),
    )
    conn.commit()

    targets, rejected, pending = repair.select_targets(
        conn,
        platform="zhihu",
        post_ids=[],
        max_items=0,
    )

    assert pending == 1
    assert not rejected
    assert targets[0]["repair_fallback"]["published_at"] == "2026-07-13T12:00:00+08:00"
    assert targets[0]["repair_fallback"]["followers_observed"] is True
    assert targets[0]["repair_fallback"]["author_followers_source"] == "search_author"
    conn.close()


@pytest.mark.parametrize(
    ("platform", "detail_target"),
    [
        ("douyin", "https://www.douyin.com/video/123"),
        ("weibo", "123"),
        ("zhihu", "https://www.zhihu.com/question/1/answer/123"),
    ],
)
def test_load_post_repair_targets_accepts_platform_specific_detail_values(
    tmp_path: Path,
    platform: str,
    detail_target: str,
) -> None:
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps(
            [
                {
                    "platform_post_id": "123",
                    "detail_target": detail_target,
                    "keyword": "青岛旅游",
                }
            ]
        ),
        encoding="utf-8",
    )

    targets = mediacrawler.load_post_repair_targets(path, platform)

    assert targets[0]["platform_post_id"] == "123"
    assert targets[0]["keyword"] == "青岛旅游"


def test_load_post_repair_targets_rejects_cross_id_target(tmp_path: Path) -> None:
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps(
            [
                {
                    "platform_post_id": "123",
                    "detail_target": "https://www.douyin.com/video/999",
                    "keyword": "青岛旅游",
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="does not match douyin ID 123"):
        mediacrawler.load_post_repair_targets(path, "douyin")


def test_repair_fallback_makes_sparse_zhihu_detail_formally_valid() -> None:
    detail = {
        "content_id": "answer-1",
        "content_type": "answer",
        "content_url": "https://www.zhihu.com/question/1/answer/answer-1",
        "content_text": "完整正文",
        "content_detail_status": "detail_observed",
        "content_detail_source": "answer_detail",
        "image_list": ["https://example.test/body.jpg"],
        "creator_hash": "hash-1",
        "user_nickname": "作***",
        "followers_count": 0,
        "followers_observed": False,
        "author_followers_source": "missing",
        "voteup_count": 1,
        "comment_count": 2,
    }
    merged = mediacrawler.merge_repair_fallback_metadata(
        "zhihu",
        detail,
        {
            "published_at": "2026-07-13T12:00:00+08:00",
            "followers_count": 123,
            "author_followers_count": 123,
            "followers_observed": True,
            "author_followers_source": "search_author",
        },
    )

    validation = mediacrawler.validate_formal_record("zhihu", merged, set())
    assert validation["valid"] is True
    assert merged["content_detail_source"] == "answer_detail"
    assert merged["content_text"] == "完整正文"


def test_build_child_command_disables_discovery_writes(tmp_path: Path) -> None:
    command = repair.build_child_command(
        platform="weibo",
        keyword="青岛旅游",
        db_path=tmp_path / "db.sqlite",
        output_root=tmp_path / "output",
        targets_path=tmp_path / "targets.json",
        target_count=4,
        timeout_seconds=600,
        headless=False,
    )

    assert command[command.index("--platforms") + 1] == "weibo"
    assert command[command.index("--candidate-hard-limit") + 1] == "4"
    assert "--post-repair" in command
    assert "--repair-targets-file" in command
    assert "--download-images" in command
    assert "--no-checkpoint-write" in command
    assert "--headed" in command


def test_repaired_rows_requires_trusted_detail_and_local_images(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "repair.sqlite")
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
    conn.execute(
        """
        INSERT INTO web_posts(id, platform_key, platform_post_id, content_text, raw_sample_json)
        VALUES (1, 'weibo', 'post-1', '完整正文', ?)
        """,
        (
            json.dumps(
                {
                    "content_detail_status": "detail_observed",
                    "content_detail_source": "mobile_detail",
                }
            ),
        ),
    )
    conn.execute(
        """
        INSERT INTO web_post_images(web_post_id, image_role, local_path, sha256)
        VALUES (1, 'content', 'data/media/weibo/post-1/0.jpg', 'abc')
        """
    )
    conn.commit()

    statuses = repair.repaired_rows(
        conn,
        "weibo",
        ["post-1", *[f"missing-{index}" for index in range(1_100)]],
    )

    assert statuses["post-1"]["recovered"] is True
    conn.close()


def test_sqlite_backup_is_consistent_and_hashed(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    destination = tmp_path / "backups" / "source.sqlite"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('ok')")
        conn.commit()

    result = repair.create_sqlite_backup(source, destination)

    assert result["quick_check"] == ["ok"]
    assert result["foreign_key_error_count"] == 0
    assert len(result["sha256"]) == 64
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "ok"


def test_partial_generic_repair_can_commit_valid_subset() -> None:
    assert mediacrawler.repair_partial_child_execution_allowed(
        repair_mode=True,
        child_execution_ok=False,
        validation={"valid_total_count": 1},
        image_materialization={"complete": True},
        behavior_validation={"ok": True},
    ) is True
    assert mediacrawler.repair_partial_child_execution_allowed(
        repair_mode=True,
        child_execution_ok=False,
        runtime_blocked=True,
        validation={"valid_total_count": 1},
        image_materialization={"complete": True},
        behavior_validation={"ok": True},
    ) is False


def test_run_repair_child_timeout_uses_formal_process_group_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int, int | None]] = []

    class FakeProcess:
        pid = 4242
        returncode = None

        def communicate(self, *, timeout: int | None = None) -> tuple[str, str]:
            if timeout == 17:
                raise subprocess.TimeoutExpired(
                    ["child"], timeout, output="before", stderr="warning"
                )
            self.returncode = 124
            return "after", "tail"

    process = FakeProcess()

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        del command
        events.append(("popen", int(kwargs["start_new_session"]), int(kwargs["text"])))
        return process

    def fake_killpg(pid: int, sig: int) -> None:
        events.append(("killpg", pid, sig))

    monkeypatch.setattr(repair.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(repair.os, "killpg", fake_killpg)

    result = repair.run_repair_child(
        ["child"],
        cwd=ROOT,
        timeout_seconds=17,
    )

    assert result["timed_out"] is True
    assert result["returncode"] == 124
    assert result["stdout"] == "beforeafter"
    assert result["stderr"] == "warningtail"
    assert events == [("popen", 1, 1), ("killpg", 4242, signal.SIGTERM)]


@pytest.mark.parametrize(
    ("error", "strict"),
    [
        ("post_detail_repair_batch_timeout:2040", False),
        ("missing_child_summary_exit_2", False),
        ("runtime_failed", False),
        ("repair_no_valid_detail", False),
        ("login_required", True),
        ("captcha_detected", True),
        ("crawl_policy_evidence_failed", True),
        ("sqlite_import_failed", True),
    ],
)
def test_strict_batch_blocker_only_stops_unattended_repair(error: str, strict: bool) -> None:
    assert repair._strict_batch_blocker(error) is strict
