"""把微博、抖音、知乎历史脱敏作者字段回填为平台原始 ID 和昵称。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from trippostcollect.application.failures import extract_stdout_json
from trippostcollect.application.repair_runtime import (
    REPAIR_INTERRUPT_SIGNALS,
    _fatal_child_failure,
    _state_fail_open,
    _strict_batch_blocker,
    create_sqlite_backup,
    interrupt_details,
    print_repair_stream,
    run_repair_child,
    terminalize_repair_interrupt,
    utc_iso,
    utc_stamp,
    write_json,
)
from trippostcollect.core.execution_state import FrozenExecutionState
from trippostcollect.core.paths import (
    AUTHOR_IDENTITY_REPAIR_BACKUP_ROOT,
    AUTHOR_IDENTITY_REPAIR_LEDGER_ROOT,
    AUTHOR_IDENTITY_REPAIR_OUTPUT,
    AUTHOR_IDENTITY_REPAIR_RUNTIME_ROOT,
    DEFAULT_DB,
    FORMAL_CRAWL_CONTRACT,
    PROJECT_ROOT,
    ensure_dir,
)
from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.records.author_observe import (
    CONSECUTIVE_NOT_OBSERVED_STOP,
    OBSERVATIONS_NAME,
)
from trippostcollect.runtime.process import operator_interrupt_error
from trippostcollect.xhs.leases import DeferredTerminationSignals


PLATFORMS = ("douyin", "weibo", "zhihu")
LEGACY_AUTHOR_ID_RE = re.compile(r"^[0-9a-f]{16}$")
IDENTITY_STOP_REASONS = {
    "network_unreachable",
    "no_progress_timeout",
    "observations_missing",
    "observations_unreadable",
    CONSECUTIVE_NOT_OBSERVED_STOP,
}
SQLITE_TIMEOUT_SECONDS = 30.0
UNREACHABLE = "author_unreachable"
MISMATCH = "identity_hash_mismatch"


def legacy_author_digest(user_id: Any) -> str:
    """旧脱敏：sha256(str(id).strip()) 的十六进制前 16 位。只用于回填核验。"""

    if user_id is None:
        return ""
    text = str(user_id).strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def legacy_display_mask(name: Any) -> str:
    """旧昵称脱敏。只用于判断 verified 与 renamed。"""

    if name is None:
        return ""
    text = str(name)
    if len(text) <= 1:
        return "*"
    if len(text) == 2:
        return text[0] + "*"
    return text[0] + "***" + text[-1]


def nickname_is_masked(value: Any) -> bool:
    text = str(value or "")
    if text == "*":
        return True
    if len(text) == 2 and text.endswith("*"):
        return True
    return len(text) == 5 and text[1:4] == "***"


def legacy_author_id(value: Any) -> bool:
    return bool(LEGACY_AUTHOR_ID_RE.fullmatch(str(value or "").strip()))


def row_is_identity_target(platform: str, author_platform_id: Any, display_name: Any) -> bool:
    return (
        platform in PLATFORMS
        and legacy_author_id(author_platform_id)
        and nickname_is_masked(display_name)
    )


def detail_target_for(platform: str, post_id: str, canonical_url: str) -> tuple[str | None, str]:
    parsed = urlparse(str(canonical_url or "").strip().split("#", 1)[0].split("?", 1)[0])
    if platform == "douyin":
        expected_paths = {f"/video/{post_id}", f"/note/{post_id}"}
        if (
            parsed.scheme == "https"
            and parsed.hostname in {"douyin.com", "www.douyin.com"}
            and parsed.path.rstrip("/") in expected_paths
        ):
            return parsed._replace(query="", fragment="").geturl(), ""
        return None, "invalid_douyin_canonical_url"
    if platform == "weibo":
        if (
            parsed.scheme == "https"
            and parsed.hostname == "m.weibo.cn"
            and parsed.path.rstrip("/") == f"/detail/{post_id}"
        ):
            return post_id, ""
        return None, "invalid_weibo_canonical_url"
    answer = bool(
        parsed.scheme == "https"
        and parsed.hostname in {"zhihu.com", "www.zhihu.com"}
        and parsed.path.rstrip("/").endswith(f"/answer/{post_id}")
    )
    article = bool(
        parsed.scheme == "https"
        and parsed.hostname == "zhuanlan.zhihu.com"
        and parsed.path.rstrip("/") == f"/p/{post_id}"
    )
    if answer or article:
        return parsed._replace(query="", fragment="").geturl(), ""
    return None, "invalid_zhihu_canonical_url"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill raw author ids and nicknames for legacy masked Douyin, "
            "Weibo, and Zhihu rows without rewriting post content."
        )
    )
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--keyword", default="青岛旅游")
    parser.add_argument("--batch-size", type=int, default=20, help="Authors per child.")
    parser.add_argument("--max-items", type=int, default=0, help="Maximum target rows; 0 means all.")
    parser.add_argument("--post-id", action="append", dest="post_ids", default=[])
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retry-unreachable", action="store_true")
    parser.add_argument(
        "--confirm-default-db-identity",
        action="store_true",
        help="Required before a non-dry-run write to data/trippostcollect.sqlite.",
    )
    return parser.parse_args(argv)


def ledger_path(platform: str) -> Path:
    return AUTHOR_IDENTITY_REPAIR_LEDGER_ROOT / f"{platform}.json"


def load_ledger(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return {}
    loaded: dict[str, dict[str, Any]] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        author_key = str(item.get("author_key") or "")
        outcome = str(item.get("outcome") or "")
        if author_key and outcome in {UNREACHABLE, MISMATCH}:
            loaded[author_key] = {
                "platform": str(item.get("platform") or ""),
                "author_key": author_key,
                "post_ids": [str(value) for value in item.get("post_ids") or []],
                "outcome": outcome,
            }
    return loaded


class IdentityPayloadError(ValueError):
    """非空作者 JSON 无法解析成对象。整组回滚，不把其余键写成空对象。"""


def store_ledger(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            {"entries": [entries[key] for key in sorted(entries)]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _json_object(value: str | None) -> dict[str, Any]:
    if value is None:
        return {}
    text = str(value)
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IdentityPayloadError(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise IdentityPayloadError("author payload is not an object")
    return parsed


def select_identity_authors(
    conn: sqlite3.Connection,
    *,
    platform: str,
    post_ids: list[str],
    max_items: int,
    ledger: dict[str, dict[str, Any]],
    retry_unreachable: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    rows = conn.execute(
        """
        SELECT id, platform_post_id, canonical_url, author_display_name, author_platform_id
        FROM web_posts
        WHERE platform_key=?
        ORDER BY id
        """,
        (platform,),
    ).fetchall()
    requested = {str(value).strip() for value in post_ids if str(value).strip()}
    grouped: dict[str, dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    pending_rows = 0
    for row in rows:
        post_id = str(row["platform_post_id"] or "").strip()
        author_key = str(row["author_platform_id"] or "").strip()
        display_name = str(row["author_display_name"] or "")
        if not row_is_identity_target(platform, author_key, display_name):
            continue
        pending_rows += 1
        detail_target, reason = detail_target_for(
            platform, post_id, str(row["canonical_url"] or "")
        )
        author = grouped.setdefault(
            author_key,
            {
                "author_key": author_key,
                "masked_name": display_name,
                "posts": [],
                "eligible_post_ids": [],
            },
        )
        author["eligible_post_ids"].append(post_id)
        in_scope = not requested or post_id in requested
        post = {
            "web_post_id": int(row["id"]),
            "platform_post_id": post_id,
            "detail_target": detail_target if in_scope else None,
            "reason": "" if in_scope else "outside_post_id_filter",
            "in_scope": in_scope,
        }
        if in_scope and not detail_target:
            post["reason"] = reason
            rejected.append({**post, "author_key": author_key})
        if in_scope:
            author["posts"].append(post)
    authors = list(grouped.values())
    selected: list[dict[str, Any]] = []
    for author in authors:
        if requested and not author["posts"]:
            continue
        entry = ledger.get(author["author_key"])
        if entry and not (retry_unreachable and entry.get("outcome") == UNREACHABLE):
            continue
        included = [post for post in author["posts"] if post.get("detail_target")]
        scoped_ids = [post["platform_post_id"] for post in author["posts"]]
        author["posts"] = included
        author["scoped_post_ids"] = scoped_ids
        author["covers_author"] = bool(scoped_ids) and len(scoped_ids) == len(
            author["eligible_post_ids"]
        )
        selected.append(author)
    if max_items > 0:
        limited: list[dict[str, Any]] = []
        count = 0
        for author in selected:
            if limited and count >= max_items:
                break
            limited.append(author)
            count += len(author["eligible_post_ids"])
        selected = limited
    return selected, rejected, pending_rows


def chunk_authors(authors: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [authors[index : index + size] for index in range(0, len(authors), size)]


def build_child_command(
    *,
    platform: str,
    keyword: str,
    db_path: Path,
    output_root: Path,
    targets_path: Path,
    headless: bool,
) -> list[str]:
    """启动身份 worker。worker 再套策略门禁和看门狗，并调用现有平台 entry。"""

    del targets_path
    return [
        sys.executable,
        "-m",
        "trippostcollect.application.author_identity_worker",
        "--platform",
        platform,
        "--keyword",
        keyword,
        "--db",
        str(db_path),
        "--save-data-path",
        str(output_root / "data"),
        "--headless",
        "true" if headless else "false",
    ]


def child_targets_payload(authors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "author_key": author["author_key"],
            "posts": [
                {
                    "platform_post_id": post["platform_post_id"],
                    "detail_target": post["detail_target"],
                }
                for post in author["posts"]
            ],
        }
        for author in authors
        if author["posts"]
    ]


def find_observation_file(output_root: Path, summary: dict[str, Any]) -> Path | None:
    hinted = str(summary.get("author_identity_observations") or "")
    if hinted:
        path = Path(hinted)
        if path.is_file():
            return path
    found = [path for path in output_root.rglob(OBSERVATIONS_NAME) if path.is_file()]
    if not found:
        return None
    return max(found, key=lambda path: path.stat().st_mtime)


def load_observation_document(path: Path | None) -> tuple[dict[str, Any] | None, str]:
    if path is None or not path.is_file():
        return None, "observations_missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "observations_unreadable"
    if not isinstance(payload, dict) or not isinstance(payload.get("observations"), list):
        return None, "observations_unreadable"
    return payload, ""


def load_child_summary(stdout: str) -> dict[str, Any]:
    envelope = extract_stdout_json(stdout)
    path_value = str(envelope.get("summary") or "")
    if not path_value:
        return {}
    try:
        payload = json.loads(Path(path_value).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def child_stop_error(summary: dict[str, Any]) -> str:
    error = _fatal_child_failure(summary)
    stop_reason = str((summary.get("formal_validation") or {}).get("stop_reason") or "")
    if not error and stop_reason in IDENTITY_STOP_REASONS:
        return stop_reason
    return error


def blocks_later_batches(error: str) -> bool:
    if not error:
        return False
    if error in IDENTITY_STOP_REASONS or error.startswith("observations_"):
        return True
    if error in {"child_nonzero_exit", "author_identity_write_failed"}:
        return True
    return _strict_batch_blocker(error)


def _nonzero_code(value: Any) -> bool:
    if value in (None, ""):
        return False
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return True


def classify_identity(author_key: str, masked_name: str, fields: dict[str, Any]) -> str:
    new_id = str(fields.get("creator_hash") or "").strip()
    new_name = str(fields.get("user_nickname") or fields.get("nickname") or "")
    if legacy_author_digest(new_id) != author_key:
        return MISMATCH
    if legacy_display_mask(new_name) == masked_name:
        return "verified"
    return "renamed"


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def update_author_group(
    conn: sqlite3.Connection,
    *,
    platform: str,
    author_key: str,
    new_id: str,
    display_name: str,
    profile_url: str,
    source_post_id: str,
) -> tuple[int, int]:
    rows = conn.execute(
        """
        SELECT id, platform_post_id, author_display_name, author_platform_id,
               author_profile_url, author_json, raw_sample_json
        FROM web_posts
        WHERE platform_key=? AND author_platform_id=?
        ORDER BY id
        """,
        (platform, author_key),
    ).fetchall()
    updated = 0
    propagated = 0
    for row in rows:
        if not row_is_identity_target(platform, row["author_platform_id"], row["author_display_name"]):
            continue
        author_json = _json_object(str(row["author_json"] or ""))
        author_json["creator_hash"] = new_id
        author_json["nickname"] = display_name
        raw_sample = _json_object(str(row["raw_sample_json"] or ""))
        raw_sample["creator_hash"] = new_id
        if platform == "zhihu":
            raw_sample["user_nickname"] = display_name
            raw_sample.pop("nickname", None)
        else:
            raw_sample["nickname"] = display_name
        stored_profile = row["author_profile_url"]
        current_profile = "" if stored_profile is None else str(stored_profile)
        next_profile = stored_profile
        if not current_profile.strip() and profile_url:
            next_profile = profile_url
        conn.execute(
            """
            UPDATE web_posts
            SET author_display_name=?,
                author_platform_id=?,
                author_profile_url=?,
                author_json=?,
                raw_sample_json=?,
                updated_at=datetime('now')
            WHERE id=?
            """,
            (
                display_name,
                new_id,
                next_profile,
                _dumps(author_json),
                _dumps(raw_sample),
                int(row["id"]),
            ),
        )
        updated += 1
        if str(row["platform_post_id"] or "") != source_post_id:
            propagated += 1
    return updated, propagated


def commit_author_group(db_path: Path, **kwargs: Any) -> tuple[int, int]:
    conn = sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        updated, propagated = update_author_group(conn, **kwargs)
        conn.commit()
        return updated, propagated
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ledger_entry(platform: str, author: dict[str, Any], outcome: str) -> dict[str, Any]:
    return {
        "platform": platform,
        "author_key": author["author_key"],
        "post_ids": list(author.get("scoped_post_ids") or author.get("eligible_post_ids") or []),
        "outcome": outcome,
    }


def apply_observations(
    *,
    db_path: Path,
    platform: str,
    authors: list[dict[str, Any]],
    document: dict[str, Any],
    ledger: dict[str, dict[str, Any]],
    interrupt_signal: Callable[[], int | None],
) -> dict[str, int]:
    """先写已核验的作者。写库途中新到的信号只结束当前事务，不再开下一组。"""

    by_key = {author["author_key"]: author for author in authors}
    counts = {
        "verified": 0,
        "renamed": 0,
        "repaired_rows": 0,
        "propagated_rows": 0,
        "unreachable": 0,
        "mismatch": 0,
        "write_errors": 0,
    }
    signal_at_start = interrupt_signal()
    for item in document.get("observations") or []:
        if not isinstance(item, dict):
            continue
        if signal_at_start is None and interrupt_signal() is not None:
            break
        author_key = str(item.get("author_key") or "")
        author = by_key.get(author_key)
        if author is None:
            continue
        status = str(item.get("status") or "")
        if status == "not_observed":
            if author.get("covers_author"):
                ledger[author_key] = _ledger_entry(platform, author, UNREACHABLE)
                counts["unreachable"] += 1
            continue
        if status != "observed":
            continue
        bucket = classify_identity(author_key, str(author.get("masked_name") or ""), item)
        display_name = str(item.get("user_nickname") or item.get("nickname") or "")
        new_id = str(item.get("creator_hash") or "").strip()
        if bucket == MISMATCH:
            ledger[author_key] = _ledger_entry(platform, author, MISMATCH)
            counts["mismatch"] += 1
            continue
        try:
            updated, propagated = commit_author_group(
                db_path,
                platform=platform,
                author_key=author_key,
                new_id=new_id,
                display_name=display_name,
                profile_url=str(item.get("author_profile_url") or ""),
                source_post_id=str(item.get("platform_post_id") or ""),
            )
        except (sqlite3.Error, TypeError, ValueError, IdentityPayloadError):
            counts["write_errors"] += 1
            continue
        ledger.pop(author_key, None)
        counts["verified" if bucket == "verified" else "renamed"] += 1
        counts["repaired_rows"] += updated
        counts["propagated_rows"] += propagated
        if signal_at_start is None and interrupt_signal() is not None:
            break
    return counts


def record_local_unreachable(
    authors: list[dict[str, Any]],
    *,
    platform: str,
    ledger: dict[str, dict[str, Any]],
) -> int:
    count = 0
    for author in authors:
        if author.get("posts"):
            continue
        if not author.get("covers_author"):
            continue
        ledger[author["author_key"]] = _ledger_entry(platform, author, UNREACHABLE)
        count += 1
    return count


def count_remaining_rows(conn: sqlite3.Connection, platform: str) -> int:
    rows = conn.execute(
        """
        SELECT author_platform_id, author_display_name
        FROM web_posts
        WHERE platform_key=?
        """,
        (platform,),
    )
    return sum(
        1
        for author_platform_id, display_name in rows
        if row_is_identity_target(platform, author_platform_id, display_name)
    )


def _summary_counts(**counts: int) -> dict[str, int]:
    return {
        "target_row_count": counts.get("target_row_count", 0),
        "author_count": counts.get("author_count", 0),
        "verified_count": counts.get("verified", 0),
        "renamed_count": counts.get("renamed", 0),
        "repaired_row_count": counts.get("repaired_rows", 0),
        "propagated_row_count": counts.get("propagated_rows", 0),
        "author_unreachable_count": counts.get("unreachable", 0),
        "identity_hash_mismatch_count": counts.get("mismatch", 0),
        "remaining_target_row_count": counts.get("remaining", 0),
        "write_error_count": counts.get("write_errors", 0),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.batch_size <= 0 or args.max_items < 0:
        raise SystemExit("--batch-size must be positive; --max-items cannot be negative")
    if not str(args.keyword or "").strip():
        raise SystemExit("--keyword cannot be empty")
    db_path = Path(args.db).expanduser().resolve()
    if (
        not args.dry_run
        and db_path == DEFAULT_DB.resolve()
        and not args.confirm_default_db_identity
    ):
        raise SystemExit("default database identity repair requires --confirm-default-db-identity")
    bootstrap_database(db_path, sync_jobs=False)
    ledger_file = ledger_path(args.platform)
    ledger = load_ledger(ledger_file)
    with sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT_SECONDS) as conn:
        conn.row_factory = sqlite3.Row
        authors, rejected, pending_rows = select_identity_authors(
            conn,
            platform=args.platform,
            post_ids=args.post_ids,
            max_items=args.max_items,
            ledger=ledger,
            retry_unreachable=args.retry_unreachable,
        )
        initial_remaining = count_remaining_rows(conn, args.platform)
    target_rows = sum(len(author.get("scoped_post_ids") or []) for author in authors)
    run_id = f"{args.platform}-{utc_stamp()}"
    run_dir = ensure_dir(AUTHOR_IDENTITY_REPAIR_OUTPUT / run_id)
    runtime_dir = ensure_dir(AUTHOR_IDENTITY_REPAIR_RUNTIME_ROOT / run_id)
    state_path = runtime_dir / "execution_state.json"
    summary_path = run_dir / "run_summary.json"
    backup_path = AUTHOR_IDENTITY_REPAIR_BACKUP_ROOT / run_id / db_path.name
    fetchable = [author for author in authors if author.get("posts")]
    plans: list[dict[str, Any]] = []
    for index, batch in enumerate(chunk_authors(fetchable, args.batch_size), start=1):
        payload = child_targets_payload(batch)
        targets_path = write_json(
            run_dir / "batches" / f"batch-{index:04d}-targets.json",
            payload,
        )
        output_root = run_dir / "children" / f"batch-{index:04d}"
        write_json(output_root / "data" / "author_identity_targets.json", payload)
        plans.append(
            {
                "batch": index,
                "authors": batch,
                "author_count": len(batch),
                "output_root": output_root,
                "command": build_child_command(
                    platform=args.platform,
                    keyword=str(args.keyword),
                    db_path=db_path,
                    output_root=output_root,
                    targets_path=targets_path,
                    headless=args.headless,
                ),
            }
        )
    base_counts = _summary_counts(
        target_row_count=target_rows,
        author_count=len(authors),
        remaining=initial_remaining,
    )
    base_summary = {
        "status": "planned" if args.dry_run else "running",
        "run_id": run_id,
        "job_kind": "author_identity_repair",
        "platform": args.platform,
        "execution_state": str(state_path),
        "started_at": utc_iso(),
        "rejected_count": len(rejected),
        "pending_target_row_count": pending_rows,
        **base_counts,
    }
    state = FrozenExecutionState.create(
        state_path,
        run_id=run_id,
        job_key=f"author_identity_repair:{args.platform}",
        site_key=args.platform,
        job_kind="author_identity_repair",
        plan={
            "run_id": run_id,
            "platform": args.platform,
            "behavior_keyword": args.keyword,
            "db": str(db_path),
            "author_count": len(authors),
            "target_row_count": target_rows,
            "batch_size": args.batch_size,
            "batch_count": len(plans),
            "discovery_writes": False,
        },
        frozen_inputs=[FORMAL_CRAWL_CONTRACT],
        dry_run=args.dry_run,
    )
    if args.dry_run:
        write_json(summary_path, {**base_summary, "status": "planned", "finished_at": utc_iso()})
        print_repair_stream(
            json.dumps({**base_summary, "summary": str(summary_path)}, ensure_ascii=False),
            sys.stdout,
        )
        return 0

    totals = {
        "verified": 0,
        "renamed": 0,
        "repaired_rows": 0,
        "propagated_rows": 0,
        "unreachable": 0,
        "mismatch": 0,
        "write_errors": 0,
    }
    batch_results: list[dict[str, Any]] = []
    fatal_error = ""
    interrupt_signum: int | None = None
    forced_termination = False
    backup: dict[str, Any] = {}
    latch = DeferredTerminationSignals(REPAIR_INTERRUPT_SIGNALS)
    latch.install()
    unexpected: BaseException | None = None
    try:
        if plans:
            backup = create_sqlite_backup(db_path, backup_path)
        totals["unreachable"] += record_local_unreachable(
            authors, platform=args.platform, ledger=ledger
        )
        store_ledger(ledger_file, ledger)
        if latch.signal_received is None:
            state.begin("command_executed")
        for plan in plans:
            if latch.signal_received is not None:
                interrupt_signum = int(latch.signal_received)
                break
            child_run = run_repair_child(
                plan["command"],
                cwd=PROJECT_ROOT,
                log_dir=run_dir / "logs" / f"batch-{int(plan['batch']):04d}",
                state_path=state_path,
                interrupt_signal=lambda: latch.signal_received,
            )
            if child_run.get("interrupt_signum") is not None:
                interrupt_signum = int(child_run["interrupt_signum"])
                forced_termination = bool(child_run.get("forced_termination"))
            summary = load_child_summary(str(child_run.get("stdout") or ""))
            observation_path = find_observation_file(plan["output_root"], summary)
            document, observation_error = load_observation_document(observation_path)
            applied = {
                "verified": 0,
                "renamed": 0,
                "repaired_rows": 0,
                "propagated_rows": 0,
                "unreachable": 0,
                "mismatch": 0,
                "write_errors": 0,
            }
            if document is not None:
                applied = apply_observations(
                    db_path=db_path,
                    platform=args.platform,
                    authors=plan["authors"],
                    document=document,
                    ledger=ledger,
                    interrupt_signal=lambda: latch.signal_received,
                )
                for key, value in applied.items():
                    totals[key] += value
                store_ledger(ledger_file, ledger)
            else:
                fatal_error = fatal_error or observation_error
            child_error = observation_error or child_stop_error(summary)
            if document is not None and document.get("stopped_early"):
                child_error = child_error or str(document.get("stop_reason") or "runtime_failed")
            if child_run.get("interrupt_signum") is not None and not child_error:
                child_error = operator_interrupt_error(int(child_run["interrupt_signum"]))
            platform_returncode = summary.get("platform_returncode")
            nonzero_exit = interrupt_signum is None and (
                _nonzero_code(child_run.get("returncode")) or _nonzero_code(platform_returncode)
            )
            if nonzero_exit:
                child_error = child_error or "child_nonzero_exit"
            classification = summary.get("failure_classification") or {}
            stop_reason = str((summary.get("formal_validation") or {}).get("stop_reason") or "")
            policy_blocked = stop_reason == "policy_blocked" or (
                classification.get("failure_type") == "policy_blocked"
            )
            policy_wait_seconds = int(classification.get("wait_seconds") or 0) if policy_blocked else 0
            batch_results.append(
                {
                    "batch": plan["batch"],
                    "author_count": plan["author_count"],
                    "exit_code": child_run.get("returncode"),
                    "platform_returncode": platform_returncode,
                    "error": child_error,
                    "verified_count": applied["verified"],
                    "renamed_count": applied["renamed"],
                    "repaired_row_count": applied["repaired_rows"],
                    "author_unreachable_count": applied["unreachable"],
                    "identity_hash_mismatch_count": applied["mismatch"],
                    "policy_blocked": policy_blocked,
                    "policy_wait_seconds": policy_wait_seconds,
                    "observations": "" if observation_path is None else str(observation_path),
                }
            )
            if totals["write_errors"] > 0:
                fatal_error = fatal_error or "author_identity_write_failed"
            stopped_early = bool(document and document.get("stopped_early"))
            if interrupt_signum is not None or stopped_early or nonzero_exit or fatal_error or (
                child_error and blocks_later_batches(child_error)
            ):
                fatal_error = fatal_error or child_error
                break
        if interrupt_signum is None and latch.signal_received is not None:
            interrupt_signum = int(latch.signal_received)
        if interrupt_signum is not None:
            if fatal_error == "":
                fatal_error = operator_interrupt_error(interrupt_signum)
            terminalize_repair_interrupt(
                state,
                error=fatal_error,
                evidence={"interrupt": {"signum": interrupt_signum}},
            )
        elif fatal_error:
            state.fail("command_executed", error=fatal_error, evidence={"batches": batch_results})
        else:
            state.complete("command_executed", evidence={"batches": batch_results})
            state.begin("artifacts_verified")
            state.complete(
                "artifacts_verified",
                evidence={"skipped": True, "reason": "author_identity_no_media"},
                skipped=True,
            )
            state.begin("persistence_verified")
            state.complete(
                "persistence_verified",
                evidence={"skipped": True, "reason": "author_identity_direct_update"},
                skipped=True,
            )
            state.finalize(outcome="completed", evidence={"batches": batch_results})
    except Exception as exc:
        if latch.signal_received is not None:
            interrupt_signum = int(latch.signal_received)
            if not fatal_error:
                fatal_error = f"author_identity_exception:{type(exc).__name__}:{exc}"
            _state_fail_open(state, fatal_error)
        elif isinstance(
            exc,
            (OSError, sqlite3.Error, RuntimeError, subprocess.SubprocessError, ValueError),
        ):
            fatal_error = fatal_error or f"author_identity_exception:{type(exc).__name__}:{exc}"
            _state_fail_open(state, fatal_error)
        else:
            fatal_error = fatal_error or f"author_identity_exception:{type(exc).__name__}:{exc}"
            _state_fail_open(state, fatal_error)
            unexpected = exc
    finally:
        latch.restore()
    try:
        with sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT_SECONDS) as conn:
            remaining = count_remaining_rows(conn, args.platform)
    except sqlite3.Error:
        remaining = initial_remaining
    if totals["write_errors"] > 0:
        fatal_error = fatal_error or "author_identity_write_failed"
    outcome = "failed" if fatal_error or interrupt_signum is not None else "completed"
    policy_wait = next(
        (
            int(item.get("policy_wait_seconds") or 0)
            for item in batch_results
            if item.get("policy_blocked")
        ),
        0,
    )
    summary_body = {
        **base_summary,
        "status": outcome,
        "error": fatal_error,
        "backup": backup,
        "batch_results": batch_results,
        "forced_termination": forced_termination,
        "policy_blocked": any(bool(item.get("policy_blocked")) for item in batch_results),
        "policy_wait_seconds": policy_wait,
        "finished_at": utc_iso(),
        **_summary_counts(
            target_row_count=target_rows,
            author_count=len(authors),
            verified=totals["verified"],
            renamed=totals["renamed"],
            repaired_rows=totals["repaired_rows"],
            propagated_rows=totals["propagated_rows"],
            unreachable=totals["unreachable"],
            mismatch=totals["mismatch"],
            remaining=remaining,
            write_errors=totals["write_errors"],
        ),
    }
    if interrupt_signum is not None:
        summary_body["interrupt"] = interrupt_details(
            interrupt_signum,
            forced_termination=forced_termination,
        )
    write_json(summary_path, summary_body)
    print_repair_stream(
        json.dumps({**summary_body, "summary": str(summary_path)}, ensure_ascii=False),
        sys.stdout,
    )
    if unexpected is not None:
        raise unexpected
    if interrupt_signum is not None:
        return 128 + int(interrupt_signum)
    return 0 if not fatal_error else 2
