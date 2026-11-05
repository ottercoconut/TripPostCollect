"""删除台账标记为 author_unreachable、且作者字段仍是旧脱敏形态的帖子。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trippostcollect.application.author_identity import (
    MISMATCH,
    PLATFORMS,
    UNREACHABLE,
    load_ledger,
    row_is_identity_target,
)
from trippostcollect.application.repair_runtime import (
    REPAIR_INTERRUPT_SIGNALS,
    create_sqlite_backup,
    interrupt_details,
    print_repair_stream,
    utc_iso,
    utc_stamp,
    write_json,
)
from trippostcollect.core.paths import (
    AUTHOR_IDENTITY_PURGE_BACKUP_ROOT,
    AUTHOR_IDENTITY_REPAIR_LEDGER_ROOT,
    AUTHOR_IDENTITY_REPAIR_OUTPUT,
    DEFAULT_DB,
    LOCAL_MEDIA_ROOT,
    PROJECT_ROOT,
)
from trippostcollect.runtime.process import operator_interrupt_error
from trippostcollect.xhs.leases import DeferredTerminationSignals


SQLITE_TIMEOUT_SECONDS = 30.0
SAMPLE_POST_IDS = 5


@dataclass(frozen=True)
class PurgeTarget:
    web_post_id: int
    platform_post_id: str


@dataclass(frozen=True)
class Selection:
    targets: tuple[PurgeTarget, ...]
    not_in_ledger: int
    mismatch: int


@dataclass(frozen=True)
class WriteResult:
    committed: bool
    rolled_back: bool
    post_ids: tuple[str, ...]
    media_paths: tuple[str, ...]
    seen: int
    waivers: int
    images: int
    posts: int


_NO_WRITE = WriteResult(False, False, (), (), 0, 0, 0, 0)
_ROLLED_BACK = WriteResult(False, True, (), (), 0, 0, 0, 0)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Delete still-masked Douyin, Weibo, and Zhihu posts whose author hash "
            "is recorded as author_unreachable."
        )
    )
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--confirm-default-db-purge",
        action="store_true",
        help="Required before a non-dry-run purge of data/trippostcollect.sqlite.",
    )
    return parser.parse_args(argv)


def ledger_file(platform: str) -> Path:
    return AUTHOR_IDENTITY_REPAIR_LEDGER_ROOT / f"{platform}.json"


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def select_purge_targets(db_path: Path, platform: str) -> Selection:
    ledger = load_ledger(ledger_file(platform))
    targets: list[PurgeTarget] = []
    not_in_ledger = 0
    mismatch = 0
    with _connect_readonly(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, platform_post_id, author_platform_id, author_display_name
            FROM web_posts
            WHERE platform_key=?
            ORDER BY id
            """,
            (platform,),
        )
        for row in rows:
            author_id = row["author_platform_id"]
            display_name = row["author_display_name"]
            if not row_is_identity_target(platform, author_id, display_name):
                continue
            author_key = str(author_id or "").strip()
            entry = ledger.get(author_key)
            if entry is None:
                not_in_ledger += 1
                continue
            recorded_platform = str(entry.get("platform") or "")
            if recorded_platform not in {"", platform}:
                continue
            outcome = str(entry.get("outcome") or "")
            if outcome == UNREACHABLE:
                targets.append(
                    PurgeTarget(
                        web_post_id=int(row["id"]),
                        platform_post_id=str(row["platform_post_id"] or ""),
                    )
                )
            elif outcome == MISMATCH:
                mismatch += 1
    return Selection(tuple(targets), not_in_ledger, mismatch)


def _dry_run_payload(platform: str, selection: Selection) -> dict[str, Any]:
    return {
        "status": "dry_run",
        "platform": platform,
        "target_count": len(selection.targets),
        "not_in_ledger": selection.not_in_ledger,
        "identity_hash_mismatch": selection.mismatch,
        "sample_post_ids": [item.platform_post_id for item in selection.targets[:SAMPLE_POST_IDS]],
    }


def _delete_rows(
    conn: sqlite3.Connection,
    *,
    platform: str,
    targets: Sequence[PurgeTarget],
) -> WriteResult:
    post_ids: list[str] = []
    media_paths: list[str] = []
    seen = 0
    waivers = 0
    images = 0
    posts = 0
    for target in targets:
        image_rows = conn.execute(
            """
            SELECT local_path
            FROM web_post_images
            WHERE web_post_id=?
            """,
            (target.web_post_id,),
        )
        for (local_path,) in image_rows:
            text = str(local_path or "").strip()
            if text:
                media_paths.append(text)
        seen += conn.execute(
            """
            DELETE FROM crawl_discovery_seen_candidates
            WHERE platform_key=? AND platform_post_id=?
            """,
            (platform, target.platform_post_id),
        ).rowcount
        waivers += conn.execute(
            "DELETE FROM post_detail_repair_waivers WHERE web_post_id=?",
            (target.web_post_id,),
        ).rowcount
        images += conn.execute(
            "DELETE FROM web_post_images WHERE web_post_id=?",
            (target.web_post_id,),
        ).rowcount
        posts += conn.execute(
            "DELETE FROM web_posts WHERE id=? AND platform_key=?",
            (target.web_post_id, platform),
        ).rowcount
        post_ids.append(target.platform_post_id)
    if posts != len(targets):
        raise RuntimeError("purge deleted an unexpected number of posts")
    return WriteResult(
        True,
        False,
        tuple(post_ids),
        tuple(dict.fromkeys(media_paths)),
        seen,
        waivers,
        images,
        posts,
    )


