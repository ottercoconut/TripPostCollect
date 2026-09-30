"""按显式数据库、发现作用域与恢复文件读取已知集合。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse


def existing_platform_identities(
    platform: str,
    *,
    db_path: str,
    xhs_target_key: str = "",
    xhs_account_id: str = "",
    xhs_fingerprint: str = "",
    job_id: str = "",
    fingerprint: str = "",
    resume_identities_path: str = "",
) -> set[str]:
    db_value = db_path.strip()
    rows = []
    if db_value:
        try:
            with sqlite3.connect(Path(db_value).expanduser()) as conn:
                rows = conn.execute(
                    """
                    SELECT platform_post_id, canonical_url
                    FROM web_posts
                    WHERE platform_key=?
                    """,
                    (platform,),
                ).fetchall()
                if platform == "xhs":
                    target_key = xhs_target_key.strip()
                    account_id = xhs_account_id.strip()
                    fingerprint = xhs_fingerprint.strip()
                    if target_key and account_id and fingerprint:
                        try:
                            seen_rows = conn.execute(
                                """
                                SELECT platform_post_id
                                FROM xhs_discovery_seen_candidates
                                WHERE target_key=? AND account_id=? AND query_fingerprint=?
                                """,
                                (target_key, account_id, fingerprint),
                            ).fetchall()
                        except sqlite3.Error:
                            seen_rows = []
                        rows.extend(
                            (platform_post_id, None)
                            for (platform_post_id,) in seen_rows
                        )
                else:
                    job_id = job_id.strip()
                    fingerprint = fingerprint.strip()
                    if job_id and fingerprint:
                        try:
                            seen_rows = conn.execute(
                                """
                                SELECT platform_post_id
                                FROM crawl_discovery_seen_candidates
                                WHERE job_id=? AND platform_key=? AND query_fingerprint=?
                                """,
                                (int(job_id), platform, fingerprint),
                            ).fetchall()
                        except (ValueError, sqlite3.Error):
                            seen_rows = []
                        rows.extend(
                            (platform_post_id, None)
                            for (platform_post_id,) in seen_rows
                        )
                        try:
                            excluded_rows = conn.execute(
                                """
                                SELECT platform_post_id
                                FROM crawl_discovery_candidate_exclusions
                                WHERE job_id=? AND platform_key=? AND query_fingerprint=?
                                """,
                                (int(job_id), platform, fingerprint),
                            ).fetchall()
                        except (ValueError, sqlite3.Error):
                            excluded_rows = []
                        rows.extend(
                            (platform_post_id, None)
                            for (platform_post_id,) in excluded_rows
                        )
        except (OSError, sqlite3.Error):
            rows = []
    identities: set[str] = set()
    for platform_post_id, canonical_url in rows:
        if platform_post_id:
            identities.add(str(platform_post_id))
        if canonical_url:
            path_parts = [part for part in urlparse(str(canonical_url)).path.split("/") if part]
            if path_parts:
                identities.add(path_parts[-1])
    resume_value = resume_identities_path.strip()
    if resume_value:
        try:
            resume_identities = json.loads(Path(resume_value).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            resume_identities = []
        identities.update(str(value) for value in resume_identities if value not in (None, ""))
    return identities
