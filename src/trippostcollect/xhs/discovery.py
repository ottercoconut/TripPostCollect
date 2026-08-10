"""Persistent per-account discovery frontiers for Xiaohongshu targets."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

from trippostcollect.scheduler.discovery import query_fingerprint


def xhs_query_fingerprint(target: Mapping[str, Any]) -> str:
    return query_fingerprint(
        "xhs",
        str(target["keyword"]),
        {
            "target_key": str(target["target_key"]),
        },
    )


def load_checkpoint(
    conn: sqlite3.Connection,
    *,
    target_key: str,
    account_id: str,
    query_fingerprint_value: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM xhs_discovery_checkpoints
        WHERE target_key=? AND account_id=? AND query_fingerprint=?
        """,
        (target_key, account_id, query_fingerprint_value),
    ).fetchone()
    return dict(row) if row is not None else None


def resolve_discovery_plan(
    conn: sqlite3.Connection,
    *,
    target: Mapping[str, Any],
    account_id: str,
) -> dict[str, Any]:
    fingerprint = xhs_query_fingerprint(target)
    checkpoint = load_checkpoint(
        conn,
        target_key=str(target["target_key"]),
        account_id=account_id,
        query_fingerprint_value=fingerprint,
    )
    summary_path = ""
    if checkpoint and checkpoint.get("last_summary_path"):
        candidate = Path(str(checkpoint["last_summary_path"])).expanduser()
        if not candidate.is_file():
            raise RuntimeError(
                "XHS checkpoint campaign summary is missing: "
                f"target={target['target_key']} account={account_id} path={candidate}"
            )
        summary_path = str(candidate.resolve())
    return {
        "target_key": str(target["target_key"]),
        "account_id": account_id,
        "keyword": str(target["keyword"]),
        "query_fingerprint": fingerprint,
        "checkpoint_found": checkpoint is not None,
        "resume_page": max(1, int((checkpoint or {}).get("resume_page") or 1)),
        "resume_search_id": str((checkpoint or {}).get("resume_search_id") or ""),
        "source_exhausted": (checkpoint or {}).get("status") == "exhausted",
        "top_refresh_max_pages": (
            max(0, int(target.get("top_refresh_max_pages") or 0))
            if checkpoint
            else 0
        ),
        "campaign_summary_path": summary_path,
        "checkpoint_before": checkpoint,
    }


def save_checkpoint(
    conn: sqlite3.Connection,
    *,
    target_key: str,
    account_id: str,
    keyword: str,
    query_fingerprint_value: str,
    resume_page: int,
    resume_search_id: str | None,
    source_has_more: bool | None,
    last_batch_complete: bool,
    last_stop_reason: str,
    last_run_id: str,
) -> None:
    if resume_page < 1:
        raise ValueError("resume_page must be positive")
    conn.execute(
        """
        INSERT INTO xhs_discovery_checkpoints (
            target_key, account_id, keyword, query_fingerprint, resume_page,
            resume_search_id, source_has_more, status, last_batch_complete,
            last_stop_reason, last_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_key, account_id, query_fingerprint) DO UPDATE SET
            keyword=excluded.keyword,
            resume_page=excluded.resume_page,
            resume_search_id=excluded.resume_search_id,
            source_has_more=excluded.source_has_more,
            status=excluded.status,
            last_batch_complete=excluded.last_batch_complete,
            last_stop_reason=excluded.last_stop_reason,
            last_run_id=excluded.last_run_id,
            updated_at=datetime('now')
        """,
        (
            target_key,
            account_id,
            keyword,
            query_fingerprint_value,
            resume_page,
            resume_search_id or None,
            None if source_has_more is None else int(source_has_more),
            "exhausted" if source_has_more is False else "active",
            int(last_batch_complete),
            last_stop_reason,
            last_run_id,
        ),
    )


def update_campaign(
    conn: sqlite3.Connection,
    *,
    target_key: str,
    account_id: str,
    query_fingerprint_value: str,
    summary_path: str | None,
    candidate_count: int,
) -> None:
    conn.execute(
        """
        UPDATE xhs_discovery_checkpoints
        SET last_summary_path=?, campaign_candidate_count=?, updated_at=datetime('now')
        WHERE target_key=? AND account_id=? AND query_fingerprint=?
        """,
        (
            summary_path,
            max(0, int(candidate_count)),
            target_key,
            account_id,
            query_fingerprint_value,
        ),
    )


