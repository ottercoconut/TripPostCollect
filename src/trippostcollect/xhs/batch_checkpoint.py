"""Durable child batch summaries, acknowledged only by the owning root runner.

No browser material is copied. Snapshots keep immutable content/manifest bytes
beside their source files so manifest-relative staging paths remain valid.
The exporter cannot advance SQLite and cannot start another batch until ACK.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from trippostcollect.core.paths import XHS_BATCH_CHECKPOINT_ROOT, XHS_RUNS_OUTPUT
from trippostcollect.artifacts.image_manifest import parse_manifest
from trippostcollect.xhs.accounts import record_event
from trippostcollect.xhs.discovery import commit_child_discovery
from trippostcollect.xhs.terminal import atomic_write_json


ENABLED_ENV = "TRIPPOSTCOLLECT_XHS_BATCH_CHECKPOINT_ENABLED"
DATA_ROOT_ENV = "TRIPPOSTCOLLECT_XHS_BATCH_DATA_ROOT"
RESUME_ENV = "TRIPPOSTCOLLECT_XHS_BATCH_RESUME_SUMMARY"
ACK_TIMEOUT_SECONDS = 60.0


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _write_bytes(path: Path, payload: bytes) -> None:
    # Unique sequence names must never change after a checkpoint references them.
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_snapshot_artifacts(summary: Mapping[str, Any]) -> None:
    files = {item["path"]: item["sha256"] for item in summary["batch_checkpoint"]["files"]}
    referenced = {
        str(value)
        for record in summary["records"]
        for key in ("jsonl_files", "image_manifest_paths")
        for value in record.get("output", {}).get(key, [])
    }
    if referenced != set(files):
        raise RuntimeError("xhs_batch_checkpoint_file_inventory_mismatch")
    for value, expected in files.items():
        if _digest(Path(value)) != expected:
            raise RuntimeError("xhs_batch_checkpoint_artifact_changed")
    for record in summary["records"]:
        output = record.get("output") or {}
        for value in output.get("image_manifest_paths") or []:
            manifest = Path(value).resolve(strict=True)
            for entry in parse_manifest(manifest.read_bytes()):
                if entry.fetch_status != "downloaded":
                    continue  # Failed candidate images remain audit-only.
                relative = Path(str(entry.staging_path))
                root = manifest.parent
                if relative.parts[0] == root.name:
                    root = root.parent
                path = (root / relative).resolve(strict=True)
                if not path.is_relative_to(root) or path.stat().st_size != entry.size_bytes:
                    raise RuntimeError("xhs_batch_checkpoint_image_invalid")
                if _digest(path) != entry.sha256:
                    raise RuntimeError("xhs_batch_checkpoint_image_changed")
                # Retain the image bytes, not just the directory entry, across power loss.
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())


def publish_batch(event: Mapping[str, Any], env: Mapping[str, str] | None = None) -> None:
    """Called after the exporter has awaited all stores in a complete batch."""
    source = os.environ if env is None else env
    if source.get(ENABLED_ENV) != "1" or event.get("platform") != "xhs":
        return
    if event.get("batch_complete") is not True or event.get("source_has_more") is False:
        return
    state_path = Path(source["TRIPPOSTCOLLECT_EXECUTION_STATE_PATH"])
    state = json.loads(state_path.read_text())
    plan = state["plan"]
    if state["status"] != "running" or state["events"][-1]["details"] != event:
        raise RuntimeError("xhs_batch_checkpoint_event_not_durable")
    run_id = state["run_id"]
    if run_id != source["TRIPPOSTCOLLECT_XHS_RUN_ID"]:
        raise RuntimeError("xhs_batch_checkpoint_run_mismatch")
    sequence = int(event["batch_no"])
    # The exporter creates this directory lazily, on its first stored record.
    # A complete refresh page containing only known candidates has no files yet.
    data_root = Path(source[DATA_ROOT_ENV]).resolve()
    if not data_root.is_relative_to((XHS_RUNS_OUTPUT / run_id).resolve()):
        raise RuntimeError("xhs_batch_checkpoint_data_scope_mismatch")
    if data_root.exists() and not data_root.is_dir():
        raise RuntimeError("xhs_batch_checkpoint_data_root_not_directory")
    if str(source.get(RESUME_ENV) or "") != str(plan["discovery"].get("campaign_summary_path") or ""):
        raise RuntimeError("xhs_batch_checkpoint_campaign_mismatch")
    paths = sorted((data_root / "xhs" / "jsonl").glob("*_contents_*.jsonl"))
    if not paths and any(int(event.get(key) or 0) > 0 for key in ("valid_new_count", "valid_existing_count")):
        raise RuntimeError("xhs_batch_checkpoint_content_missing")
    manifest = data_root / "xhs" / "image_manifest.jsonl"
    snapshot_files: list[dict[str, str]] = []
    contents: list[str] = []
    manifests: list[str] = []
    for path in [*paths, *([manifest] if manifest.is_file() else [])]:
        payload = path.read_bytes()
        # Partial JSONL must never turn into a durable discovery decision.
        for line in payload.splitlines():
            if line.strip() and not isinstance(json.loads(line), dict):
                raise RuntimeError("xhs_batch_checkpoint_invalid_record")
        snapshot = path.with_name(f"{path.name}.batch-{sequence:06d}.snapshot")
        _write_bytes(snapshot, payload)
        _sync_directory(snapshot.parent)
        snapshot_files.append({"path": str(snapshot), "sha256": _digest(snapshot)})
        (manifests if path == manifest else contents).append(str(snapshot))

    prior_records: list[dict[str, Any]] = []
    identities = set(event.get("candidate_identities") or [])
    if source.get(RESUME_ENV):
        prior = json.loads(Path(source[RESUME_ENV]).read_text())
        prior_records = prior.get("records") or []
        pagination = prior.get("pagination_evidence") or {}
        previous_event = pagination.get("stop_event") or (pagination.get("batches") or [{}])[-1]
        identities.update(previous_event.get("candidate_identities") or [])
    # Carry prior artifact hashes too: advancing a page must not drop the old campaign.
    for record in prior_records:
        for key in ("jsonl_files", "image_manifest_paths"):
            for value in (record.get("output") or {}).get(key) or []:
                path = Path(value).resolve(strict=True)
                snapshot_files.append({"path": str(path), "sha256": _digest(path)})
    for record in [*prior_records, {"output": {"jsonl_files": contents}}]:
        for value in record["output"].get("jsonl_files") or []:
            for line in Path(value).read_text().splitlines():
                if line.strip():
                    identity = json.loads(line).get("note_id")
                    if identity:
                        identities.add(str(identity))
    summary = {
        "keyword": plan["keyword"],
        "completion_mode": "source-exhausted",
        "import_completion_met": False,
        "import_result": {"skipped": True, "reason": "batch_checkpoint_only"},
        "formal_validation": {
            "candidate_count": len(identities),
            "source_exhausted_met": False,
            "completion_met": False,
        },
        "records": [
            *prior_records,
            {
                "platform": "xhs",
                "status": "incomplete",
                "ok": False,
                "output": {"jsonl_files": contents, "image_manifest_paths": manifests},
            },
        ],
        "pagination_evidence": {"stopped": False, "batches": [dict(event)]},
        "batch_checkpoint": {
            "run_id": run_id,
            "account_id": plan["account_id"],
            "target_key": plan["target_key"],
            "query_fingerprint": plan["discovery"]["query_fingerprint"],
            "sequence": sequence,
            "files": snapshot_files,
        },
    }
    run_dir = XHS_BATCH_CHECKPOINT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / f"batch-{sequence:06d}.summary.json"
    atomic_write_json(summary_path, summary)
    _sync_directory(run_dir)
    pointer = {"summary": str(summary_path), "sha256": _digest(summary_path)}
    pointer_path = run_dir / "batch_checkpoint.json"
    atomic_write_json(pointer_path, pointer)
    _sync_directory(run_dir)
    deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
    ack_path = run_dir / "batch_checkpoint_ack.json"
    while time.monotonic() < deadline:
        if ack_path.is_file() and json.loads(ack_path.read_text()) == pointer:
            return
        time.sleep(0.25)
    raise RuntimeError("xhs_batch_checkpoint_ack_timeout")


class BatchCheckpointCommitter:
    """Root-side checkpoint transaction; the lease token is never serialized."""

    def __init__(
        self, *, guard: Any, state_path: Path, target: Mapping[str, Any], discovery_plan: Mapping[str, Any]
    ) -> None:
        self.guard = guard
        self.state_path = state_path
        self.target = target
        self.discovery_plan = discovery_plan
        self.last_sequence = 0

    def __call__(self) -> None:
        if self.guard.signal_received is not None:
            return
        run_dir = XHS_BATCH_CHECKPOINT_ROOT / self.guard.run_id
        pointer_path = run_dir / "batch_checkpoint.json"
        if not pointer_path.is_file():
            return
        pointer = json.loads(pointer_path.read_text())
        summary_path = Path(pointer["summary"]).resolve(strict=True)
        if summary_path.parent != run_dir.resolve():
            raise RuntimeError("xhs_batch_checkpoint_path_mismatch")
        if _digest(summary_path) != pointer["sha256"]:
            raise RuntimeError("xhs_batch_checkpoint_summary_changed")
        summary = json.loads(summary_path.read_text())
        meta = summary["batch_checkpoint"]
        sequence = int(meta["sequence"])
        if sequence == self.last_sequence:
            return
        if sequence < self.last_sequence:
            raise RuntimeError("xhs_batch_checkpoint_sequence_regressed")
        if any(
            meta[k] != v
            for k, v in {
                "run_id": self.guard.run_id,
                "account_id": self.discovery_plan["account_id"],
                "target_key": self.target["target_key"],
                "query_fingerprint": self.discovery_plan["query_fingerprint"],
            }.items()
        ):
            raise RuntimeError("xhs_batch_checkpoint_scope_mismatch")
        state = json.loads(self.state_path.read_text())
        prior_records = []
        prior_path = self.discovery_plan.get("campaign_summary_path")
        if prior_path:
            prior_records = json.loads(Path(prior_path).read_text()).get("records") or []
        if (
            summary["records"][:-1] != prior_records
            or summary["records"][-1].get("platform") != "xhs"
            or summary.get("import_completion_met") is not False
            or summary.get("keyword") != self.target["keyword"]
        ):
            raise RuntimeError("xhs_batch_checkpoint_campaign_mismatch")
        for values in summary["records"][-1]["output"].values():
            for value in values:
                path = Path(value).resolve(strict=True)
                if not path.is_relative_to((XHS_RUNS_OUTPUT / self.guard.run_id).resolve()) or not path.name.endswith(
                    f".batch-{sequence:06d}.snapshot"
                ):
                    raise RuntimeError("xhs_batch_checkpoint_data_scope_mismatch")
        event = summary["pagination_evidence"]["batches"][-1]
        if state["status"] != "running" or not any(
            e["type"] == "adaptive_batch_completed" and e["details"] == event for e in state["events"]
        ):
            raise RuntimeError("xhs_batch_checkpoint_event_mismatch")
        if (
            event.get("batch_complete") is not True
            or event.get("source_has_more") is False
            or int(event["batch_no"]) != sequence
            or not event.get("resume_cursor")
            or int(event["resume_page"]) != int(event["source_page"]) + 1
        ):
            raise RuntimeError("xhs_batch_checkpoint_unsafe_boundary")
        verify_snapshot_artifacts(summary)
        # The root owns the transaction. The exporter never writes discovery tables.
        with sqlite3.connect(self.guard.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute(
                "SELECT run_id FROM xhs_account_leases WHERE lease_id=? AND owner_token=?",
                (self.guard.lease_id, self.guard.owner_token),
            ).fetchone()
            if lease is None or lease["run_id"] != self.guard.run_id:
                raise RuntimeError("xhs_batch_checkpoint_lease_lost")
            if self.guard.signal_received is not None:
                return
            result = commit_child_discovery(
                conn,
                target=self.target,
                account_id=meta["account_id"],
                run_id=self.guard.run_id,
                discovery_plan=self.discovery_plan,
                child_summary_path=summary_path,
                child_summary=summary,
                imported_completion_verified=False,
                commit=False,
            )
            record_event(
                conn,
                account_id=meta["account_id"],
                run_id=self.guard.run_id,
                event_type="batch_checkpoint_committed",
                details={
                    "sequence": sequence,
                    "summary_sha256": pointer["sha256"],
                    "resume_page": result["resume_page"],
                },
            )
        atomic_write_json(run_dir / "batch_checkpoint_ack.json", pointer)
        _sync_directory(run_dir)
        self.last_sequence = sequence
