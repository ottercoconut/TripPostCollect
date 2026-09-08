"""A completed page must survive loss of the root before finalization."""

from __future__ import annotations

import json
from importlib import import_module
import signal
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.xhs import batch_checkpoint as batch
from trippostcollect.xhs.discovery import resolve_discovery_plan

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    db = tmp_path / "test.sqlite"
    target = {"target_key": "qingdao_trip", "keyword": "青岛旅行", "top_refresh_max_pages": 5}
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        bootstrap_connection(conn, sync_content=True, sync_jobs=False)
        for account in ("xhs-a01", "xhs-a02"):
            conn.execute("INSERT INTO xhs_accounts(account_id,status) VALUES (?, 'active')", (account,))
        discovery = resolve_discovery_plan(conn, target=target, account_id="xhs-a02")
        lease = {
            "account_id": "xhs-a02",
            "lease_id": "lease",
            "owner_token": "secret",
            "run_id": "run",
            "lease_kind": "crawl",
            "owner_host_id": "host",
            "owner_boot_id": "boot",
            "owner_pid": 42,
            "owner_pgid": 42,
            "owner_process_started_at": "2026-09-08T00:00:00+00:00",
            "owner_process_start_token": "start",
            "execution_state_path": "state",
            "runtime_profile_dir": "profile",
            "acquired_at": "now",
            "heartbeat_at": "now",
            "expires_at": "later",
            "lease_duration_seconds": 300,
            "child_shutdown_budget_seconds": 30,
            "root_finalize_budget_seconds": 270,
        }
        conn.execute(
            f"INSERT INTO xhs_account_leases ({','.join(lease)}) VALUES ({','.join('?' for _ in lease)})",
            tuple(lease.values()),
        )
    data = tmp_path / "outputs" / "run" / "child" / "xhs" / "data"
    contents = data / "xhs" / "jsonl" / "search_contents_test.jsonl"
    contents.parent.mkdir(parents=True)
    contents.write_text('{"note_id":"note1","desc":"青岛旅行"}\n')
    event = {
        "platform": "xhs",
        "batch_no": 1,
        "source_page": 46,
        "resume_page": 47,
        "source_cursor": "search",
        "resume_cursor": "search",
        "source_has_more": True,
        "batch_complete": True,
        "discovery_phase": "frontier",
        "stop_reason": "continue",
        "candidate_count": 1,
        "candidate_identities": ["note1"],
    }
    state_path = tmp_path / "state.json"
    state = {
        "run_id": "run",
        "status": "running",
        "plan": {**target, "account_id": "xhs-a02", "discovery": discovery},
        "events": [{"type": "adaptive_batch_completed", "details": event}],
    }
    state_path.write_text(json.dumps(state))
    monkeypatch.setattr(batch, "XHS_BATCH_CHECKPOINT_ROOT", tmp_path / "checkpoints")
    monkeypatch.setattr(batch, "XHS_RUNS_OUTPUT", tmp_path / "outputs")
    env = {
        batch.ENABLED_ENV: "1",
        batch.DATA_ROOT_ENV: str(data),
        "TRIPPOSTCOLLECT_EXECUTION_STATE_PATH": str(state_path),
        "TRIPPOSTCOLLECT_XHS_RUN_ID": "run",
    }
    guard = SimpleNamespace(db_path=db, run_id="run", lease_id="lease", owner_token="secret", signal_received=None)
    committer = batch.BatchCheckpointCommitter(
        guard=guard, state_path=state_path, target=target, discovery_plan=discovery
    )
    monkeypatch.setattr(batch.time, "sleep", lambda _: committer())
    return SimpleNamespace(**locals())


def saved(s: SimpleNamespace) -> dict:
    with sqlite3.connect(s.db) as conn:
        conn.row_factory = sqlite3.Row
        return resolve_discovery_plan(conn, target=s.target, account_id="xhs-a02")


