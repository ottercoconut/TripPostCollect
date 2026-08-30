from __future__ import annotations

import argparse
from hashlib import sha256
import io
import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path

from PIL import Image

from trippostcollect.artifacts.image_candidates import content_image_candidates
from trippostcollect.artifacts.image_completion import (
    verify_image_artifacts,
    verify_image_persistence,
)
from trippostcollect.artifacts.image_manifest import ImageManifestEntry, write_manifest_atomic
from trippostcollect.artifacts.image_materialization import write_staging_image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

crawl_runner = import_module("crawl_runner")
mediacrawler_crawl = import_module("mediacrawler_crawl")
execution_state = import_module("execution_state")


def png_bytes(color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (7, 5), color=color).save(output, format="PNG")
    return output.getvalue()


def complete_xhs_summary(project_root: Path) -> tuple[dict, Path, Path]:
    record = {
        "note_id": "runner-post-1",
        "title": "title",
        "desc": "body",
        "image_list": "https://sns.test/notes_pre_post/runner-asset",
    }
    candidate = content_image_candidates("xhs", record)[0]
    data_root = project_root / "temp" / "batch" / "xhs" / "data"
    staged = write_staging_image(
        [png_bytes()],
        staging_root=data_root,
        relative_stem="xhs/images/runner-post-1/000",
    )
    manifest_path = data_root / "xhs" / "image_manifest.jsonl"
    write_manifest_atomic(
        manifest_path,
        [
            ImageManifestEntry(
                schema_version=1,
                platform_key=candidate.platform_key,
                platform_post_id=candidate.platform_post_id,
                image_role=candidate.image_role,
                source_index=candidate.source_index,
                source_key=candidate.source_key,
                source_asset_key=candidate.source_asset_key,
                source_url=candidate.source_url,
                fetch_status="downloaded",
                attempts=1,
                http_status=200,
                staging_path=staged.path.relative_to(data_root).as_posix(),
                size_bytes=staged.size_bytes,
                mime_type=staged.mime_type,
                width=staged.width,
                height=staged.height,
                sha256=staged.sha256,
                error_code=None,
            )
        ],
    )
    identity = mediacrawler_crawl.formal_record_identity("xhs", record)
    selected = [
        {
            "platform": "xhs",
            "record": record,
            "identity": identity,
            "source_path": str(data_root / "jsonl" / "contents.jsonl"),
            "line_number": 1,
            "is_new": True,
            "manifest_paths": [str(manifest_path)],
        }
    ]
    media_root = project_root / "temp" / "media"
    image_report, materialized, _ = mediacrawler_crawl.materialize_formal_record_images(
        selected,
        project_root=project_root,
        media_root=media_root,
        promote=True,
    )
    selected[0]["materialized_images"] = materialized[identity]
    db_path = project_root / "temp" / "runner.sqlite"
    summary = {
        "captured_at": "2026-08-07T00:00:00+00:00",
        "keyword": "青岛旅游",
        "batch_dir": str(data_root.parent.parent),
        "image_materialization": image_report,
        "formal_validation": {
            "valid_total_count": 1,
            "new_identities": [identity],
            "existing_identities": [],
        },
    }
    summary["import_result"] = mediacrawler_crawl.import_valid_records(
        summary,
        selected,
        db_path,
        project_root=project_root,
        media_root=media_root,
        require_local_images=True,
    )
    summary["import_completion_met"] = True
    return summary, db_path, media_root


def test_generic_dry_run_freezes_image_contract_for_all_four_platforms(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "runner.sqlite"
    state_root = tmp_path / "states"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "crawl_runner.py",
            "--db",
            str(db_path),
            "--config",
            str(ROOT / "config" / "crawl_targets.json"),
            "--run-root",
            str(tmp_path / "runs"),
            "--execution-state-root",
            str(state_root),
            "--max-jobs",
            "4",
            "--dry-run",
        ],
    )

    assert crawl_runner.main() == 0
    states = [json.loads(path.read_text(encoding="utf-8")) for path in state_root.rglob("*.json")]

    assert len(states) == 4
    assert {item["site_key"] for item in states} == {
        "bilibili",
        "weibo",
        "douyin",
        "zhihu",
    }
    for state in states:
        plan = state["plan"]
        command = plan["command"]
        assert plan["local_image_storage_required"] is True
        assert plan["media_root"] == str(crawl_runner.LOCAL_MEDIA_ROOT.resolve())
        assert plan["discovery"]["local_image_storage_required"] is True
        assert plan["discovery"]["media_root"] == plan["media_root"]
        assert "--download-images" in command
        assert command[command.index("--media-root") + 1] == plan["media_root"]
        assert command[command.index("--behavior-profile") + 1] == "social_high_risk"
        assert "--get-media" not in command
        assert state["steps"]["command_executed"]["status"] == "frozen"


