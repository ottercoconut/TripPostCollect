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
    elif platform == "zhihu":
        row["source_url"] = f"https://www.zhihu.com/question/1/answer/{post_id}"
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
    return backfill.main(argv, verified_fields=xhs_parser.creator_verified_fields)


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


def _json_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _ordered_author(following: object) -> str:
    return _json_text(
        {
            "nickname": "青岛作者",
            "following_count": following,
            "followers_count": 9,
            "headline": "简介",
        }
    )


def _ordered_raw(following: object, content: str = "不要改正文") -> str:
    return _json_text(
        {
            "user_nickname": "青岛作者",
            "content_text": content,
            "following_count": following,
            "voteup_count": 0,
            "comment_count": 0,
            "meta": {"city": "青岛", "n": 1},
        }
    )


def _stored(db_path: Path, post_id: str) -> dict:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM web_posts WHERE platform_post_id=?",
            (post_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _clear_reports(tmp_path: Path) -> None:
    for name in ("out", "rt"):
        target = tmp_path / name
        if target.exists():
            shutil.rmtree(target)


def test_zhihu_following_zero_is_nulled_only_when_all_three_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="uniform",
            author_platform_id="author-uniform",
            author_display_name="青岛作者",
            author_followers_count=9,
            author_following_count=0,
            author_description="简介",
            content_text="不要改正文",
            author_json=_ordered_author(0),
            raw_sample_json=_ordered_raw(0),
        )
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="mismatch",
            author_platform_id="author-mismatch",
            author_display_name="不一致",
            author_followers_count=4,
            author_following_count=0,
            content_text="不一致正文",
            author_json=_ordered_author(5),
            raw_sample_json=_ordered_raw(0, "不一致正文"),
        )
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="nonzero",
            author_platform_id="author-nonzero",
            author_following_count=37,
            content_text="已有关注",
            author_json=_ordered_author(37),
            raw_sample_json=_ordered_raw(37, "已有关注"),
        )
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="already-null",
            author_platform_id="author-null",
            author_following_count=None,
            content_text="已经是空",
            author_json=_ordered_author(None),
            raw_sample_json=_ordered_raw(None, "已经是空"),
        )
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="invalid",
            author_platform_id="author-invalid",
            author_following_count=0,
            content_text="坏 JSON",
            author_json="not-json",
            raw_sample_json=_ordered_raw(0, "坏 JSON"),
        )
        # 修复部署后入库的三处 0 只能是平台给出的真实 0，不得改。
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="after-fix",
            author_platform_id="author-after-fix",
            author_following_count=0,
            content_text="修复后真实 0",
            created_at=backfill.ZHIHU_FOLLOWING_FIX_CREATED_BEFORE,
            author_json=_ordered_author(0),
            raw_sample_json=_ordered_raw(0, "修复后真实 0"),
        )
        connection.commit()
    post_ids = ("uniform", "mismatch", "nonzero", "already-null", "invalid", "after-fix")
    before = {post_id: _stored(db_path, post_id) for post_id in post_ids}

    def fail_child(*_args, **_kwargs):
        raise AssertionError("知乎离线修正不应访问平台")

    monkeypatch.setattr(backfill, "run_repair_child", fail_child)
    assert _run(tmp_path, monkeypatch, ["--platform", "zhihu", "--db", str(db_path), "--dry-run"]) == 0
    dry = _summary(tmp_path)
    assert dry["dry_run"] is True
    assert dry["status"] == "planned"
    assert dry["batch_plan"][0]["mode"] == "offline"
    assert dry["target_row_count"] == 1
    assert dry["target_author_count"] == 1
    assert dry["changed_row_count"] == 0
    assert dry["anomaly_count"] == 4
    assert {item["platform_post_id"] for item in dry["anomalies"]} == {"mismatch", "nonzero", "invalid", "after-fix"}
    assert not list((tmp_path / "bk").glob("**/*"))
    assert _stored(db_path, "uniform") == before["uniform"]
    _clear_reports(tmp_path)

    assert _run(tmp_path, monkeypatch, ["--platform", "zhihu", "--db", str(db_path)]) == 0
    report = _summary(tmp_path)
    assert report["changed_row_count"] == 1
    assert report["success_count"] == 1
    assert report["target_row_count"] == 1
    assert report["anomaly_count"] == 4
    assert report["backup"]["sha256"]
    states = list((tmp_path / "rt").glob("*/execution_state.json"))
    assert len(states) == 1
    assert json.loads(states[0].read_text(encoding="utf-8"))["status"] == "completed"
    reasons = {item["platform_post_id"]: item["reason"] for item in report["anomalies"]}
    assert "author_json=5" in reasons["mismatch"]
    assert "列=0" in reasons["mismatch"]
    assert "列=37" in reasons["nonzero"]
    assert "无法解析" in reasons["invalid"]
    assert "不自动改" in reasons["after-fix"]

    updated = _stored(db_path, "uniform")
    unchanged_columns = set(before["uniform"]) - {"author_following_count", "author_json", "raw_sample_json"}
    for column in unchanged_columns:
        assert updated[column] == before["uniform"][column], column
    assert updated["author_following_count"] is None
    assert updated["author_followers_count"] == 9
    assert updated["author_display_name"] == "青岛作者"
    assert updated["content_text"] == "不要改正文"
    author = json.loads(updated["author_json"])
    raw = json.loads(updated["raw_sample_json"])
    assert list(author) == ["nickname", "following_count", "followers_count", "headline"]
    assert author["following_count"] is None
    assert author["nickname"] == "青岛作者"
    assert author["followers_count"] == 9
    assert author["headline"] == "简介"
    assert "profile_backfill" not in author
    expected_author = json.loads(before["uniform"]["author_json"])
    expected_author["following_count"] = None
    assert updated["author_json"] == json.dumps(expected_author, ensure_ascii=False)
    expected_raw = json.loads(before["uniform"]["raw_sample_json"])
    expected_raw["following_count"] = None
    assert updated["raw_sample_json"] == json.dumps(expected_raw, ensure_ascii=False)
    assert raw["content_text"] == "不要改正文"
    assert raw["voteup_count"] == 0
    assert raw["comment_count"] == 0
    assert raw["meta"] == {"city": "青岛", "n": 1}
    for post_id in ("mismatch", "nonzero", "already-null", "invalid", "after-fix"):
        assert _stored(db_path, post_id) == before[post_id]
    _clear_reports(tmp_path)

    assert _run(tmp_path, monkeypatch, ["--platform", "zhihu", "--db", str(db_path)]) == 0
    again = _summary(tmp_path)
    assert again["target_row_count"] == 0
    assert again["target_author_count"] == 0
    assert again["changed_row_count"] == 0
    assert again["anomaly_count"] == 4
    assert _stored(db_path, "uniform")["author_following_count"] is None
    assert len(list((tmp_path / "bk").glob("*"))) == 1


