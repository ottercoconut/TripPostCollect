from __future__ import annotations

import sqlite3
import sys
from importlib import import_module
from pathlib import Path

from trippostcollect.db.bootstrap import bootstrap_connection


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

mediacrawler_crawl = import_module("mediacrawler_crawl")


def test_content_import_bootstrap_preserves_parent_runner_jobs() -> None:
    one_off_config = {
        "jobs": [
            {
                "job_key": "once_first",
                "site_key": "weibo",
                "target_url": "https://example.invalid/first",
                "job_kind": "mediacrawler_search",
                "enabled": True,
            },
            {
                "job_key": "once_second",
                "site_key": "douyin",
                "target_url": "https://example.invalid/second",
                "job_kind": "mediacrawler_search",
                "enabled": True,
            },
        ]
    }
    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, config=one_off_config)

        result = mediacrawler_crawl.ensure_web_schema(conn)

        jobs = conn.execute(
            "SELECT job_key, enabled FROM crawl_jobs ORDER BY job_key"
        ).fetchall()
    assert result["synced_jobs"] == 0
    assert jobs == [("once_first", 1), ("once_second", 1)]