def commit_purge(
    db_path: Path,
    *,
    platform: str,
    targets: Sequence[PurgeTarget],
    interrupt_signal: Callable[[], int | None],
) -> WriteResult:
    """单个事务删除目标行。提交前若已有信号，回滚并保持媒体文件不动。"""

    if interrupt_signal() is not None:
        return _ROLLED_BACK
    if not targets:
        return _NO_WRITE
    conn = sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT_SECONDS)
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = _delete_rows(conn, platform=platform, targets=targets)
            if interrupt_signal() is not None:
                conn.execute("ROLLBACK")
                return _ROLLED_BACK
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return result


def _candidate_path(local_path: str) -> Path:
    raw = Path(local_path).expanduser()
    if raw.is_absolute():
        return raw
    return PROJECT_ROOT / raw


def _resolve_without_requiring(path: Path) -> Path | None:
    try:
        return path.resolve(strict=False)
    except OSError:
        return None


def _referenced_files(db_path: Path) -> set[Path]:
    referenced: set[Path] = set()
    with _connect_readonly(db_path) as conn:
        rows = conn.execute(
            """
            SELECT local_path
            FROM web_post_images
            WHERE local_path IS NOT NULL AND trim(local_path) != ''
            """
        )
        for (local_path,) in rows:
            candidate = _candidate_path(str(local_path).strip())
            referenced.add(candidate)
            resolved = _resolve_without_requiring(candidate)
            if resolved is not None:
                referenced.add(resolved)
    return referenced


def _inside_media_root(path: Path, media_root: Path) -> bool:
    if path == media_root:
        return False
    try:
        path.relative_to(media_root)
    except ValueError:
        return False
    return True


def _remove_empty_post_directories(start: Path, media_root: Path) -> int:
    removed = 0
    current = start
    while True:
        if current.is_symlink() or not current.is_dir():
            break
        resolved = _resolve_without_requiring(current)
        if resolved is None or not _inside_media_root(resolved, media_root):
            break
        if resolved.parent == media_root:
            break
        try:
            current.rmdir()
        except OSError:
            break
        removed += 1
        current = current.parent
    return removed


def remove_unreferenced_media(db_path: Path, local_paths: Sequence[str]) -> dict[str, int]:
    media_root = Path(LOCAL_MEDIA_ROOT).expanduser().resolve()
    referenced = _referenced_files(db_path)
    counts = {
        "media_deleted": 0,
        "media_missing": 0,
        "media_failed": 0,
        "media_skipped_symlink": 0,
        "media_skipped_outside": 0,
        "media_skipped_shared": 0,
        "directories_removed": 0,
    }
    deleted_files: list[Path] = []
    for local_path in dict.fromkeys(local_paths):
        candidate = _candidate_path(local_path)
        if candidate.is_symlink():
            counts["media_skipped_symlink"] += 1
            continue
        resolved = _resolve_without_requiring(candidate)
        if resolved is None:
            counts["media_failed"] += 1
            continue
        if not _inside_media_root(resolved, media_root):
            counts["media_skipped_outside"] += 1
            continue
        if candidate in referenced or resolved in referenced:
            counts["media_skipped_shared"] += 1
            continue
        if not candidate.exists():
            counts["media_missing"] += 1
            continue
        if not candidate.is_file():
            counts["media_failed"] += 1
            continue
        try:
            candidate.unlink()
        except OSError:
            counts["media_failed"] += 1
            continue
        counts["media_deleted"] += 1
        deleted_files.append(candidate)
    for parent in dict.fromkeys(path.parent for path in deleted_files):
        counts["directories_removed"] += _remove_empty_post_directories(parent, media_root)
    return counts


def vacuum_and_check(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT_SECONDS)
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        quick_check = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
        foreign_key_check = [list(row) for row in conn.execute("PRAGMA foreign_key_check")]
    finally:
        conn.close()
    return {
        "vacuum": True,
        "quick_check": quick_check,
        "foreign_key_check": foreign_key_check,
    }


