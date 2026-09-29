"""父侧命令行输入与平台选择，保持原入口参数契约。"""

from __future__ import annotations

import argparse

from trippostcollect.core.paths import DEFAULT_DB, LOCAL_MEDIA_ROOT
from trippostcollect.core.paths import MEDIACRAWLER_RUNS_OUTPUT as DEFAULT_OUTPUT


PLATFORMS: dict[str, dict[str, str]] = {
    "bilibili": {"mediacrawler": "bili", "label": "B站"},
    "xhs": {"mediacrawler": "xhs", "label": "小红书"},
    "weibo": {"mediacrawler": "wb", "label": "微博"},
    "douyin": {"mediacrawler": "dy", "label": "抖音"},
    "zhihu": {"mediacrawler": "zhihu", "label": "知乎"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run MediaCrawler for supported structured social platforms.")
    parser.add_argument("--keyword", default="青岛旅游", help="Qingdao search keyword.")
    parser.add_argument(
        "--platforms",
        nargs="+",
        default=["weibo", "douyin"],
        help="bilibili weibo douyin zhihu; XHS is a low-level target selected only by xhs_runner.py",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT), help="Output root.")
    parser.add_argument(
        "--timeout-per-platform",
        type=int,
        default=180,
        help="Maximum seconds without durable MediaCrawler progress.",
    )
    parser.add_argument("--login-type", default="cookie", choices=("cookie", "qrcode", "phone"), help="MediaCrawler login type.")
    parser.add_argument("--get-media", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--download-images",
        action="store_true",
        help="Download and verify authoritative post-body images for all selected platforms. Videos remain disabled.",
    )
    parser.add_argument(
        "--media-root",
        default=str(LOCAL_MEDIA_ROOT),
        help="Immutable local image root. Overrides are restricted to the project temp directory.",
    )
    parser.add_argument("--headed", action="store_true", help="Run browser with visible UI.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path for web_posts import.")
    parser.add_argument("--required-fields-profile", default="image_post_with_followers_v1")
    parser.add_argument("--behavior-profile", default="social_high_risk")
    parser.add_argument("--xhs-account-id", help="Required isolated account id for the XHS low-level executor.")
    parser.add_argument("--xhs-profile-dir", help="Required run-scoped browser profile for XHS.")
    parser.add_argument("--xhs-discovery-target-key", help=argparse.SUPPRESS)
    parser.add_argument("--xhs-discovery-query-fingerprint", help=argparse.SUPPRESS)
    parser.add_argument(
        "--xhs-post-interaction",
        choices=("none", "comment-scroll", "like-one", "random"),
        default="none",
        help="Optional one-post visible XHS interaction selected by xhs_runner.py.",
    )
    parser.add_argument("--start-page", type=int, default=1, help="Recovery-only first platform page.")
    parser.add_argument("--start-offset", type=int, default=0, help="Saved platform offset for the discovery frontier.")
    parser.add_argument("--start-cursor", default="", help="Saved opaque platform cursor for the discovery frontier.")
    parser.add_argument("--resume-summary", help="Recovery-only prior summary whose JSONL records join this run.")
    parser.add_argument("--discovery-job-id", type=int)
    parser.add_argument("--discovery-query-fingerprint")
    parser.add_argument("--discovery-run-id")
    parser.add_argument("--top-refresh-max-pages", type=int, default=0)
    parser.add_argument("--discovery-source-exhausted", action="store_true")
    parser.add_argument("--no-checkpoint-write", action="store_true")
    parser.add_argument("--no-import", action="store_true", help="Do not import MediaCrawler JSONL records into SQLite.")
    parser.add_argument(
        "--zhihu-detail-urls-file",
        help="Diagnostic-only JSON array of Zhihu answer/article URLs to inspect via detail mode.",
    )
    parser.add_argument(
        "--xhs-detail-urls-file",
        help="Existing XHS note URLs to inspect via detail mode; only allowed with --xhs-repair.",
    )
    parser.add_argument(
        "--xhs-repair-target-ids-file",
        help="JSON array of existing XHS post IDs allowed in --xhs-repair mode.",
    )
    parser.add_argument(
        "--xhs-repair",
        action="store_true",
        help="Repair existing XHS rows from specified note detail URLs without discovery writes.",
    )
    parser.add_argument(
        "--xhs-repair-batch-size",
        type=int,
        default=5,
        help="Number of isolated XHS repair candidates processed per in-browser batch.",
    )
    parser.add_argument(
        "--post-repair",
        action="store_true",
        help=(
            "Repair an explicit allowlist of existing Douyin, Weibo, or Zhihu rows "
            "through platform detail mode without discovery writes."
        ),
    )
    parser.add_argument(
        "--repair-targets-file",
        help=(
            "Frozen JSON array of repair target objects containing platform_post_id, "
            "detail_target, and keyword; requires --post-repair."
        ),
    )
    return parser.parse_args()


def selected_platforms(values: list[str]) -> list[str]:
    unknown = sorted(set(values) - set(PLATFORMS))
    if unknown:
        raise SystemExit(f"Unsupported MediaCrawler platform in this project: {', '.join(unknown)}")
    return values
