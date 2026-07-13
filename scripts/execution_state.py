#!/usr/bin/env python3
"""Frozen, atomically updated execution state for formal crawl jobs."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trippostcollect.core.paths import ensure_dir, ensure_parent


SCHEMA_VERSION = 1
FORMAL_STEPS = (
    "plan_frozen",
    "command_executed",
    "artifacts_verified",
    "persistence_verified",
    "task_finalized",
)
UNLOCKING_STATUSES = {"completed", "skipped"}


class ExecutionStateError(RuntimeError):
    pass


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    target = ensure_parent(path)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


class FrozenExecutionState:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    @classmethod
    def create(
        cls,
        path: str | Path,
        *,
        run_id: str,
        job_key: str,
        site_key: str,
        job_kind: str,
        plan: dict[str, Any],
        frozen_inputs: list[Path],
        dry_run: bool = False,
    ) -> "FrozenExecutionState":
        state = cls(path)
        ensure_dir(state.path.parent)
        if state.path.exists():
            raise ExecutionStateError(f"execution state already exists: {state.path}")
        now = utc_iso()
        input_records = []
        for source in frozen_inputs:
            resolved = source.expanduser().resolve()
            if not resolved.is_file():
                raise ExecutionStateError(f"frozen input does not exist: {resolved}")
            input_records.append({"path": str(resolved), "sha256": sha256_file(resolved)})
        steps: dict[str, dict[str, Any]] = {}
        for index, name in enumerate(FORMAL_STEPS):
            steps[name] = {
                "order": index,
                "status": "completed" if name == "plan_frozen" else "frozen",
                "started_at": now if name == "plan_frozen" else None,
                "finished_at": now if name == "plan_frozen" else None,
                "evidence": {"plan_sha256": sha256_text(canonical_json(plan))} if name == "plan_frozen" else {},
                "error": None,
            }
        if not dry_run:
            steps[FORMAL_STEPS[1]]["status"] = "pending"
        payload = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "job_key": job_key,
            "site_key": site_key,
            "job_kind": job_kind,
            "status": "planned" if dry_run else "running",
            "created_at": now,
            "updated_at": now,
            "plan": plan,
            "plan_sha256": sha256_text(canonical_json(plan)),
            "frozen_inputs": input_records,
            "steps": steps,
            "events": [],
        }
        _atomic_write(state.path, payload)
        return state

    def load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExecutionStateError(f"cannot read execution state {self.path}: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise ExecutionStateError(f"unsupported execution state schema: {self.path}")
        return value

    def _validate_frozen(self, payload: dict[str, Any]) -> None:
        expected_plan_hash = str(payload.get("plan_sha256") or "")
        actual_plan_hash = sha256_text(canonical_json(payload.get("plan") or {}))
        if not expected_plan_hash or expected_plan_hash != actual_plan_hash:
            raise ExecutionStateError("execution plan changed after it was frozen")
        for item in payload.get("frozen_inputs") or []:
            path = Path(str(item.get("path") or ""))
            expected = str(item.get("sha256") or "")
            if not path.is_file() or not expected or sha256_file(path) != expected:
                raise ExecutionStateError(f"frozen input changed during execution: {path}")

    def begin(self, step: str) -> dict[str, Any]:
        payload = self.load()
        self._validate_frozen(payload)
        steps = payload.get("steps") or {}
        if step not in steps or step == FORMAL_STEPS[0]:
            raise ExecutionStateError(f"unknown or immutable execution step: {step}")
        index = FORMAL_STEPS.index(step)
        previous = steps[FORMAL_STEPS[index - 1]]
        current = steps[step]
        if previous.get("status") not in UNLOCKING_STATUSES:
            raise ExecutionStateError(f"step {step} is frozen by {FORMAL_STEPS[index - 1]}")
        if current.get("status") != "pending":
            raise ExecutionStateError(f"step {step} is not pending: {current.get('status')}")
        current.update({"status": "in_progress", "started_at": utc_iso(), "error": None})
        payload["updated_at"] = utc_iso()
        _atomic_write(self.path, payload)
        return payload

    def complete(self, step: str, *, evidence: dict[str, Any] | None = None, skipped: bool = False) -> dict[str, Any]:
        payload = self.load()
        self._validate_frozen(payload)
        current = (payload.get("steps") or {}).get(step)
        if not current or current.get("status") != "in_progress":
            raise ExecutionStateError(f"step {step} is not in progress")
        current.update(
            {
                "status": "skipped" if skipped else "completed",
                "finished_at": utc_iso(),
                "evidence": evidence or {},
                "error": None,
            }
        )
        index = FORMAL_STEPS.index(step)
        if index + 1 < len(FORMAL_STEPS):
            next_step = payload["steps"][FORMAL_STEPS[index + 1]]
            if next_step.get("status") == "frozen":
                next_step["status"] = "pending"
        payload["updated_at"] = utc_iso()
        _atomic_write(self.path, payload)
        return payload

    def fail(self, step: str, *, error: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self.load()
        self._validate_frozen(payload)
        current = (payload.get("steps") or {}).get(step)
        if not current or current.get("status") not in {"pending", "in_progress"}:
            raise ExecutionStateError(f"step {step} cannot fail from {current.get('status') if current else 'missing'}")
        current.update(
            {
                "status": "failed",
                "finished_at": utc_iso(),
                "evidence": evidence or {},
                "error": error,
            }
        )
        payload["status"] = "failed"
        payload["updated_at"] = utc_iso()
        _atomic_write(self.path, payload)
        return payload

    def append_event(self, event_type: str, details: dict[str, Any]) -> dict[str, Any]:
        payload = self.load()
        self._validate_frozen(payload)
        events = payload.setdefault("events", [])
        events.append({"at": utc_iso(), "type": event_type, "details": details})
        payload["updated_at"] = utc_iso()
        _atomic_write(self.path, payload)
        return payload

    def finalize(self, *, outcome: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        self.begin("task_finalized")
        payload = self.complete("task_finalized", evidence=evidence or {})
        payload["status"] = outcome
        payload["updated_at"] = utc_iso()
        _atomic_write(self.path, payload)
        return payload
