#!/usr/bin/env python3
"""Run the 100-row Bilibili pilot gate, then promote and continue full repair."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from promote_bilibili_repair_results import (
    PromotionConfig,
    clone_repair_state_for_target,
    run_promotion,
)
from repair_bilibili_articles import (
    RepairConfig,
    assert_external_invariants,
    create_online_backup,
    json_text,
    meta_values,
    repair_state_validation,
    run_continuous_repair,
    scoped_state_counts,
    sqlite_connect,
    state_counts,
    target_summary,
    utc_now,
)
from trippostcollect.core.paths import DEFAULT_DB, ensure_dir, ensure_parent


@dataclass(frozen=True)
class SupervisorConfig:
    pilot_db_path: Path
    pilot_state_path: Path
    source_backup_path: Path
    target_db_path: Path
    expected_target_sha256: str
    pre_full_backup_path: Path
    full_state_path: Path
    pilot_report_dir: Path
    promotion_report_dir: Path
    full_report_dir: Path
    supervisor_report_path: Path
    confirm_default_db_full_repair: bool
    source_limit: int
    max_items: int
    session_size: int
    pacing_min: float
    pacing_max: float
    session_pause_min: float
    session_pause_max: float
    retry_delay_seconds: int


def write_supervisor_report(
    config: SupervisorConfig,
    *,
    stage: str,
    status: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "stage": stage,
        "status": status,
        "pilot_db": str(config.pilot_db_path),
        "pilot_state": str(config.pilot_state_path),
        "source_backup": str(config.source_backup_path),
        "target_db": str(config.target_db_path),
        "pre_full_backup": str(config.pre_full_backup_path),
        "full_state": str(config.full_state_path),
        "details": details,
    }
    ensure_parent(config.supervisor_report_path).write_text(
        json_text(payload, pretty=True) + "\n",
        encoding="utf-8",
    )
    print(json_text(payload, pretty=True), flush=True)
    return payload


def repair_config(
    config: SupervisorConfig,
    *,
    db_path: Path,
    state_path: Path,
    report_dir: Path,
    expected_baseline_sha256: str,
    source_limit: int,
) -> RepairConfig:
    return RepairConfig(
        db_path=db_path,
        state_db_path=state_path,
        report_dir=report_dir,
        backup_path=config.source_backup_path,
        expected_baseline_sha256=expected_baseline_sha256,
        apply=True,
        confirm_default_db_repair=config.confirm_default_db_full_repair,
        max_items=config.max_items,
        session_size=config.session_size,
        pacing_min=config.pacing_min,
        pacing_max=config.pacing_max,
        session_pause_min=config.session_pause_min,
        session_pause_max=config.session_pause_max,
        retry_delay_seconds=config.retry_delay_seconds,
        source_limit=source_limit,
        only_ids=frozenset(),
    )


def validate_pilot_gate(config: SupervisorConfig) -> dict[str, Any]:
    with sqlite_connect(config.pilot_state_path, readonly=True) as connection:
        meta = meta_values(connection)
        scope = scoped_state_counts(
            connection,
            source_limit=config.source_limit,
            only_ids=frozenset(),
        )
        validation = repair_state_validation(
            config.pilot_db_path,
            connection,
            meta,
        )
    terminal = sum(
        int(scope.get(status) or 0)
        for status in (
            "succeeded",
            "permanent_unavailable",
            "invalid_detail",
            "operator_excluded",
        )
    )
    if int(scope.get("total") or 0) != config.source_limit:
        raise RuntimeError("pilot scope count differs from required source limit")
    if int(scope.get("pending") or 0) != 0:
        raise RuntimeError("pilot still has pending rows")
    if int(scope.get("retryable") or 0) != 0:
        raise RuntimeError("pilot still has retryable rows")
    if int(scope.get("conflict") or 0) != 0:
        raise RuntimeError("pilot has optimistic-lock conflicts")
    if terminal != config.source_limit:
        raise RuntimeError("pilot terminal statuses do not account for the complete scope")
    if not validation["ok"]:
        raise RuntimeError(f"pilot row validation failed: {validation}")
    invariants = assert_external_invariants(config.pilot_db_path, meta)
    return {
        "scope": scope,
        "validation": validation,
        "external_invariants": invariants,
        "target": target_summary(config.pilot_db_path),
    }


def validate_full_completion(config: SupervisorConfig) -> dict[str, Any]:
    with sqlite_connect(config.full_state_path, readonly=True) as connection:
        meta = meta_values(connection)
        counts = state_counts(connection)
        validation = repair_state_validation(
            config.target_db_path,
            connection,
            meta,
        )
    if int(counts.get("pending") or 0) != 0:
        raise RuntimeError("full repair still has pending rows")
    if int(counts.get("retryable") or 0) != 0:
        raise RuntimeError("full repair still has retryable rows")
    if not validation["ok"]:
        raise RuntimeError(f"full repair row validation failed: {validation}")
    return {
        "counts": counts,
        "validation": validation,
        "external_invariants": assert_external_invariants(
            config.target_db_path,
            meta,
        ),
        "target": target_summary(config.target_db_path),
    }


def continue_full_repair(config: SupervisorConfig) -> int:
    if not config.pre_full_backup_path.is_file():
        raise RuntimeError("full repair state exists without its pre-full backup")
    with sqlite_connect(config.full_state_path, readonly=True) as connection:
        full_meta = meta_values(connection)
        if full_meta.get("target_db_path") != str(config.target_db_path.resolve()):
            raise RuntimeError("full repair state belongs to a different target")
        preflight_validation = repair_state_validation(
            config.target_db_path,
            connection,
            full_meta,
        )
        full_baseline_sha256 = full_meta["baseline_db_sha256"]
    if not preflight_validation["ok"]:
        raise RuntimeError(
            f"full repair resume validation failed: {preflight_validation}"
        )
    full_config = repair_config(
        config,
        db_path=config.target_db_path,
        state_path=config.full_state_path,
        report_dir=config.full_report_dir,
        expected_baseline_sha256=full_baseline_sha256,
        source_limit=0,
    )
    full_code, full_result = run_continuous_repair(
        full_config,
        stop_when_scope_attempted=False,
    )
    if full_code != 0:
        write_supervisor_report(
            config,
            stage="full_repair",
            status="stopped",
            details={
                "return_code": full_code,
                "preflight_validation": preflight_validation,
                "result": full_result,
            },
        )
        return full_code

    completion = validate_full_completion(config)
    write_supervisor_report(
        config,
        stage="completed",
        status="completed",
        details=completion,
    )
    return 0


def run_supervisor(config: SupervisorConfig) -> int:
    if config.target_db_path.resolve() == DEFAULT_DB.resolve():
        if not config.confirm_default_db_full_repair:
            raise RuntimeError(
                "default database supervisor requires --confirm-default-db-full-repair"
            )
    if config.full_state_path.exists():
        return continue_full_repair(config)
    if config.pre_full_backup_path.exists():
        raise RuntimeError(
            "pre-full backup exists without a resumable full repair state"
        )

    with sqlite_connect(config.pilot_state_path, readonly=True) as connection:
        pilot_meta = meta_values(connection)
        pilot_baseline_sha256 = pilot_meta["baseline_db_sha256"]
    pilot_config = repair_config(
        config,
        db_path=config.pilot_db_path,
        state_path=config.pilot_state_path,
        report_dir=config.pilot_report_dir,
        expected_baseline_sha256=pilot_baseline_sha256,
        source_limit=config.source_limit,
    )
    pilot_code, pilot_result = run_continuous_repair(
        pilot_config,
        stop_when_scope_attempted=False,
    )
    if pilot_code != 0:
        write_supervisor_report(
            config,
            stage="pilot",
            status="stopped",
            details={"return_code": pilot_code, "result": pilot_result},
        )
        return pilot_code

    pilot_gate = validate_pilot_gate(config)
    write_supervisor_report(
        config,
        stage="pilot_gate",
        status="completed",
        details=pilot_gate,
    )

    promotion_config = PromotionConfig(
        target_db_path=config.target_db_path,
        staged_db_path=config.pilot_db_path,
        state_db_path=config.pilot_state_path,
        backup_path=config.source_backup_path,
        report_dir=config.promotion_report_dir,
        expected_target_sha256=config.expected_target_sha256,
        apply=True,
        confirm_default_db_promotion=config.confirm_default_db_full_repair,
    )
    promotion_code, promotion_result = run_promotion(promotion_config)
    if promotion_code != 0:
        write_supervisor_report(
            config,
            stage="promotion",
            status="failed",
            details={"return_code": promotion_code, "result": promotion_result},
        )
        return promotion_code

    pre_full_backup = create_online_backup(
        config.target_db_path,
        config.pre_full_backup_path,
    )
    clone_result = clone_repair_state_for_target(
        source_state_path=config.pilot_state_path,
        destination_state_path=config.full_state_path,
        staged_db_path=config.pilot_db_path,
        target_db_path=config.target_db_path,
    )
    write_supervisor_report(
        config,
        stage="full_ready",
        status="completed",
        details={
            "promotion": promotion_result,
            "pre_full_backup": pre_full_backup,
            "state_clone": clone_result,
        },
    )

    return continue_full_repair(config)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-db", required=True)
    parser.add_argument("--pilot-state", required=True)
    parser.add_argument("--source-backup", required=True)
    parser.add_argument("--target-db", default=str(DEFAULT_DB))
    parser.add_argument("--expected-target-sha256", required=True)
    parser.add_argument("--pre-full-backup", required=True)
    parser.add_argument("--full-state", required=True)
    parser.add_argument("--pilot-report-dir", required=True)
    parser.add_argument("--promotion-report-dir", required=True)
    parser.add_argument("--full-report-dir", required=True)
    parser.add_argument("--supervisor-report", required=True)
    parser.add_argument("--confirm-default-db-full-repair", action="store_true")
    parser.add_argument("--source-limit", type=int, default=100)
    parser.add_argument("--max-items", type=int, default=100)
    parser.add_argument("--session-size", type=int, default=1)
    parser.add_argument("--pacing-min", type=float, default=90.0)
    parser.add_argument("--pacing-max", type=float, default=120.0)
    parser.add_argument("--session-pause-min", type=float, default=90.0)
    parser.add_argument("--session-pause-max", type=float, default=120.0)
    parser.add_argument("--retry-delay-seconds", type=int, default=300)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if min(
        args.source_limit,
        args.max_items,
        args.session_size,
    ) <= 0:
        raise SystemExit("source limit, max items, and session size must be positive")
    if min(
        args.pacing_min,
        args.pacing_max,
        args.session_pause_min,
        args.session_pause_max,
        args.retry_delay_seconds,
    ) < 0:
        raise SystemExit("wait and retry values cannot be negative")
    config = SupervisorConfig(
        pilot_db_path=Path(args.pilot_db).expanduser().resolve(),
        pilot_state_path=Path(args.pilot_state).expanduser().resolve(),
        source_backup_path=Path(args.source_backup).expanduser().resolve(),
        target_db_path=Path(args.target_db).expanduser().resolve(),
        expected_target_sha256=str(args.expected_target_sha256).strip(),
        pre_full_backup_path=Path(args.pre_full_backup).expanduser().resolve(),
        full_state_path=Path(args.full_state).expanduser().resolve(),
        pilot_report_dir=ensure_dir(Path(args.pilot_report_dir).expanduser().resolve()),
        promotion_report_dir=ensure_dir(
            Path(args.promotion_report_dir).expanduser().resolve()
        ),
        full_report_dir=ensure_dir(Path(args.full_report_dir).expanduser().resolve()),
        supervisor_report_path=Path(args.supervisor_report).expanduser().resolve(),
        confirm_default_db_full_repair=bool(args.confirm_default_db_full_repair),
        source_limit=int(args.source_limit),
        max_items=int(args.max_items),
        session_size=int(args.session_size),
        pacing_min=min(args.pacing_min, args.pacing_max),
        pacing_max=max(args.pacing_min, args.pacing_max),
        session_pause_min=min(args.session_pause_min, args.session_pause_max),
        session_pause_max=max(args.session_pause_min, args.session_pause_max),
        retry_delay_seconds=int(args.retry_delay_seconds),
    )
    return run_supervisor(config)


if __name__ == "__main__":
    raise SystemExit(main())