def test_generic_runner_rejects_non_formal_behavior_profile() -> None:
    row = {
        "id": 1,
        "job_key": "invalid_profile_job",
        "site_key": "weibo",
        "target_url": "",
        "job_kind": "mediacrawler_search",
        "behavior_profile_json": json.dumps({"name": "media_crawler_low_frequency"}),
        "params_json": json.dumps(
            {
                "platform": "weibo",
                "required_fields_profile": "image_post_with_followers_v1",
                "followers_policy": "required",
            }
        ),
    }

    try:
        crawl_runner.build_command(row, argparse.Namespace())
    except ValueError as exc:
        assert "must use social_high_risk" in str(exc)
    else:
        raise AssertionError("invalid formal behavior profile was accepted")


def test_runner_rehashes_manifests_and_rejects_tampering(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    summary, _, _ = complete_xhs_summary(project_root)

    verified = verify_image_artifacts(
        summary,
        project_root=project_root,
        expect_promotion=True,
    )
    manifest_path = project_root / summary["image_materialization"]["manifest_paths"][0]
    manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
    rejected = verify_image_artifacts(
        summary,
        project_root=project_root,
        expect_promotion=True,
    )

    assert verified["ok"] is True
    assert verified["verified_manifests"] == 1
    assert rejected["ok"] is False
    assert "SHA-256 mismatch" in rejected["reason"]


def test_runner_accepts_complete_selected_images_with_skipped_candidates(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    summary, _, _ = complete_xhs_summary(project_root)
    summary["image_materialization"].update(
        {
            "retryable_failures": 2,
            "terminal_failures": 3,
            "failures": [],
        }
    )

    verified = verify_image_artifacts(
        summary,
        project_root=project_root,
        expect_promotion=True,
    )

    assert verified["ok"] is True


def test_runner_revalidates_sqlite_relations_and_long_term_bytes(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    summary, db_path, media_root = complete_xhs_summary(project_root)

    verified = verify_image_persistence(
        summary,
        db_path,
        project_root=project_root,
        media_root=media_root,
    )
    with sqlite3.connect(db_path) as conn:
        local_path = conn.execute(
            "SELECT local_path FROM web_post_images WHERE image_role='content'"
        ).fetchone()[0]
    image_path = project_root / local_path
    original_sha256 = sha256(image_path.read_bytes()).hexdigest()
    image_path.unlink()
    rejected = verify_image_persistence(
        summary,
        db_path,
        project_root=project_root,
        media_root=media_root,
    )

    assert verified == {
        "ok": True,
        "checked_posts": 1,
        "checked_images": 1,
        "expected_images": 1,
        "source_images": 1,
        "sha256_duplicate_images": 0,
        "quick_check": "ok",
        "foreign_key_check": "ok",
        "reason": "",
    }
    assert len(original_sha256) == 64
    assert rejected["ok"] is False
    assert "does not exist" in rejected["reason"]


def test_image_gate_failure_keeps_later_frozen_steps(tmp_path: Path) -> None:
    state = execution_state.FrozenExecutionState.create(
        tmp_path / "state.json",
        run_id="runner-contract",
        job_key="job",
        site_key="xhs",
        job_kind="xhs_account_search",
        plan={
            "local_image_storage_required": True,
            "media_root": "/frozen/media/root",
        },
        frozen_inputs=[],
    )
    state.begin("command_executed")
    state.complete("command_executed")
    state.begin("artifacts_verified")
    state.fail(
        "artifacts_verified",
        error="local_image_artifacts_incomplete",
        evidence={"local_images": {"ok": False}},
    )

    payload = state.load()
    assert payload["steps"]["artifacts_verified"]["status"] == "failed"
    assert payload["steps"]["persistence_verified"]["status"] == "frozen"
    assert payload["steps"]["task_finalized"]["status"] == "frozen"