def _org_sample(
    *,
    is_org: object = True,
    include_is_org: bool = True,
    author_verified: object = None,
    verified_text: str = "",
    content: str = "正文",
) -> str:
    badges: dict[str, object] = {"badgeV2": {"title": ""}}
    if include_is_org:
        badges["isOrg"] = is_org
    return json.dumps(
        {
            "content_text": content,
            "author_verified": author_verified,
            "verified_text": verified_text,
            "following_count": 4,
            "author_page_badges": badges,
            "meta": {"city": "青岛", "n": 1},
        },
        ensure_ascii=False,
    )


def _same_except(before: dict, after: dict, *changed: str) -> None:
    for column, value in before.items():
        if column not in changed:
            assert after[column] == value, column


def test_zhihu_org_identity_backfill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _db(tmp_path)
    described_author = json.dumps({"nickname": "有描述", "following_count": 4, "headline": "简介"}, ensure_ascii=False)
    blank_author = json.dumps({"nickname": "无描述", "following_count": 4}, ensure_ascii=False)
    with sqlite3.connect(db_path) as connection:
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="described",
            author_platform_id="author-described",
            author_display_name="有描述",
            author_followers_count=9,
            author_following_count=4,
            author_verified=1,
            author_verified_text="已认证机构号",
            content_text="有描述正文",
            author_json=described_author,
            raw_sample_json=_org_sample(
                author_verified=1,
                verified_text="已认证机构号",
                content="有描述正文",
            ),
        )
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="blank",
            author_platform_id="author-blank",
            author_display_name="无描述",
            author_followers_count=2,
            author_following_count=4,
            content_text="无描述正文",
            author_json=blank_author,
            raw_sample_json=_org_sample(content="无描述正文"),
        )
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="people",
            author_platform_id="author-people",
            author_verified=0,
            content_text="个人正文",
            author_json=json.dumps({"nickname": "个人", "following_count": 4}, ensure_ascii=False),
            raw_sample_json=_org_sample(is_org=False, author_verified=0, content="个人正文"),
        )
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="string-true",
            author_platform_id="author-string",
            content_text="字符串",
            author_json=json.dumps({"nickname": "字符串", "following_count": 4}, ensure_ascii=False),
            raw_sample_json=_org_sample(is_org="true", content="字符串"),
        )
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="missing",
            author_platform_id="author-missing",
            content_text="缺失",
            author_json=json.dumps({"nickname": "缺失", "following_count": 4}, ensure_ascii=False),
            raw_sample_json=_org_sample(include_is_org=False, content="缺失"),
        )
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="already",
            author_platform_id="author-already",
            author_verified=1,
            author_verified_text="机构号、已有",
            content_text="已写过",
            author_json=json.dumps({"nickname": "已写过", "following_count": 4}, ensure_ascii=False),
            raw_sample_json=_org_sample(
                author_verified=1,
                verified_text="机构号、已有",
                content="已写过",
            ),
        )
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="number",
            author_platform_id="author-number",
            author_verified=0,
            content_text="数字",
            author_json=json.dumps({"nickname": "数字", "following_count": 4}, ensure_ascii=False),
            raw_sample_json=_org_sample(is_org=1, author_verified=0, content="数字"),
        )
        connection.commit()
    post_ids = ("described", "blank", "people", "string-true", "missing", "already", "number")
    before = {post_id: _stored(db_path, post_id) for post_id in post_ids}

    def fail_child(*_args, **_kwargs):
        raise AssertionError("知乎机构号回填不应访问平台")

    monkeypatch.setattr(backfill, "run_repair_child", fail_child)
    assert _run(
        tmp_path,
        monkeypatch,
        ["--platform", "zhihu", "--org-identity", "--db", str(db_path), "--dry-run"],
    ) == 0
    dry = _summary(tmp_path)
    assert dry["dry_run"] is True
    assert dry["status"] == "planned"
    assert dry["mode"] == "org_identity"
    assert dry["target_row_count"] == 2
    assert dry["target_author_count"] == 2
    assert dry["changed_row_count"] == 0
    assert dry["verification_distribution"]["before"] == {
        "author_verified": {"1": 2, "0": 2, "null": 3},
        "author_verified_text": {"org_prefix": 1, "other": 1, "empty": 5},
    }
    assert dry["verification_distribution"]["after"] == dry["verification_distribution"]["before"]
    assert dry["column_counts"]["author_verified"] == {"before": 4, "after": 4}
    assert not list((tmp_path / "bk").glob("**/*"))
    for post_id in post_ids:
        assert _stored(db_path, post_id) == before[post_id]
    _clear_reports(tmp_path)

    assert _run(
        tmp_path,
        monkeypatch,
        ["--platform", "zhihu", "--org-identity", "--db", str(db_path)],
    ) == 0
    report = _summary(tmp_path)
    assert report["changed_row_count"] == 2
    assert report["target_row_count"] == 2
    assert report["target_author_count"] == 2
    assert report["success_count"] == 2
    assert report["verification_distribution"]["before"] == dry["verification_distribution"]["before"]
    assert report["verification_distribution"]["after"] == {
        "author_verified": {"1": 3, "0": 2, "null": 2},
        "author_verified_text": {"org_prefix": 3, "other": 0, "empty": 4},
    }
    assert report["column_counts"]["author_verified"] == {"before": 4, "after": 5}
    assert report["column_counts"]["author_verified_text"] == {"before": 2, "after": 3}
    assert report["backup"]["sha256"]
    states = list((tmp_path / "rt").glob("*/execution_state.json"))
    assert len(states) == 1
    assert json.loads(states[0].read_text(encoding="utf-8"))["status"] == "completed"

    described = _stored(db_path, "described")
    _same_except(before["described"], described, "author_verified", "author_verified_text", "raw_sample_json")
    assert described["author_verified"] == 1
    assert described["author_verified_text"] == "机构号、已认证机构号"
    assert described["author_json"] == described_author
    assert described["author_following_count"] == 4
    assert described["content_text"] == "有描述正文"
    described_raw = json.loads(described["raw_sample_json"])
    assert list(described_raw) == list(json.loads(before["described"]["raw_sample_json"]))
    assert described_raw["author_verified"] == 1
    assert described_raw["verified_text"] == "机构号、已认证机构号"
    assert described_raw["author_page_badges"]["isOrg"] is True
    assert described_raw["content_text"] == "有描述正文"
    assert described_raw["following_count"] == 4
    assert described_raw["meta"] == {"city": "青岛", "n": 1}

    blank = _stored(db_path, "blank")
    _same_except(before["blank"], blank, "author_verified", "author_verified_text", "raw_sample_json")
    assert blank["author_verified"] == 1
    assert blank["author_verified_text"] == "机构号"
    assert blank["author_json"] == blank_author
    blank_raw = json.loads(blank["raw_sample_json"])
    assert list(blank_raw) == list(json.loads(before["blank"]["raw_sample_json"]))
    assert blank_raw["author_verified"] == 1
    assert blank_raw["verified_text"] == "机构号"
    assert blank_raw["author_page_badges"] == {"badgeV2": {"title": ""}, "isOrg": True}
    for post_id in ("people", "string-true", "missing", "already", "number"):
        assert _stored(db_path, post_id) == before[post_id]
    _clear_reports(tmp_path)

    assert _run(
        tmp_path,
        monkeypatch,
        ["--platform", "zhihu", "--org-identity", "--db", str(db_path)],
    ) == 0
    again = _summary(tmp_path)
    assert again["target_row_count"] == 0
    assert again["target_author_count"] == 0
    assert again["changed_row_count"] == 0
    assert again["verification_distribution"]["before"] == again["verification_distribution"]["after"]
    assert _stored(db_path, "described")["author_verified_text"] == "机构号、已认证机构号"
    assert _stored(db_path, "blank")["author_verified_text"] == "机构号"
    assert len(list((tmp_path / "bk").glob("*"))) == 1