def _base_summary(platform: str, stamp: str, db_path: Path, selection: Selection) -> dict[str, Any]:
    return {
        "status": "running",
        "platform": platform,
        "run_id": f"purge-{platform}-{stamp}",
        "db": str(db_path),
        "started_at": utc_iso(),
        "target_count": len(selection.targets),
        "not_in_ledger": selection.not_in_ledger,
        "identity_hash_mismatch": selection.mismatch,
        "post_ids": [],
        "deleted_posts": 0,
        "deleted_seen": 0,
        "deleted_waivers": 0,
        "deleted_images": 0,
        "media_deleted": 0,
        "media_missing": 0,
        "media_failed": 0,
        "media_skipped_symlink": 0,
        "media_skipped_outside": 0,
        "media_skipped_shared": 0,
        "directories_removed": 0,
        "backup": {},
        "quick_check": [],
        "foreign_key_check": [],
        "vacuum": False,
        "error": "",
    }


def _finalize_summary(summary: dict[str, Any], interrupt_signum: int | None) -> None:
    summary["finished_at"] = utc_iso()
    if summary["media_missing"] or summary["media_failed"]:
        summary["status"] = "failed"
        if not summary["error"]:
            summary["error"] = "media_cleanup_incomplete"
    if summary["vacuum"] and (summary["quick_check"] != ["ok"] or summary["foreign_key_check"]):
        summary["status"] = "failed"
        if not summary["error"]:
            summary["error"] = "sqlite_check_failed"
    if summary["error"] and summary["status"] != "failed":
        summary["status"] = "failed"
    if interrupt_signum is not None:
        summary["status"] = "failed"
        summary["error"] = operator_interrupt_error(interrupt_signum)
        summary["interrupt"] = interrupt_details(interrupt_signum, forced_termination=False)
    elif summary["status"] == "running":
        if summary["vacuum"] and summary["quick_check"] == ["ok"] and not summary["foreign_key_check"]:
            summary["status"] = "completed"
        else:
            summary["status"] = "failed"
            if not summary["error"]:
                summary["error"] = "sqlite_check_failed"


def _apply_write_result(summary: dict[str, Any], result: WriteResult) -> None:
    summary["post_ids"] = list(result.post_ids)
    summary["deleted_posts"] = result.posts
    summary["deleted_seen"] = result.seen
    summary["deleted_waivers"] = result.waivers
    summary["deleted_images"] = result.images


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = Path(args.db).expanduser().resolve()
    if (
        not args.dry_run
        and db_path == Path(DEFAULT_DB).expanduser().resolve()
        and not args.confirm_default_db_purge
    ):
        raise SystemExit("default database purge requires --confirm-default-db-purge")
    if not db_path.is_file():
        raise SystemExit(f"database does not exist: {db_path}")
    selection = select_purge_targets(db_path, args.platform)
    if args.dry_run:
        print_repair_stream(
            json.dumps(_dry_run_payload(args.platform, selection), ensure_ascii=False),
            sys.stdout,
        )
        return 0

    stamp = utc_stamp()
    summary_path = AUTHOR_IDENTITY_REPAIR_OUTPUT / f"purge-{args.platform}-{stamp}" / "run_summary.json"
    backup_path = AUTHOR_IDENTITY_PURGE_BACKUP_ROOT / f"purge-{args.platform}-{stamp}" / db_path.name
    summary = _base_summary(args.platform, stamp, db_path, selection)
    latch = DeferredTerminationSignals(REPAIR_INTERRUPT_SIGNALS)
    latch.install()
    interrupt_signum: int | None = None
    try:
        if latch.signal_received is not None:
            interrupt_signum = int(latch.signal_received)
        else:
            summary["backup"] = create_sqlite_backup(db_path, backup_path)
            if latch.signal_received is not None:
                interrupt_signum = int(latch.signal_received)
            else:
                result = commit_purge(
                    db_path,
                    platform=args.platform,
                    targets=selection.targets,
                    interrupt_signal=lambda: latch.signal_received,
                )
                if result.rolled_back:
                    if latch.signal_received is not None:
                        interrupt_signum = int(latch.signal_received)
                else:
                    if result.committed:
                        _apply_write_result(summary, result)
                        summary.update(remove_unreferenced_media(db_path, result.media_paths))
                    summary.update(vacuum_and_check(db_path))
                    if latch.signal_received is not None:
                        interrupt_signum = int(latch.signal_received)
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = summary["error"] or f"author_identity_purge_exception:{type(exc).__name__}:{exc}"
    finally:
        if interrupt_signum is None and latch.signal_received is not None:
            interrupt_signum = int(latch.signal_received)
        latch.restore()
        _finalize_summary(summary, interrupt_signum)
        write_json(summary_path, summary)
        print_repair_stream(
            json.dumps({**summary, "summary": str(summary_path)}, ensure_ascii=False),
            sys.stdout,
        )
    if interrupt_signum is not None:
        return 128 + interrupt_signum
    return 0 if summary["status"] == "completed" else 2
