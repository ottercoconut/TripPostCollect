"""正式内容的 SQLite 入库与提交确定性。"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from pathlib import Path
from trippostcollect.application.contracts import XhsRuntimeSupervisionError as XhsRuntimeSupervisionError
from trippostcollect.artifacts.image_candidates import image_items_for_record
from trippostcollect.application.contracts import ImagePersistenceError
from trippostcollect.artifacts.image_persistence import existing_image_records as _existing_image_records
from trippostcollect.artifacts.image_persistence import normalize_persistence_items as _normalize_persistence_items
from trippostcollect.artifacts.image_persistence import prepare_image_rows as _prepare_image_rows
from trippostcollect.artifacts.image_persistence import replace_image_rows
from trippostcollect.core.paths import LOCAL_MEDIA_ROOT
from trippostcollect.core.paths import PROJECT_ROOT
from trippostcollect.core.paths import ensure_parent
from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.db.connection import connect_db
from trippostcollect.records import formal as _formal
from trippostcollect.runtime.helpers import _runtime_progress as _runtime_progress
from typing import Any
from typing import Callable
from typing import TYPE_CHECKING
import sqlite3

if TYPE_CHECKING:
    from trippostcollect.artifacts.image_materialization import MaterializedImage
FORMAL_SQLITE_BUSY_TIMEOUT_MS = 60_000


def row_for_record(
    platform_key: str,
    record: dict[str, Any],
    *,
    artifact_dir: str,
    captured_at: str,
    keyword: str,
    materialized_images: list[MaterializedImage] | None = None,
) -> dict[str, Any]:
    return _formal.row_for_record(
        platform_key, record, artifact_dir=artifact_dir, captured_at=captured_at,
        keyword=keyword, materialized_images=materialized_images,
        image_items_for_record=image_items_for_record, ImagePersistenceError=ImagePersistenceError,
    )


def ensure_web_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    return bootstrap_connection(conn, sync_jobs=False)


def find_existing_post(conn: sqlite3.Connection, row: dict[str, Any]) -> int | None:
    if row.get("platform_post_id"):
        found = conn.execute(
            "SELECT id FROM web_posts WHERE platform_key=? AND platform_post_id=?",
            (row["platform_key"], row["platform_post_id"]),
        ).fetchone()
        if found:
            return int(found[0])
    if row.get("canonical_url"):
        found = conn.execute(
            "SELECT id FROM web_posts WHERE platform_key=? AND canonical_url=?",
            (row["platform_key"], row["canonical_url"]),
        ).fetchone()
        if found:
            return int(found[0])
    return None


def upsert_web_post(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    *,
    project_root: str | Path = PROJECT_ROOT,
    media_root: str | Path = LOCAL_MEDIA_ROOT,
    require_local_images: bool = False,
) -> tuple[int, bool]:
    post_row = dict(row)
    image_items = _normalize_persistence_items(list(post_row.pop("_image_items", [])))
    content_count = sum(item["role"] == "content" for item in image_items)
    if post_row.get("post_images_count") is not None and int(post_row["post_images_count"]) != content_count:
        raise ImagePersistenceError("post_images_count does not match projected content images")
    existing_id = find_existing_post(conn, post_row)
    platform_key = str(post_row.get("platform_key") or "")
    existing_images = _existing_image_records(conn, existing_id) if existing_id else []
    resolved_project_root = Path(project_root).expanduser().resolve(strict=True)
    resolved_media_root = Path(media_root).expanduser().resolve()
    if resolved_media_root != resolved_project_root and resolved_project_root not in resolved_media_root.parents:
        raise ImagePersistenceError("media root escapes project root")
    prepared_images = _prepare_image_rows(
        platform_key,
        image_items,
        existing_images,
        project_root=resolved_project_root,
        media_root=resolved_media_root,
        require_local_images=require_local_images,
    )

    conn.execute("SAVEPOINT trippostcollect_web_post_upsert")
    try:
        columns = list(post_row)
        if existing_id:
            updates = ", ".join(f"{column}=:{column}" for column in columns)
            conn.execute(
                f"UPDATE web_posts SET {updates}, updated_at=datetime('now') WHERE id=:id",
                {**post_row, "id": existing_id},
            )
            post_id = existing_id
        else:
            placeholders = ", ".join(f":{column}" for column in columns)
            conn.execute(
                f"INSERT INTO web_posts ({', '.join(columns)}) VALUES ({placeholders})",
                post_row,
            )
            post_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        replace_image_rows(conn, post_id, prepared_images)
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT trippostcollect_web_post_upsert")
        conn.execute("RELEASE SAVEPOINT trippostcollect_web_post_upsert")
        raise
    conn.execute("RELEASE SAVEPOINT trippostcollect_web_post_upsert")
    return post_id, existing_id is None


class FormalImportBeforeCommitError(RuntimeError):

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause


def commit_formal_import(conn: sqlite3.Connection) -> None:
    conn.commit()


def import_valid_records(
    summary: dict[str, Any],
    selected: list[dict[str, Any]],
    db_path: Path,
    *,
    project_root: str | Path = PROJECT_ROOT,
    media_root: str | Path = LOCAL_MEDIA_ROOT,
    require_local_images: bool = False,
    progress_callback: Callable[[], object] | None = None,
) -> dict[str, Any]:
    _runtime_progress(progress_callback)
    db_path = ensure_parent(db_path)
    captured_at = str(summary.get("captured_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    keyword = str(summary.get("keyword") or "")
    processed = inserted = updated = 0
    relevant_inserted = relevant_updated = 0
    irrelevant_inserted = irrelevant_updated = 0
    conn = connect_db(db_path, busy_timeout_ms=FORMAL_SQLITE_BUSY_TIMEOUT_MS)
    commit_started = False
    try:
        db_sync = ensure_web_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        for item in selected:
            _runtime_progress(progress_callback)
            record = item["record"]
            platform_key = str(item["platform"])
            row = row_for_record(
                platform_key,
                record,
                artifact_dir=str(Path(str(summary.get("batch_dir") or "")).resolve()),
                captured_at=captured_at,
                keyword=keyword,
                materialized_images=item.get("materialized_images"),
            )
            _, was_inserted = upsert_web_post(
                conn,
                row,
                project_root=project_root,
                media_root=media_root,
                require_local_images=require_local_images,
            )
            processed += 1
            inserted += int(was_inserted)
            updated += int(not was_inserted)
            relevant = bool(row["topic_relevant"])
            relevant_inserted += int(relevant and was_inserted)
            relevant_updated += int(relevant and not was_inserted)
            irrelevant_inserted += int(not relevant and was_inserted)
            irrelevant_updated += int(not relevant and not was_inserted)
        commit_started = True
        _runtime_progress(progress_callback)
        commit_formal_import(conn)
    except XhsRuntimeSupervisionError:
        conn.rollback()
        raise
    except BaseException as exc:
        if commit_started and not conn.in_transaction:
            raise
        conn.rollback()
        raise FormalImportBeforeCommitError(exc) from exc
    finally:
        conn.close()
    return {
        "db": str(db_path),
        "db_sync": db_sync,
        "processed_rows": processed,
        "inserted_rows": inserted,
        "updated_rows": updated,
        "topic_relevant_inserted_rows": relevant_inserted,
        "topic_relevant_updated_rows": relevant_updated,
        "topic_irrelevant_inserted_rows": irrelevant_inserted,
        "topic_irrelevant_updated_rows": irrelevant_updated,
        "skipped": 0,
        "skipped_video": 0,
        "parse_errors": 0,
    }