def save_seen_candidates(
    conn: sqlite3.Connection,
    *,
    target_key: str,
    account_id: str,
    query_fingerprint_value: str,
    platform_post_ids: list[str],
    run_id: str,
) -> int:
    identities = sorted({str(value).strip() for value in platform_post_ids if str(value).strip()})
    conn.executemany(
        """
        INSERT INTO xhs_discovery_seen_candidates (
            target_key, account_id, query_fingerprint, platform_post_id,
            first_run_id, last_run_id
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_key, account_id, query_fingerprint, platform_post_id)
        DO UPDATE SET
            last_run_id=excluded.last_run_id,
            last_seen_at=datetime('now')
        """,
        [
            (
                target_key,
                account_id,
                query_fingerprint_value,
                identity,
                run_id,
                run_id,
            )
            for identity in identities
        ],
    )
    return len(identities)


def commit_child_discovery(
    conn: sqlite3.Connection,
    *,
    target: Mapping[str, Any],
    account_id: str,
    run_id: str,
    discovery_plan: Mapping[str, Any],
    child_summary_path: str | Path,
    child_summary: Mapping[str, Any],
    imported_completion_verified: bool | None = None,
) -> dict[str, Any]:
    """Commit a durable frontier only from child pagination evidence."""
    import_result = child_summary.get("import_result") or {}
    if import_result.get("reason") == "sqlite_import_failed":
        return {"skipped": True, "reason": "sqlite_import_failed"}
    pagination = child_summary.get("pagination_evidence") or {}
    event = pagination.get("stop_event") or ((pagination.get("batches") or [None])[-1])
    if not isinstance(event, Mapping):
        raise RuntimeError("XHS child summary has no durable pagination evidence")

    refresh_only = event.get("discovery_phase") == "refresh"
    if refresh_only:
        resume_page = max(1, int(discovery_plan.get("resume_page") or 1))
        resume_search_id = str(discovery_plan.get("resume_search_id") or "") or None
        source_has_more: bool | None = (
            False if discovery_plan.get("source_exhausted") else None
        )
    else:
        resume_page = max(
            1,
            int(
                event.get("resume_page")
                or event.get("source_page")
                or discovery_plan.get("resume_page")
                or 1
            ),
        )
        resume_search_id = str(
            event.get("resume_cursor")
            or event.get("source_cursor")
            or discovery_plan.get("resume_search_id")
            or ""
        ) or None
        source_has_more_value = event.get("source_has_more")
        if source_has_more_value in (False, 0) and not bool(event.get("batch_complete")):
            source_has_more = None
        else:
            source_has_more = (
                None if source_has_more_value is None else bool(source_has_more_value)
            )

    candidate_identities = event.get("candidate_identities") or []
    if not isinstance(candidate_identities, list):
        raise RuntimeError("XHS pagination candidate identities must be a list")
    imported_target = bool(
        child_summary.get("import_completion_met")
        if "import_completion_met" in child_summary
        else child_summary.get("import_new_target_met")
    ) and not bool(import_result.get("reason"))
    if imported_completion_verified is not None:
        imported_target = bool(imported_target and imported_completion_verified)
    formal_validation = child_summary.get("formal_validation") or {}
    if imported_target:
        campaign_summary_path = None
        campaign_candidate_count = 0
    else:
        summary_path = Path(child_summary_path).expanduser().resolve()
        if not summary_path.is_file():
            raise RuntimeError(f"XHS child summary is missing: {summary_path}")
        campaign_summary_path = str(summary_path)
        campaign_candidate_count = int(formal_validation.get("candidate_count") or 0)

    fingerprint = str(discovery_plan["query_fingerprint"])
    save_checkpoint(
        conn,
        target_key=str(target["target_key"]),
        account_id=account_id,
        keyword=str(target["keyword"]),
        query_fingerprint_value=fingerprint,
        resume_page=resume_page,
        resume_search_id=resume_search_id,
        source_has_more=source_has_more,
        last_batch_complete=bool(event.get("batch_complete")),
        last_stop_reason=str(event.get("stop_reason") or "continue"),
        last_run_id=run_id,
    )
    seen_candidate_count = save_seen_candidates(
        conn,
        target_key=str(target["target_key"]),
        account_id=account_id,
        query_fingerprint_value=fingerprint,
        platform_post_ids=candidate_identities,
        run_id=run_id,
    )
    update_campaign(
        conn,
        target_key=str(target["target_key"]),
        account_id=account_id,
        query_fingerprint_value=fingerprint,
        summary_path=campaign_summary_path,
        candidate_count=campaign_candidate_count,
    )
    conn.commit()
    return {
        "skipped": False,
        "refresh_only": refresh_only,
        "resume_page": resume_page,
        "resume_search_id": resume_search_id,
        "source_has_more": source_has_more,
        "last_stop_reason": str(event.get("stop_reason") or "continue"),
        "campaign_summary_path": campaign_summary_path,
        "campaign_candidate_count": campaign_candidate_count,
        "imported_target": imported_target,
        "seen_candidate_count": seen_candidate_count,
    }