def test_complete_batch_survives_root_loss_and_mutable_export_tail(scenario: SimpleNamespace) -> None:
    s = scenario
    batch.publish_batch(s.event, s.env)
    # No run_summary, terminal commit, or final state is ever produced.
    s.contents.write_text('{"note_id":"half-written-next-page"')
    plan = saved(s)
    assert plan["resume_page"] == 47
    summary = json.loads(Path(plan["campaign_summary_path"]).read_text())
    snapshot = Path(summary["records"][-1]["output"]["jsonl_files"][0])
    assert json.loads(snapshot.read_text())["note_id"] == "note1"
    assert summary["import_completion_met"] is False
    assert summary["formal_validation"]["source_exhausted_met"] is False
    assert json.loads(s.state_path.read_text())["status"] == "running"
    with sqlite3.connect(s.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 0
        assert conn.execute("SELECT platform_post_id FROM xhs_discovery_seen_candidates").fetchall() == [("note1",)]
        conn.row_factory = sqlite3.Row
        assert not resolve_discovery_plan(conn, target=s.target, account_id="xhs-a01")["checkpoint_found"]


@pytest.mark.parametrize("change", [{"batch_complete": False}, {"source_has_more": False}, {"platform": "weibo"}])
def test_no_commit_for_partial_or_exhaustion_or_other_platform(scenario: SimpleNamespace, change: dict) -> None:
    s = scenario
    batch.publish_batch({**s.event, **change}, s.env)
    assert not saved(s)["checkpoint_found"]


def test_no_import_disables_snapshot(scenario: SimpleNamespace) -> None:
    s = scenario
    batch.publish_batch(s.event, {**s.env, batch.ENABLED_ENV: "0"})
    assert not saved(s)["checkpoint_found"]
    assert not (s.tmp_path / "checkpoints").exists()


def test_exporter_stops_without_ack_and_does_not_write_sqlite(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = scenario
    monkeypatch.setattr(batch, "ACK_TIMEOUT_SECONDS", 0)
    with pytest.raises(RuntimeError, match="ack_timeout"):
        batch.publish_batch(s.event, s.env)
    assert not saved(s)["checkpoint_found"]
    assert (s.tmp_path / "checkpoints/run/batch_checkpoint.json").is_file()


@pytest.mark.parametrize("failure", ["lease", "scope", "artifact", "event", "partial", "signal"])
def test_commit_rejects_unsafe_or_changed_inputs(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    s = scenario
    monkeypatch.setattr(batch, "ACK_TIMEOUT_SECONDS", 0)
    with pytest.raises(RuntimeError, match="ack_timeout"):
        batch.publish_batch(s.event, s.env)
    pointer_path = s.tmp_path / "checkpoints/run/batch_checkpoint.json"
    pointer = json.loads(pointer_path.read_text())
    path = Path(pointer["summary"])
    summary = json.loads(path.read_text())
    if failure == "lease":
        s.guard.owner_token = "wrong"
    elif failure == "scope":
        summary["batch_checkpoint"]["account_id"] = "xhs-a01"
    elif failure == "artifact":
        Path(summary["batch_checkpoint"]["files"][0]["path"]).write_text("corrupted")
    elif failure == "event":
        s.state["events"] = []
        s.state_path.write_text(json.dumps(s.state))
    elif failure == "partial":
        summary["pagination_evidence"]["batches"][0]["batch_complete"] = False
    else:
        s.guard.signal_received = 15
    path.write_text(json.dumps(summary))
    pointer["sha256"] = batch._digest(path)
    pointer_path.write_text(json.dumps(pointer))
    if failure == "signal":
        s.committer()
    else:
        with pytest.raises(RuntimeError):
            s.committer()
    assert not saved(s)["checkpoint_found"]
    assert not (pointer_path.parent / "batch_checkpoint_ack.json").exists()


def test_top_refresh_retains_deep_frontier(scenario: SimpleNamespace) -> None:
    s = scenario
    s.discovery.update(resume_page=47, resume_search_id="deep")
    s.event.update(source_page=1, resume_page=2, discovery_phase="refresh")
    s.state_path.write_text(json.dumps(s.state))
    batch.publish_batch(s.event, s.env)
    assert saved(s)["resume_page"] == 47
    assert saved(s)["resume_search_id"] == "deep"


@pytest.mark.parametrize("directory_exists", [False, True])
def test_empty_refresh_commits_history_without_creating_export_files(
    scenario: SimpleNamespace, directory_exists: bool
) -> None:
    s = scenario
    shutil.rmtree(s.data.parent)
    if directory_exists:
        s.data.mkdir(parents=True)
    old = s.tmp_path / "old.jsonl"
    old.write_text('{"note_id":"old-note"}\n')
    old_record = {"platform": "xhs", "output": {"jsonl_files": [str(old)]}}
    prior = s.tmp_path / "prior.json"
    prior.write_text(json.dumps({"records": [old_record]}))
    s.discovery.update(resume_page=31, resume_search_id="deep", campaign_summary_path=str(prior))
    s.env[batch.RESUME_ENV] = str(prior)
    s.event.update(source_page=1, resume_page=2, discovery_phase="refresh",
                   candidate_count=0, candidate_identities=[], valid_new_count=0, valid_existing_count=0)
    s.state_path.write_text(json.dumps(s.state))

    batch.publish_batch(s.event, s.env)

    plan = saved(s)
    assert plan["resume_page"] == 31
    assert plan["resume_search_id"] == "deep"
    summary = json.loads(Path(plan["campaign_summary_path"]).read_text())
    assert summary["records"][0] == old_record
    assert summary["records"][-1]["output"] == {"jsonl_files": [], "image_manifest_paths": []}
    assert summary["formal_validation"]["candidate_count"] == 1
    batch.verify_snapshot_artifacts(summary)
    assert s.data.exists() is directory_exists
    assert not list(s.data.rglob("*.jsonl"))
    assert (s.tmp_path / "checkpoints/run/batch_checkpoint_ack.json").exists()
    with sqlite3.connect(s.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_posts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM xhs_discovery_seen_candidates").fetchone()[0] == 0


def test_empty_first_batch_then_first_content_uses_next_ack_sequence(scenario: SimpleNamespace) -> None:
    s = scenario
    shutil.rmtree(s.data.parent)
    s.event.update(source_page=1, resume_page=2, candidate_count=0, candidate_identities=[])
    s.state_path.write_text(json.dumps(s.state))
    batch.publish_batch(s.event, s.env)
    assert saved(s)["resume_page"] == 2
    assert not s.data.exists()
    s.contents.parent.mkdir(parents=True)
    s.contents.write_text('{"note_id":"first-note"}\n')
    s.event.update(batch_no=2, source_page=2, resume_page=3,
                   candidate_count=1, candidate_identities=["first-note"], valid_new_count=1)
    s.state_path.write_text(json.dumps(s.state))
    batch.publish_batch(s.event, s.env)
    assert saved(s)["resume_page"] == 3
    assert s.committer.last_sequence == 2
    batch.verify_snapshot_artifacts(json.loads(Path(saved(s)["campaign_summary_path"]).read_text()))


@pytest.mark.parametrize("valid_field", ["valid_new_count", "valid_existing_count"])
def test_missing_export_with_valid_records_does_not_advance(scenario: SimpleNamespace, valid_field: str) -> None:
    s = scenario
    batch.publish_batch(s.event, s.env)
    previous = saved(s)
    # Remove only the mutable original; the acknowledged snapshot remains intact.
    s.contents.unlink()
    s.event.update(batch_no=2, source_page=47, resume_page=48, **{valid_field: 1})
    s.state_path.write_text(json.dumps(s.state))
    with pytest.raises(RuntimeError, match="xhs_batch_checkpoint_content_missing"):
        batch.publish_batch(s.event, s.env)
    assert saved(s) == previous


def test_missing_directory_outside_run_is_not_an_empty_batch(scenario: SimpleNamespace) -> None:
    s = scenario
    s.env[batch.DATA_ROOT_ENV] = str(s.tmp_path / "outside-run/missing")
    with pytest.raises(RuntimeError, match="data_scope_mismatch"):
        batch.publish_batch(s.event, s.env)
    assert not saved(s)["checkpoint_found"]


def test_corrupt_page_does_not_replace_previous_checkpoint(scenario: SimpleNamespace) -> None:
    s = scenario
    batch.publish_batch(s.event, s.env)
    previous = saved(s)
    s.event.update(batch_no=2, source_page=47, resume_page=48)
    s.state_path.write_text(json.dumps(s.state))
    s.contents.write_text('{"note_id":')
    with pytest.raises(ValueError):
        batch.publish_batch(s.event, s.env)
    assert saved(s) == previous


def test_ack_failure_after_db_commit_is_idempotent(scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    s = scenario
    monkeypatch.setattr(batch, "ACK_TIMEOUT_SECONDS", 0)
    with pytest.raises(RuntimeError, match="ack_timeout"):
        batch.publish_batch(s.event, s.env)
    original = batch.atomic_write_json

    def fail_ack(path: Path, value: dict) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(batch, "atomic_write_json", fail_ack)
    with pytest.raises(OSError):
        s.committer()
    assert saved(s)["resume_page"] == 47
    monkeypatch.setattr(batch, "atomic_write_json", original)
    s.committer()
    s.committer()
    with sqlite3.connect(s.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM xhs_discovery_seen_candidates").fetchone()[0] == 1


@pytest.mark.parametrize("commit_before_kill", [True, False])
def test_sigkill_preserves_exactly_the_acknowledged_boundary(
    scenario: SimpleNamespace, commit_before_kill: bool
) -> None:
    s = scenario
    spec = s.tmp_path / "worker.json"
    spec.write_text(
        json.dumps(
            {
                "db": str(s.db),
                "state": str(s.state_path),
                "target": s.target,
                "discovery": s.discovery,
                "env": s.env,
                "event": s.event,
                "root": str(s.tmp_path),
                "commit": commit_before_kill,
            }
        )
    )
    code = """
import json, os, signal, sys
from pathlib import Path
from types import SimpleNamespace
from trippostcollect.xhs import batch_checkpoint as b
s = json.loads(Path(sys.argv[1]).read_text())
b.XHS_BATCH_CHECKPOINT_ROOT = Path(s['root']) / 'checkpoints'
b.XHS_RUNS_OUTPUT = Path(s['root']) / 'outputs'
guard = SimpleNamespace(db_path=s['db'], run_id='run', lease_id='lease', owner_token='secret', signal_received=None)
commit = b.BatchCheckpointCommitter(guard=guard, state_path=Path(s['state']), target=s['target'], discovery_plan=s['discovery'])
def root_tick(_):
    if s['commit']:
        commit()
    os.kill(os.getpid(), signal.SIGKILL)
b.time.sleep = root_tick
b.publish_batch(s['event'], s['env'])
"""
    result = subprocess.run([sys.executable, "-c", code, str(spec)], capture_output=True, timeout=10)
    assert result.returncode == -signal.SIGKILL, result.stderr.decode()
    plan = saved(s)
    assert plan["checkpoint_found"] == commit_before_kill
    if commit_before_kill:
        assert plan["resume_page"] == 47
        assert Path(plan["campaign_summary_path"]).is_file()


def test_snapshot_manifest_stays_usable_and_corruption_blocks_resume(scenario: SimpleNamespace) -> None:
    from dataclasses import asdict
    from hashlib import sha256
    from io import BytesIO
    from PIL import Image
    from trippostcollect.artifacts.image_manifest import ImageManifestEntry

    crawler = import_module("mediacrawler_crawl")

    s = scenario
    record = {"note_id": "note1", "desc": "青岛旅行", "image_list": ["https://sns.test/notes_pre_post/asset"]}
    s.contents.write_text(json.dumps(record) + "\n")
    image_path = s.data / "xhs/images/note1/000.png"
    image_path.parent.mkdir(parents=True)
    output = BytesIO()
    Image.new("RGB", (4, 3)).save(output, format="PNG")
    payload = output.getvalue()
    image_path.write_bytes(payload)
    candidate = crawler.content_image_candidates("xhs", record)[0]
    entry = ImageManifestEntry(
        **asdict(candidate),
        schema_version=1,
        attempts=1,
        http_status=200,
        fetch_status="downloaded",
        staging_path="xhs/images/note1/000.png",
        size_bytes=len(payload),
        mime_type="image/png",
        width=4,
        height=3,
        sha256=sha256(payload).hexdigest(),
        error_code=None,
    )
    manifest = s.data / "xhs/image_manifest.jsonl"
    manifest.write_text(json.dumps(asdict(entry)) + "\n")
    batch.publish_batch(s.event, s.env)
    summary = json.loads(Path(saved(s)["campaign_summary_path"]).read_text())
    batch.verify_snapshot_artifacts(summary)
    # The ongoing exporter replaces its original manifest, not the checkpoint copy.
    manifest.write_text("")
    selected = [
        {
            "platform": "xhs",
            "record": record,
            "identity": "xhs:id:note1",
            "manifest_paths": summary["records"][-1]["output"]["image_manifest_paths"],
        }
    ]
    report, _, _ = crawler.materialize_formal_record_images(
        selected, project_root=s.tmp_path, media_root=s.tmp_path / "media", promote=False
    )
    assert report["complete"] is True
    assert report["validated_images"] == 1
    assert not (s.tmp_path / "media").exists()
    image_path.write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="image_invalid"):
        batch.verify_snapshot_artifacts(summary)


def test_snapshot_keeps_prior_campaign_and_freezes_new_rows(scenario: SimpleNamespace) -> None:
    s = scenario
    old = s.tmp_path / "old_contents_export.jsonl"
    old.write_text('{"note_id":"old-note"}\n')
    prior = s.tmp_path / "prior.json"
    old_record = {"platform": "xhs", "output": {"jsonl_files": [str(old)]}}
    prior.write_text(json.dumps({"records": [old_record]}))
    s.discovery["campaign_summary_path"] = str(prior)
    s.env[batch.RESUME_ENV] = str(prior)
    s.state_path.write_text(json.dumps(s.state))
    batch.publish_batch(s.event, s.env)
    summary = json.loads(Path(saved(s)["campaign_summary_path"]).read_text())
    assert summary["records"][0] == old_record
    assert summary["formal_validation"]["candidate_count"] == 2
    assert "secret" not in json.dumps(summary)
    assert "profile" not in json.dumps(summary)


def test_batch_snapshot_files_do_not_duplicate_live_export_counts(scenario: SimpleNamespace) -> None:
    crawler = import_module("mediacrawler_crawl")
    s = scenario
    batch.publish_batch(s.event, s.env)
    output = crawler.summarize_output(s.data, "青岛旅行")
    assert output["jsonl_files"] == [str(s.contents)]


def test_exporter_hook_publishes_only_after_durable_event(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import ModuleType
    from scripts import mediacrawler_export_entrypoint as exporter

    s = scenario
    s.state["events"] = []
    s.state_path.write_text(json.dumps(s.state))

    def append(event_type: str, details: dict) -> None:
        state = json.loads(s.state_path.read_text())
        state["events"].append({"type": event_type, "details": details})
        s.state_path.write_text(json.dumps(state))

    tools_module = ModuleType("tools")
    tools_module.trippostcollect_adaptive = SimpleNamespace(append_execution_event=append)
    monkeypatch.setitem(sys.modules, "tools", tools_module)
    for key, value in s.env.items():
        monkeypatch.setenv(key, value)
    exporter.install_batch_checkpoint_hook()
    tools_module.trippostcollect_adaptive.append_execution_event("adaptive_batch_completed", s.event)
    assert saved(s)["resume_page"] == 47


@pytest.mark.parametrize("error, detail", [
    (FileNotFoundError("missing export directory"), "xhs_batch_checkpoint_filenotfounderror"),
    (RuntimeError("xhs_batch_checkpoint_ack_timeout"), "xhs_batch_checkpoint_ack_timeout"),
])
def test_exporter_checkpoint_failure_keeps_precise_terminal_evidence(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, error: Exception, detail: str
) -> None:
    from types import ModuleType
    from scripts import mediacrawler_export_entrypoint as exporter

    s = scenario
    events = []
    tools_module = ModuleType("tools")
    tools_module.trippostcollect_adaptive = SimpleNamespace(
        append_execution_event=lambda kind, data: events.append({"type": kind, "details": data})
    )
    monkeypatch.setitem(sys.modules, "tools", tools_module)
    monkeypatch.setenv(batch.ENABLED_ENV, "1")

    def fail(*_args):
        raise error

    monkeypatch.setattr(batch, "publish_batch", fail)
    exporter.install_batch_checkpoint_hook()
    with pytest.raises(RuntimeError, match=detail):
        tools_module.trippostcollect_adaptive.append_execution_event("adaptive_batch_completed", s.event)
    assert [event["type"] for event in events] == ["adaptive_batch_completed", "xhs_runtime_terminal"]
    terminal = events[-1]["details"]
    assert terminal["phase"] == "batch_checkpoint"
    assert terminal["stop_detail"] == detail
    crawler = import_module("mediacrawler_crawl")
    blocker = crawler.runtime_blocker_from_terminal_event(terminal)
    assert blocker["failure_type"] == "runtime_failed"
    assert blocker["reason"] == detail
    assert not saved(s)["checkpoint_found"]