def test_zhihu_org_identity_and_following_do_not_mix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _db(tmp_path)
    org_author = json.dumps({"nickname": "机构", "following_count": 4}, ensure_ascii=False)
    zero_author = json.dumps({"nickname": "关注为零", "following_count": 0}, ensure_ascii=False)
    zero_raw = json.dumps(
        {
            "content_text": "关注为零",
            "following_count": 0,
            "author_verified": 0,
            "verified_text": "",
            "author_page_badges": {"isOrg": False},
        },
        ensure_ascii=False,
    )
    with sqlite3.connect(db_path) as connection:
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="org",
            author_platform_id="author-org",
            author_following_count=4,
            author_verified=1,
            author_verified_text="已认证机构号",
            content_text="机构正文",
            author_json=org_author,
            raw_sample_json=_org_sample(
                author_verified=1,
                verified_text="已认证机构号",
                content="机构正文",
            ),
        )
        _insert(
            connection,
            platform_key="zhihu",
            platform_post_id="zero",
            author_platform_id="author-zero",
            author_following_count=0,
            author_verified=0,
            content_text="关注为零",
            created_at="2026-11-13 00:00:00",
            author_json=zero_author,
            raw_sample_json=zero_raw,
        )
        connection.commit()

    def fail_child(*_args, **_kwargs):
        raise AssertionError("知乎离线修正不应访问平台")

    monkeypatch.setattr(backfill, "run_repair_child", fail_child)
    assert _run(
        tmp_path,
        monkeypatch,
        ["--platform", "zhihu", "--org-identity", "--db", str(db_path)],
    ) == 0
    org = _stored(db_path, "org")
    zero = _stored(db_path, "zero")
    assert org["author_verified"] == 1
    assert org["author_verified_text"] == "机构号、已认证机构号"
    assert org["author_following_count"] == 4
    assert org["author_json"] == org_author
    assert json.loads(org["raw_sample_json"])["following_count"] == 4
    assert zero["author_following_count"] == 0
    assert zero["author_verified"] == 0
    assert zero["author_verified_text"] is None
    assert zero["raw_sample_json"] == zero_raw
    _clear_reports(tmp_path)

    assert _run(tmp_path, monkeypatch, ["--platform", "zhihu", "--db", str(db_path)]) == 0
    org_after = _stored(db_path, "org")
    zero_after = _stored(db_path, "zero")
    assert org_after["author_verified_text"] == "机构号、已认证机构号"
    assert org_after["author_following_count"] == 4
    assert org_after["author_json"] == org_author
    assert zero_after["author_following_count"] is None
    assert zero_after["author_verified"] == 0
    assert zero_after["author_verified_text"] is None
    assert json.loads(zero_after["author_json"])["following_count"] is None
    assert json.loads(zero_after["raw_sample_json"])["author_page_badges"] == {"isOrg": False}


def test_org_identity_switch_requires_zhihu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _redirect(monkeypatch, tmp_path)
    with pytest.raises(SystemExit, match="只适用于 --platform zhihu"):
        backfill.main(["--platform", "xhs", "--org-identity", "--db", str(tmp_path / "none.sqlite")])
