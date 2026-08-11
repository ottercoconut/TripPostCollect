"""Persistent discovery frontiers for structured crawl jobs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping


NON_SOURCE_PARAM_KEYS = {
    "candidate_hard_limit",
    "followers_policy",
    "headless",
    "login_type",
    "max_stagnant_batches",
    "required_fields_profile",
    "target_new_posts",
    "top_refresh_max_pages",
    "timeout_per_platform",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def source_query_options(params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in params.items()
        if key not in NON_SOURCE_PARAM_KEYS and key not in {"keyword", "platform"}
    }


def query_fingerprint(
    platform_key: str,
    keyword: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    payload = {
        "platform_key": str(platform_key),
        "keyword": str(keyword),
        "source_options": source_query_options(params or {}),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def load_checkpoint(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    query_fingerprint_value: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM crawl_discovery_checkpoints
        WHERE job_id=? AND query_fingerprint=?
        """,
        (job_id, query_fingerprint_value),
    ).fetchone()
    return dict(row) if row is not None else None


def save_checkpoint(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    platform_key: str,
    keyword: str,
    query_fingerprint_value: str,
    resume_page: int,
    resume_offset: int | None,
    resume_cursor: str | None,
    source_has_more: bool | None,
    last_batch_complete: bool,
    last_stop_reason: str,
    last_run_id: str | None,
    last_stop_detail: str = "",
) -> None:
    if resume_page < 1:
        raise ValueError("resume_page must be positive")
    status = "exhausted" if source_has_more is False else "active"
    conn.execute(
        """
        INSERT INTO crawl_discovery_checkpoints (
            job_id, platform_key, keyword, query_fingerprint, resume_page,
            resume_offset, resume_cursor, source_has_more, status,
            last_batch_complete, last_stop_reason, last_stop_detail, last_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, query_fingerprint) DO UPDATE SET
            platform_key=excluded.platform_key,
            keyword=excluded.keyword,
            resume_page=excluded.resume_page,
            resume_offset=excluded.resume_offset,
            resume_cursor=excluded.resume_cursor,
            source_has_more=excluded.source_has_more,
            status=excluded.status,
            last_batch_complete=excluded.last_batch_complete,
            last_stop_reason=excluded.last_stop_reason,
            last_stop_detail=excluded.last_stop_detail,
            last_run_id=excluded.last_run_id,
            updated_at=datetime('now')
        """,
        (
            job_id,
            platform_key,
            keyword,
            query_fingerprint_value,
            resume_page,
            resume_offset,
            resume_cursor or None,
            None if source_has_more is None else int(source_has_more),
            status,
            int(last_batch_complete),
            last_stop_reason,
            last_stop_detail,
            last_run_id,
        ),
    )


def save_seen_candidates(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    platform_key: str,
    query_fingerprint_value: str,
    platform_post_ids: list[str],
    run_id: str,
) -> int:
    identities = sorted(
        {
            str(value).strip()
            for value in platform_post_ids
            if str(value).strip()
        }
    )
    conn.executemany(
        """
        INSERT INTO crawl_discovery_seen_candidates (
            job_id, platform_key, query_fingerprint, platform_post_id,
            first_run_id, last_run_id
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, query_fingerprint, platform_post_id)
        DO UPDATE SET
            platform_key=excluded.platform_key,
            last_run_id=excluded.last_run_id,
            last_seen_at=datetime('now')
        """,
        [
            (
                job_id,
                platform_key,
                query_fingerprint_value,
                identity,
                run_id,
                run_id,
            )
            for identity in identities
        ],
    )
    return len(identities)


def load_seen_candidates(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    platform_key: str,
    query_fingerprint_value: str,
) -> set[str]:
    rows = conn.execute(
        """
        SELECT platform_post_id
        FROM crawl_discovery_seen_candidates
        WHERE job_id=? AND platform_key=? AND query_fingerprint=?
        """,
        (job_id, platform_key, query_fingerprint_value),
    ).fetchall()
    return {
        str(row[0]).strip()
        for row in rows
        if row[0] is not None and str(row[0]).strip()
    }


def save_candidate_exclusion(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    platform_key: str,
    query_fingerprint_value: str,
    platform_post_id: str,
    reason: str,
    authorized_run_id: str,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    identity = str(platform_post_id).strip()
    platform = str(platform_key).strip()
    fingerprint = str(query_fingerprint_value).strip()
    reason_value = str(reason).strip()
    run_id = str(authorized_run_id).strip()
    if not identity:
        raise ValueError("platform_post_id must not be empty")
    if not platform:
        raise ValueError("candidate exclusion platform_key must not be empty")
    if not fingerprint:
        raise ValueError("candidate exclusion query_fingerprint must not be empty")
    if not reason_value:
        raise ValueError("candidate exclusion reason must not be empty")
    if not run_id:
        raise ValueError("candidate exclusion authorized_run_id must not be empty")
    conn.execute(
        """
        INSERT INTO crawl_discovery_candidate_exclusions (
            job_id, platform_key, query_fingerprint, platform_post_id,
            reason, evidence_json, authorized_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, query_fingerprint, platform_post_id)
        DO UPDATE SET
            platform_key=excluded.platform_key,
            reason=excluded.reason,
            evidence_json=excluded.evidence_json,
            authorized_run_id=excluded.authorized_run_id,
            updated_at=datetime('now')
        """,
        (
            int(job_id),
            platform,
            fingerprint,
            identity,
            reason_value,
            canonical_json(dict(evidence or {})),
            run_id,
        ),
    )


def load_candidate_exclusions(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    platform_key: str,
    query_fingerprint_value: str,
) -> set[str]:
    rows = conn.execute(
        """
        SELECT platform_post_id
        FROM crawl_discovery_candidate_exclusions
        WHERE job_id=? AND platform_key=? AND query_fingerprint=?
        """,
        (job_id, platform_key, query_fingerprint_value),
    ).fetchall()
    return {
        str(row[0]).strip()
        for row in rows
        if row[0] is not None and str(row[0]).strip()
    }


def load_skipped_candidates(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    platform_key: str,
    query_fingerprint_value: str,
) -> set[str]:
    return load_seen_candidates(
        conn,
        job_id=job_id,
        platform_key=platform_key,
        query_fingerprint_value=query_fingerprint_value,
    ) | load_candidate_exclusions(
        conn,
        job_id=job_id,
        platform_key=platform_key,
        query_fingerprint_value=query_fingerprint_value,
    )


def update_campaign(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    query_fingerprint_value: str,
    summary_path: str | None,
    campaign_candidate_count: int,
) -> None:
    conn.execute(
        """
        UPDATE crawl_discovery_checkpoints
        SET last_summary_path=?, campaign_candidate_count=?, updated_at=datetime('now')
        WHERE job_id=? AND query_fingerprint=?
        """,
        (
            summary_path,
            max(0, int(campaign_candidate_count)),
            job_id,
            query_fingerprint_value,
        ),
    )


def clear_campaign(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    query_fingerprint_value: str,
) -> None:
    update_campaign(
        conn,
        job_id=job_id,
        query_fingerprint_value=query_fingerprint_value,
        summary_path=None,
        campaign_candidate_count=0,
    )
