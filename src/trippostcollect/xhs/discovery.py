"""小红书按目标、账号与查询指纹保存的发现记忆：已处理候选与未完成累计摘要。

每轮都从首页搜索后全量滚到 has_more=false，不保存页码或 search ID；checkpoint 只记最近停止原因、
上次是否完整到达来源末尾（审计）与累计摘要。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from trippostcollect.scheduler.discovery import query_fingerprint
from trippostcollect.xhs.search_target import target_search_fingerprint_params


def xhs_query_fingerprint(target: Mapping[str, Any]) -> str:
    # 目标排序与类型并入指纹：来源查询参数改变须形成新记忆。
    return query_fingerprint(
        "xhs",
        str(target["keyword"]),
        {
            "target_key": str(target["target_key"]),
            **target_search_fingerprint_params(),
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
    visit_ledgers: list[str] = []
    if checkpoint and checkpoint.get("last_summary_path"):
        candidate = Path(str(checkpoint["last_summary_path"])).expanduser()
        if not candidate.is_file():
            raise RuntimeError(
                "XHS checkpoint campaign summary is missing: "
                f"target={target['target_key']} account={account_id} path={candidate}"
            )
        summary_path = str(candidate.resolve())
        visit_ledgers = campaign_visit_ledgers(candidate)
    seen_count = conn.execute(
        """
        SELECT COUNT(*) FROM xhs_discovery_seen_candidates
        WHERE target_key=? AND account_id=? AND query_fingerprint=?
        """,
        (str(target["target_key"]), account_id, fingerprint),
    ).fetchone()[0]
    return {
        "target_key": str(target["target_key"]),
        "account_id": account_id,
        "keyword": str(target["keyword"]),
        "query_fingerprint": fingerprint,
        "checkpoint_found": checkpoint is not None,
        "last_status": str((checkpoint or {}).get("status") or ""),
        "last_stop_reason": str((checkpoint or {}).get("last_stop_reason") or ""),
        "seen_candidate_count": int(seen_count or 0),
        "campaign_summary_path": summary_path,
        "campaign_visit_ledgers": visit_ledgers,
        "campaign_visit_ledger_count": len(visit_ledgers),
        "checkpoint_before": checkpoint,
    }


def campaign_visit_ledgers(summary_path: str | Path) -> list[str]:
    """累计摘要各记录携带的访问账本快照（门禁与冻结输入共用）。"""
    summary = json.loads(Path(summary_path).expanduser().read_text(encoding="utf-8"))
    return [
        str(value)
        for record in summary.get("records") or []
        if isinstance(record, Mapping)
        for value in ((record.get("output") or {}).get("content_visit_paths") or [])
    ]


def save_checkpoint(
    conn: sqlite3.Connection,
    *,
    target_key: str,
    account_id: str,
    keyword: str,
    query_fingerprint_value: str,
    source_has_more: bool | None,
    source_exhausted: bool,
    last_batch_complete: bool,
    last_stop_reason: str,
    last_run_id: str,
) -> None:
    """status 只在停止事件是完整的来源耗尽时写 exhausted，作审计；下一轮照样全量扫描。"""
    conn.execute(
        """
        INSERT INTO xhs_discovery_checkpoints (
            target_key, account_id, keyword, query_fingerprint,
            source_has_more, status, last_batch_complete,
            last_stop_reason, last_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_key, account_id, query_fingerprint) DO UPDATE SET
            keyword=excluded.keyword,
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
            None if source_has_more is None else int(source_has_more),
            "exhausted" if source_exhausted else "active",
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
    commit: bool = True,
) -> dict[str, Any]:
    import_result = child_summary.get("import_result") or {}
    if import_result.get("reason") == "sqlite_import_failed":
        return {"skipped": True, "reason": "sqlite_import_failed"}
    pagination = child_summary.get("pagination_evidence") or {}
    event = pagination.get("stop_event") or ((pagination.get("batches") or [None])[-1])
    if not isinstance(event, Mapping):
        raise RuntimeError("XHS child summary has no durable pagination evidence")

    source_has_more_value = event.get("source_has_more")
    batch_complete = bool(event.get("batch_complete"))
    if source_has_more_value in (False, 0) and not batch_complete:
        source_has_more: bool | None = None
    else:
        source_has_more = None if source_has_more_value is None else bool(source_has_more_value)
    source_exhausted = bool(
        event.get("stop_reason") == "source_exhausted"
        and batch_complete
        and source_has_more is False
    )

    candidate_identities = event.get("candidate_identities") or []
    if not isinstance(candidate_identities, list):
        raise RuntimeError("XHS pagination candidate identities must be a list")
    imported_completion = bool(child_summary.get("import_completion_met")) and not bool(
        import_result.get("reason")
    )
    if imported_completion_verified is not None:
        imported_completion = bool(imported_completion and imported_completion_verified)
    formal_validation = child_summary.get("formal_validation") or {}
    if imported_completion:
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
        source_has_more=source_has_more,
        source_exhausted=source_exhausted,
        last_batch_complete=batch_complete,
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
    if commit:
        conn.commit()
    return {
        "skipped": False,
        "source_has_more": source_has_more,
        "source_exhausted": source_exhausted,
        "last_stop_reason": str(event.get("stop_reason") or "continue"),
        "campaign_summary_path": campaign_summary_path,
        "campaign_candidate_count": campaign_candidate_count,
        "imported_completion": imported_completion,
        "seen_candidate_count": seen_candidate_count,
    }
