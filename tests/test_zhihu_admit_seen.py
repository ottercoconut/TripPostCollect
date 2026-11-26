"""#209 知乎已见未入库回答按 ID 准入。临时 SQLite，不访问平台。"""

from __future__ import annotations

import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path

import pytest

from trippostcollect.application.repair import (
    load_post_repair_targets,
    post_repair_pagination_evidence,
    post_repair_successful_identities,
)
from trippostcollect.db.bootstrap import bootstrap_database


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

repair = import_module("repair_post_details")
mediacrawler = import_module("mediacrawler_crawl")


def _redirect(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_OUTPUT", tmp_path / "out")
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_RUNTIME_ROOT", tmp_path / "rt")
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_BACKUP_ROOT", tmp_path / "bk")
    monkeypatch.setattr(repair, "POST_DETAIL_REPAIR_PAUSE_ROOT", tmp_path / "rt" / "pause")


def _job(conn, job_id: int, keyword: str | None, *, site: str = "zhihu") -> None:
    params = {} if keyword is None else {"keyword": keyword}
    conn.execute(
        """
        INSERT INTO crawl_jobs (
            id, job_key, site_key, target_url, job_kind, next_run_at, params_json
        ) VALUES (?, ?, ?, '', 'mediacrawler_search', '2026-01-01T00:00:00', ?)
        """,
        (job_id, f"{site}-{job_id}", site, json.dumps(params, ensure_ascii=False)),
    )


def _seen(conn, job_id: int, post_id: str, seen_at: str, *, platform: str = "zhihu", fingerprint: str = "fp") -> None:
    conn.execute(
        """
        INSERT INTO crawl_discovery_seen_candidates (
            job_id, platform_key, query_fingerprint, platform_post_id,
            first_run_id, last_run_id, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, 'run', 'run', ?, ?)
        """,
        (job_id, platform, fingerprint, post_id, seen_at, seen_at),
    )


def _seed(db_path: Path) -> None:
    bootstrap_database(db_path, sync_jobs=False)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _job(conn, 25, "青岛老城")
        _job(conn, 29, "崂山风景")
        _job(conn, 39, None)
        _job(conn, 40, "青岛", site="weibo")
        _seen(conn, 29, "201", "2021-01-01T00:00:00")
        _seen(conn, 25, "201", "2020-01-01T00:00:00")
        _seen(conn, 25, "201", "2019-01-01T00:00:00", fingerprint="older-but-same-job")
        _seen(conn, 25, "abc", "2020-01-01T00:00:00")
        _seen(conn, 25, "202", "2020-02-01T00:00:00")
        _seen(conn, 25, "203", "2020-03-01T00:00:00")
        _seen(conn, 25, "204", "2020-04-01T00:00:00")
        _seen(conn, 39, "206", "2020-06-01T00:00:00")
        _seen(conn, 25, "207", "2022-01-01T00:00:00")
        _seen(conn, 25, "208", "2023-01-01T00:00:00")
        _seen(conn, 25, "300", "2020-01-01T00:00:00", platform="weibo")
        conn.execute(
            """
            INSERT INTO web_posts (
                platform_key, platform_post_id, source_type, source_url, captured_at
            ) VALUES ('zhihu', '202', 'search', 'https://www.zhihu.com/answer/202', '2026-01-01T00:00:00+08:00')
            """
        )
        conn.execute(
            """
            INSERT INTO crawl_discovery_candidate_exclusions (
                job_id, platform_key, query_fingerprint, platform_post_id,
                reason, authorized_run_id
            ) VALUES (29, 'zhihu', 'other', '203', 'operator', 'run')
            """
        )
        conn.execute(
            """
            INSERT INTO crawl_discovery_checkpoints (
                job_id, platform_key, keyword, query_fingerprint
            ) VALUES (25, 'zhihu', '青岛老城', 'fp')
            """
        )
        conn.commit()


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _select(conn, **kwargs):
    params = {
        "job_ids": [25, 29, 39],
        "post_ids": [],
        "max_items": 0,
        "exclude_ids": set(),
    }
    params.update(kwargs)
    return repair.select_admit_seen_targets(conn, **params)


def test_admit_selection_dedupes_and_applies_each_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "admit.sqlite"
    _seed(db_path)
    with _open(db_path) as conn:
        targets, rejected, pending = _select(conn)

    by_id = {item["platform_post_id"]: item for item in targets}
    assert set(by_id) == {"201", "204", "207", "208"}
    assert pending == 4
    assert by_id["201"]["keyword"] == "青岛老城"
    assert by_id["201"]["detail_target"] == "https://www.zhihu.com/answer/201"
    assert by_id["201"]["admit"] is True
    assert by_id["201"]["repair_fallback"] == {}
    assert [item["platform_post_id"] for item in rejected] == ["206"]
    assert rejected[0]["reason"] == "admit_keyword_missing"
    assert "abc" not in by_id
    assert "202" not in by_id
    assert "203" not in by_id
    assert "300" not in by_id

    with _open(db_path) as conn:
        narrowed, _rejected, pending = _select(conn, post_ids=["208", "missing"])
    assert [item["platform_post_id"] for item in narrowed] == ["208"]
    assert pending == 1

    with _open(db_path) as conn:
        limited, _rejected, pending = _select(conn, max_items=1)
    assert [item["platform_post_id"] for item in limited] == ["201"]
    assert pending == 4

    with _open(db_path) as conn:
        continued, _rejected, _pending = _select(conn, exclude_ids={"204", "201"})
    assert {item["platform_post_id"] for item in continued} == {"207", "208"}

    with _open(db_path) as conn:
        conn.execute(
            """
            INSERT INTO web_posts (
                platform_key, platform_post_id, source_type, source_url, captured_at
            ) VALUES ('zhihu', '207', 'search', 'https://www.zhihu.com/answer/207', '2026-01-02T00:00:00+08:00')
            """
        )
        again, _rejected, _pending = _select(conn, exclude_ids={"204"})
    assert {item["platform_post_id"] for item in again} == {"201", "208"}


def test_exclude_file_must_be_a_string_array(tmp_path: Path) -> None:
    path = tmp_path / "ids.json"
    path.write_text(json.dumps(["204", " 201 "]), encoding="utf-8")
    assert repair.load_exclude_post_ids(str(path)) == {"204", "201"}
    path.write_text(json.dumps([204]), encoding="utf-8")
    with pytest.raises(SystemExit, match="JSON array of strings"):
        repair.load_exclude_post_ids(str(path))


def test_admit_job_must_exist_and_be_zhihu(tmp_path: Path) -> None:
    db_path = tmp_path / "admit.sqlite"
    _seed(db_path)
    with _open(db_path) as conn:
        with pytest.raises(SystemExit, match="not found"):
            _select(conn, job_ids=[99])
        with pytest.raises(SystemExit, match="not zhihu"):
            _select(conn, job_ids=[40])


def test_bare_answer_url_requires_admit_mode_and_flag(tmp_path: Path) -> None:
    path = tmp_path / "targets.json"
    bare = {
        "platform_post_id": "101",
        "detail_target": "https://www.zhihu.com/answer/101",
        "keyword": "青岛老城",
        "admit": True,
        "repair_fallback": {"published_at": "2020-01-01T00:00:00+08:00", "followers_count": 9},
    }
    path.write_text(json.dumps([bare]), encoding="utf-8")
    with pytest.raises(SystemExit, match="does not match zhihu"):
        load_post_repair_targets(path, "zhihu")
    loaded = load_post_repair_targets(path, "zhihu", db_path=tmp_path / "missing.sqlite", admit=True)
    assert loaded == [{
        "platform_post_id": "101",
        "detail_target": "https://www.zhihu.com/answer/101",
        "keyword": "青岛老城",
        "admit": True,
    }]
    canonical = {
        "platform_post_id": "101",
        "detail_target": "https://www.zhihu.com/question/10/answer/101",
        "keyword": "青岛老城",
    }
    path.write_text(json.dumps([canonical]), encoding="utf-8")
    assert load_post_repair_targets(path, "zhihu")[0]["detail_target"].endswith("/answer/101")
    with pytest.raises(SystemExit, match="admit: true"):
        load_post_repair_targets(path, "zhihu", admit=True)
    with pytest.raises(SystemExit, match="zhihu"):
        load_post_repair_targets(path, "weibo", admit=True)


@pytest.mark.parametrize(
    "argv",
    [
        ["repair_post_details.py", "--platform", "weibo", "--admit-seen", "--admit-seen-job", "25"],
        ["repair_post_details.py", "--platform", "zhihu", "--admit-seen"],
        ["repair_post_details.py", "--platform", "zhihu", "--admit-seen-job", "25"],
        ["repair_post_details.py", "--platform", "zhihu", "--exclude-post-ids-file", "ids.json"],
        [
            "repair_post_details.py", "--platform", "zhihu", "--admit-seen", "--admit-seen-job", "25",
            "--refresh-all",
        ],
        [
            "repair_post_details.py", "--platform", "zhihu", "--admit-seen", "--admit-seen-job", "25",
            "--dedupe-images",
        ],
        [
            "repair_post_details.py", "--platform", "zhihu", "--admit-seen", "--admit-seen-job", "25",
            "--keep-stored-images",
        ],
    ],
)
def test_admit_flags_exit_before_touching_a_database(monkeypatch, argv: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit):
        repair.main()


def test_new_identity_counts_as_success_only_for_admit() -> None:
    validation = {"existing_identities": [], "new_identities": ["zhihu:id:101"]}
    assert post_repair_successful_identities(validation) == set()
    assert post_repair_successful_identities(validation, admit=True) == {"zhihu:id:101"}
    denied = post_repair_pagination_evidence(
        [{"platform_post_id": "101"}],
        platform="zhihu",
        successful_identities=post_repair_successful_identities(validation),
    )
    assert denied["skipped_candidate_failures"][0]["error_code"] == "repair_target_no_valid_output"
    assert "unavailable_candidate_ids" not in denied
    allowed = post_repair_pagination_evidence(
        [{"platform_post_id": "101"}, {"platform_post_id": "102"}],
        platform="zhihu",
        successful_identities=post_repair_successful_identities(validation, admit=True),
        target_outcomes=[{
            "platform_post_id": "102",
            "error_code": "admit_redirect_home",
            "evidence_source": "zhihu_admit",
            "attempts": 1,
            "retryable": False,
        }],
    )
    assert allowed["skipped_candidate_count"] == 0
    assert allowed["successful_candidate_count"] == 1
    assert allowed["unavailable_candidate_ids"] == ["102"]


def test_all_unavailable_is_not_no_progress() -> None:
    assert repair.adjust_admit_persistence_failure(
        "post_detail_repair_no_progress",
        admit=True,
        target_ids=["101"],
        unavailable_ids=["101"],
        candidate_failures=[],
    ) == ""
    assert repair.adjust_admit_persistence_failure(
        "post_detail_repair_no_progress",
        admit=True,
        target_ids=["101"],
        unavailable_ids=[],
        candidate_failures=[{"platform_post_id": "101"}],
    ) == "post_detail_repair_no_progress"
    assert repair.adjust_admit_persistence_failure(
        "post_detail_repair_no_progress",
        admit=False,
        target_ids=["101"],
        unavailable_ids=["101"],
        candidate_failures=[],
    ) == "post_detail_repair_no_progress"


def _record(content_id: str, text: str) -> dict:
    return {
        "content_id": content_id,
        "content_type": "answer",
        "question_id": "10",
        "title": "青岛问题",
        "content_text": text,
        "source_keyword": "青岛老城",
        "content_detail_status": "detail_observed",
        "content_detail_source": "answer_detail",
        "image_list": [],
        "created_time": 1_700_000_000,
        "author_platform_id": "author-1",
        "user_nickname": "作者",
        "followers_count": 8,
        "followers_observed": True,
        "author_followers_source": "author_page",
        "voteup_count": 1,
        "comment_count": 2,
        "collected_count": 3,
        "content_visit_refs": {"images": []},
    }


def _discovery_counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        names = (
            "crawl_discovery_seen_candidates",
            "crawl_discovery_candidate_exclusions",
            "crawl_discovery_checkpoints",
        )
        return {name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]) for name in names}


