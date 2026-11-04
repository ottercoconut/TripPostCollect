"""详情修复与作者身份回填共用的备份、信号收束和 child 监督。"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from trippostcollect.core.execution_state import FORMAL_STEPS, FrozenExecutionState
from trippostcollect.records.sanitization import (
    author_avatar_log_marker,
    redact_author_avatar_text,
)
from trippostcollect.runtime.process import (
    EXECUTION_STATE_PATH_ENV,
    PROCESS_CLEANUP_GRACE_SECONDS,
    PROCESS_FINAL_REAP_SECONDS,
    decode_text,
    discard_hung_up_stream,
    kill_recorded_child_process_groups,
    operator_interrupt_error,
    terminate_managed_process,
    write_command_logs,
)


REPAIR_INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
REPAIR_CHILD_POLL_SECONDS = 0.2
REPAIR_CHILD_INTERRUPT_GRACE_SECONDS = (
    PROCESS_CLEANUP_GRACE_SECONDS + PROCESS_FINAL_REAP_SECONDS + 15.0
)
COMPLETED_REPAIR_OUTCOMES = {"completed", "completed_with_remaining"}


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f%z")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tail(value: str, limit: int = 6000) -> str:
    cleaned, _ = redact_author_avatar_text(value or "")
    return cleaned[-limit:] if len(cleaned) > limit else cleaned


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_sqlite_backup(source_path: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError(f"backup destination already exists: {destination}")
    source_uri = f"file:{source_path}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    with sqlite3.connect(destination) as check:
        quick_check = [str(row[0]) for row in check.execute("PRAGMA quick_check")]
        foreign_keys = [tuple(row) for row in check.execute("PRAGMA foreign_key_check")]
    if quick_check != ["ok"] or foreign_keys:
        raise RuntimeError(
            f"SQLite backup validation failed: quick_check={quick_check}, "
            f"foreign_key_errors={len(foreign_keys)}"
        )
    return {
        "path": str(destination),
        "sha256": sha256_file(destination),
        "size_bytes": destination.stat().st_size,
        "quick_check": quick_check,
        "foreign_key_error_count": len(foreign_keys),
    }


def _redacted_child_output(stdout: str, stderr: str) -> tuple[str, str]:
    """与 write_command_logs 相同：任一路含头像词时，两路一起换成同一审计标记。"""

    marker = author_avatar_log_marker({"stdout": stdout, "stderr": stderr})
    if marker is None:
        return stdout, stderr
    return marker, marker


def print_repair_stream(line: str, stream: Any) -> None:
    try:
        print(line, file=stream, flush=True)
    except OSError:
        discard_hung_up_stream(stream)


def _child_text(value: bytes | None) -> str:
    return decode_text(value).replace("\r\n", "\n").replace("\r", "\n")


def interrupt_details(
    signum: int,
    *,
    forced_termination: bool,
    exception: str = "",
) -> dict[str, Any]:
    details = {
        "reason": "operator_interrupt",
        "signum": int(signum),
        "signal": signal.Signals(int(signum)).name,
        "exit_code": 128 + int(signum),
        "error": operator_interrupt_error(signum),
        "forced_termination": bool(forced_termination),
    }
    if exception:
        details["exception"] = exception
    return details


def interrupted_batch_result(batch_plan: dict[str, Any], child_run: dict[str, Any]) -> dict[str, Any]:
    signum = int(child_run["interrupt_signum"])
    stdout = str(child_run.get("stdout") or "")
    stderr = str(child_run.get("stderr") or "")
    logs = child_run.get("logs") or {}
    result = {
        "batch": batch_plan["batch"],
        "target_count": batch_plan["target_count"],
        "launched": bool(child_run.get("launched", True)),
        "exit_code": child_run.get("returncode"),
        "error": operator_interrupt_error(signum),
        "interrupt": interrupt_details(
            signum,
            forced_termination=bool(child_run.get("forced_termination")),
        ),
        "stdout_tail": stdout[-6000:],
        "stderr_tail": stderr[-6000:],
        "killed_child_process_groups": list(child_run.get("killed_groups") or []),
        "logs": {
            key: logs.get(key)
            for key in ("stdout_log", "stderr_log", "command_log")
        },
    }
    log_error = str(child_run.get("log_error") or "")
    if log_error:
        result["log_error"] = log_error
    return result


def terminalize_repair_interrupt(
    state: FrozenExecutionState,
    *,
    error: str,
    evidence: dict[str, Any],
) -> str | None:
    """未完成的执行状态收成中断失败。已经 completed 的状态不改写。"""

    try:
        payload = state.load()
        if payload.get("status") in COMPLETED_REPAIR_OUTCOMES:
            return None
        steps = payload.get("steps") or {}
        open_step: str | None = None
        for name in FORMAL_STEPS:
            if name == "plan_frozen":
                continue
            step_status = (steps.get(name) or {}).get("status")
            if step_status == "failed":
                open_step = None
                break
            if step_status in {"pending", "in_progress"}:
                open_step = name
                break
        if open_step is not None and open_step != "task_finalized":
            state.fail(open_step, error=error, evidence=evidence)
        state.finalize_failure(error=error, evidence=evidence)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _interrupted_child_result(
    command: list[str],
    *,
    log_dir: Path,
    signum: int,
    returncode: int | None,
    stdout: str,
    stderr: str,
    forced_termination: bool,
    launched: bool,
    killed_groups: list[dict[str, Any]],
    write_logs: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    log_error = ""
    write_logs = write_command_logs if write_logs is None else write_logs
    try:
        logs = write_logs(command, log_dir, stdout, stderr)
        stdout_out, stderr_out = logs["stdout"], logs["stderr"]
    except OSError as exc:
        stdout_out, stderr_out = _redacted_child_output(stdout, stderr)
        logs = {}
        log_error = f"{type(exc).__name__}: {exc}"
    return {
        "returncode": returncode,
        "stdout": stdout_out,
        "stderr": stderr_out,
        "interrupt_signum": int(signum),
        "forced_termination": forced_termination,
        "launched": launched,
        "killed_groups": killed_groups,
        "logs": logs,
        "log_error": log_error,
    }


def run_repair_child(
    command: list[str],
    *,
    cwd: Path,
    log_dir: Path,
    state_path: Path,
    interrupt_signal: Callable[[], int | None],
    grace_seconds: float | None = None,
    poll_seconds: float | None = None,
    popen: Callable[..., Any] | None = None,
    terminate_process: Callable[..., tuple[bytes, bytes, bool]] | None = None,
    kill_groups: Callable[[Path], list[dict[str, Any]]] | None = None,
    write_logs: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """运行一批修复 child。没有整轮墙钟；首个信号只向中间层发一次 SIGTERM。

    调用方传入的 ``terminate_process`` / ``popen`` / ``write_logs`` 在调用时解析，
    以便详情修复脚本上的模块级 monkeypatch 继续生效。
    """

    if grace_seconds is None:
        grace_seconds = REPAIR_CHILD_INTERRUPT_GRACE_SECONDS
    if poll_seconds is None:
        poll_seconds = REPAIR_CHILD_POLL_SECONDS
    popen = subprocess.Popen if popen is None else popen
    terminate_process = terminate_managed_process if terminate_process is None else terminate_process
    kill_groups = kill_recorded_child_process_groups if kill_groups is None else kill_groups
    signum = interrupt_signal()
    if signum is not None:
        return _interrupted_child_result(
            command,
            log_dir=log_dir,
            signum=int(signum),
            returncode=None,
            stdout="",
            stderr="",
            forced_termination=False,
            launched=False,
            killed_groups=[],
            write_logs=write_logs,
        )
    env = os.environ.copy()
    env[EXECUTION_STATE_PATH_ENV] = str(state_path)
    process = popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    forced = False
    killed: list[dict[str, Any]] = []
    while True:
        signum = interrupt_signal()
        if signum is not None:
            stdout_bytes, stderr_bytes, forced = terminate_process(
                process,
                grace_seconds=grace_seconds,
            )
            if forced:
                killed = kill_groups(state_path)
            break
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=poll_seconds)
        except subprocess.TimeoutExpired:
            continue
        signum = interrupt_signal()
        if signum is None:
            return {
                "returncode": int(process.returncode or 0),
                "stdout": _child_text(stdout_bytes),
                "stderr": _child_text(stderr_bytes),
            }
        forced = False
        break
    returncode = process.returncode
    return _interrupted_child_result(
        command,
        log_dir=log_dir,
        signum=int(signum),
        returncode=None if returncode is None else int(returncode),
        stdout=_child_text(stdout_bytes),
        stderr=_child_text(stderr_bytes),
        forced_termination=forced,
        launched=True,
        killed_groups=killed,
        write_logs=write_logs,
    )


def _fatal_child_failure(summary: dict[str, Any]) -> str:
    validation = summary.get("formal_validation") or {}
    stop_reason = str(validation.get("stop_reason") or "")
    if stop_reason in {
        "runtime_failed",
        "login_required",
        "captcha_detected",
        "rate_limited",
        "platform_security_limit",
        "policy_blocked",
        "blocked_or_forbidden",
        "runtime_permission_error",
        "browser_launch_failed",
        "browser_target_closed",
    }:
        return stop_reason
    behavior = summary.get("behavior_validation") or {}
    if behavior and not behavior.get("behavior_ok"):
        return "behavior_evidence_failed"
    if behavior and not behavior.get("policy_ok"):
        return "crawl_policy_evidence_failed"
    import_result = summary.get("import_result") or {}
    if import_result.get("reason") == "sqlite_import_failed":
        return "sqlite_import_failed"
    return ""


def child_candidate_failures(summary: dict[str, Any]) -> list[dict[str, Any]]:
    validation = summary.get("formal_validation") or {}
    failures = validation.get("skipped_candidate_failures") or []
    return [dict(item) for item in failures if isinstance(item, dict)]


def repair_persistence_failure_reason(
    *,
    recovered_ids: list[str],
    successful_child_summaries: list[dict[str, Any]],
    persistence_checks: list[dict[str, Any]],
) -> str:
    if not recovered_ids and not successful_child_summaries:
        return "post_detail_repair_no_progress"
    if not (
        recovered_ids
        and successful_child_summaries
        and all(check.get("ok") for check in persistence_checks)
    ):
        return "post_detail_repair_persistence_not_verified"
    return ""


def _strict_batch_blocker(error: str) -> bool:
    if not error:
        return False
    if error.startswith("missing_child_summary_exit_"):
        return False
    return error in {
        "login_required",
        "captcha_detected",
        "rate_limited",
        "platform_security_limit",
        "policy_blocked",
        "blocked_or_forbidden",
        "runtime_permission_error",
        "browser_launch_failed",
        "browser_target_closed",
        "behavior_evidence_failed",
        "crawl_policy_evidence_failed",
        "sqlite_import_failed",
        "post_detail_repair_image_artifacts_incomplete",
        "post_detail_repair_persistence_not_verified",
    }


def _state_fail_open(state: FrozenExecutionState | None, error: str) -> None:
    if state is None:
        return
    try:
        payload = state.load()
        for step in FORMAL_STEPS[1:]:
            status = (payload.get("steps") or {}).get(step, {}).get("status")
            if status in {"pending", "in_progress"}:
                state.fail(step, error=error)
                return
    except Exception:
        return


def _complete_no_op(
    state: FrozenExecutionState,
    *,
    reason: str,
) -> None:
    state.begin("command_executed")
    state.complete("command_executed", evidence={"skipped": True, "reason": reason})
    state.begin("artifacts_verified")
    state.complete("artifacts_verified", evidence={"skipped": True, "reason": reason})
    state.begin("persistence_verified")
    state.complete(
        "persistence_verified",
        evidence={"skipped": True, "reason": reason},
        skipped=True,
    )
    state.finalize(outcome="completed", evidence={"skipped": True, "reason": reason})