def test_new_row_imports_through_the_formal_gate_without_discovery_writes(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    media_root = project_root / "media"
    db_path = project_root / "formal.sqlite"
    _seed(db_path)
    before = _discovery_counts(db_path)
    record = _record("501", "青岛纯文字正文")
    jsonl = project_root / "zhihu" / "zhihu_contents_501.jsonl"
    jsonl.parent.mkdir()
    jsonl.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    validation, selected = mediacrawler.collect_formal_records(
        {"records": [{"output": {"jsonl_files": [str(jsonl)]}}], "keyword": "青岛旅游"},
        db_path=db_path,
        repair_mode=True,
        repair_metadata_by_identity={
            "zhihu:id:501": {
                "keyword": "青岛老城",
                "published_at": "1999-01-01T00:00:00+08:00",
                "followers_count": 999,
                "author_followers_source": "author_page",
                "followers_observed": True,
            },
        },
        allowed_identities={"zhihu:id:501"},
    )
    assert validation["valid_new_count"] == 1
    assert validation["repair_import_met"] is True
    kept = selected[0]["record"]
    assert kept["source_keyword"] == "青岛老城"
    assert kept["followers_count"] == 8
    assert kept["created_time"] == 1_700_000_000
    _report, materialized, _complete = mediacrawler.materialize_formal_record_images(
        selected,
        project_root=project_root,
        media_root=media_root,
        promote=True,
    )
    selected[0]["materialized_images"] = materialized[selected[0]["identity"]]
    imported = mediacrawler.import_valid_records(
        {"captured_at": "2026-11-25T00:00:00+00:00", "keyword": "青岛旅游", "batch_dir": str(project_root)},
        selected,
        db_path,
        project_root=project_root,
        media_root=media_root,
        require_local_images=True,
        skip_existing=True,
    )
    assert imported["inserted_rows"] == 1
    assert imported["updated_rows"] == 0
    assert imported["already_present_rows"] == 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT keyword, content_text, post_images_count,
                   json_extract(raw_sample_json, '$.content_detail_status'),
                   json_extract(raw_sample_json, '$.content_detail_source'),
                   author_followers_count
            FROM web_posts WHERE platform_post_id = '501'
            """
        ).fetchone()
        images = conn.execute("SELECT COUNT(*) FROM web_post_images").fetchone()[0]
    assert row == ("青岛老城", "青岛问题\n青岛纯文字正文", 0, "detail_observed", "answer_detail", 8)
    assert images == 0
    assert _discovery_counts(db_path) == before

    selected[0]["record"] = _record("501", "不该覆盖的新正文")
    again = mediacrawler.import_valid_records(
        {"captured_at": "2026-11-25T00:00:00+00:00", "keyword": "青岛旅游", "batch_dir": str(project_root)},
        selected,
        db_path,
        project_root=project_root,
        media_root=media_root,
        require_local_images=True,
        skip_existing=True,
    )
    assert again["inserted_rows"] == 0
    assert again["updated_rows"] == 0
    assert again["already_present_rows"] == 1
    with sqlite3.connect(db_path) as conn:
        text = conn.execute(
            "SELECT content_text FROM web_posts WHERE platform_post_id = '501'"
        ).fetchone()[0]
    assert text == "青岛问题\n青岛纯文字正文"

    overwritten = mediacrawler.import_valid_records(
        {"captured_at": "2026-11-25T00:00:00+00:00", "keyword": "青岛旅游", "batch_dir": str(project_root)},
        selected,
        db_path,
        project_root=project_root,
        media_root=media_root,
        require_local_images=True,
    )
    assert overwritten["updated_rows"] == 1
    with sqlite3.connect(db_path) as conn:
        text = conn.execute(
            "SELECT content_text FROM web_posts WHERE platform_post_id = '501'"
        ).fetchone()[0]
    assert text == "青岛问题\n不该覆盖的新正文"


def _summary_path(tmp_path: Path) -> Path:
    found = list((tmp_path / "out").glob("*/run_summary.json"))
    assert len(found) == 1
    return found[0]


def test_dry_run_writes_admit_targets_without_a_child(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "admit.sqlite"
    _seed(db_path)
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "repair_post_details.py", "--platform", "zhihu", "--db", str(db_path),
            "--keyword", "青岛旅游", "--admit-seen", "--admit-seen-job", "25",
            "--admit-seen-job", "29", "--dry-run", "--headless",
        ],
    )
    assert repair.main() == 0
    summary = json.loads(_summary_path(tmp_path).read_text(encoding="utf-8"))
    assert summary["selection"] == "admit_seen"
    assert summary["plan"]["selection"] == "admit_seen"
    commands = json.loads(Path(summary["plan"]["commands"]).read_text(encoding="utf-8"))
    command = commands[0]["command"]
    assert "--admit-seen" in command
    assert "--no-checkpoint-write" in command
    assert "--discovery-job-id" not in command
    targets = json.loads(Path(commands[0]["targets_path"]).read_text(encoding="utf-8"))
    assert targets[0]["admit"] is True
    assert targets[0]["repair_fallback"] == {}
    assert targets[0]["detail_target"].startswith("https://www.zhihu.com/answer/")


def _child_stdout(path: Path, payload: dict) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return json.dumps({"summary": str(path)})


def test_all_unavailable_round_completes(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "admit.sqlite"
    _seed(db_path)
    before = _discovery_counts(db_path)
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "repair_post_details.py", "--platform", "zhihu", "--db", str(db_path),
            "--keyword", "青岛旅游", "--admit-seen", "--admit-seen-job", "25",
            "--post-id", "207", "--post-id", "208", "--headless",
        ],
    )

    def fake_child(_command, **_kwargs):
        return {
            "returncode": 2,
            "stdout": _child_stdout(tmp_path / "child.json", {
                "import_completion_met": False,
                "formal_validation": {
                    "stop_reason": "repair_no_valid_detail",
                    "valid_total_count": 0,
                    "skipped_candidate_failures": [],
                },
                "pagination_evidence": {
                    "unavailable_candidate_ids": ["207", "208"],
                    "unavailable_candidate_count": 2,
                },
            }),
            "stderr": "",
        }

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    assert repair.main() == 0
    summary = json.loads(_summary_path(tmp_path).read_text(encoding="utf-8"))
    assert summary["status"] == "completed"
    assert not summary["error"]
    assert summary["unavailable_count"] == 2
    assert summary["unavailable_ids_sample"] == ["207", "208"]
    assert summary["candidate_failure_count"] == 0
    assert summary["recovered_count"] == 0
    listed = json.loads(Path(summary["unavailable_ids_path"]).read_text(encoding="utf-8"))
    assert listed == ["207", "208"]
    assert "post_detail_repair_no_progress" not in json.dumps(summary)
    assert _discovery_counts(db_path) == before


def test_admit_failure_without_unavailable_still_has_no_progress(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "admit.sqlite"
    _seed(db_path)
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "repair_post_details.py", "--platform", "zhihu", "--db", str(db_path),
            "--keyword", "青岛旅游", "--admit-seen", "--admit-seen-job", "25",
            "--post-id", "208", "--headless",
        ],
    )

    def fake_child(_command, **_kwargs):
        return {
            "returncode": 2,
            "stdout": _child_stdout(tmp_path / "child.json", {
                "import_completion_met": False,
                "formal_validation": {
                    "stop_reason": "repair_no_valid_detail",
                    "valid_total_count": 0,
                    "skipped_candidate_failures": [{
                        "platform_post_id": "208",
                        "error_code": "repair_target_no_valid_output",
                    }],
                },
                "pagination_evidence": {"skipped_candidate_failures": []},
            }),
            "stderr": "",
        }

    monkeypatch.setattr(repair, "run_repair_child", fake_child)
    assert repair.main() == 2
    summary = json.loads(_summary_path(tmp_path).read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["error"] == "post_detail_repair_no_progress"


def test_keyword_missing_alone_is_not_actionable(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "admit.sqlite"
    _seed(db_path)
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "repair_post_details.py", "--platform", "zhihu", "--db", str(db_path),
            "--keyword", "青岛旅游", "--admit-seen", "--admit-seen-job", "39", "--headless",
        ],
    )
    assert repair.main() == 2
    summary = json.loads(_summary_path(tmp_path).read_text(encoding="utf-8"))
    assert summary["error"] == "no_actionable_repair_targets"
    assert summary["rejected_sample"][0]["reason"] == "admit_keyword_missing"


def test_weibo_selection_ignores_zhihu_seen_candidates(tmp_path: Path) -> None:
    db_path = tmp_path / "admit.sqlite"
    _seed(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO web_posts (
                platform_key, platform_post_id, author_platform_id, source_type, source_url,
                canonical_url, keyword, captured_at, raw_sample_json
            ) VALUES (
                'weibo', '456', '88', 'search', 'https://weibo.com/88/456',
                'https://weibo.com/88/456', '青岛', '2026-01-01T00:00:00+08:00', '{}'
            )
            """
        )
        conn.commit()
    with _open(db_path) as conn:
        targets, _rejected, _pending = repair.select_targets(
            conn, platform="weibo", post_ids=[], max_items=0,
        )
    assert [item["platform_post_id"] for item in targets] == ["456"]
