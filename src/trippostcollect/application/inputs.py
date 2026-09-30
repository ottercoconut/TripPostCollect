"""父侧命令行输入与平台选择，保持原入口参数契约。"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qsl

from trippostcollect.core.paths import DEFAULT_DB, LOCAL_MEDIA_ROOT
from trippostcollect.core.paths import MEDIACRAWLER_RUNS_OUTPUT as DEFAULT_OUTPUT


from trippostcollect.application.contracts import PLATFORMS as PLATFORMS


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


def load_zhihu_detail_urls(path_value: str | Path) -> list[str]:
    path = Path(path_value).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid Zhihu detail URL file: {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemExit("Zhihu detail URL file must contain a JSON array")

    urls: list[str] = []
    for value in payload:
        url = str(value or "").strip().split("#", 1)[0].split("?", 1)[0]
        parsed = urlparse(url)
        answer_url = bool(
            parsed.hostname in {"zhihu.com", "www.zhihu.com"}
            and re.fullmatch(r"/question/[^/]+/answer/[^/]+/?", parsed.path)
        )
        article_url = bool(
            parsed.hostname == "zhuanlan.zhihu.com"
            and re.fullmatch(r"/p/[^/]+/?", parsed.path)
        )
        if not (parsed.scheme == "https" and (answer_url or article_url)):
            raise SystemExit(f"unsupported Zhihu detail URL: {url or value!r}")
        if url not in urls:
            urls.append(url)
    if not urls:
        raise SystemExit("Zhihu detail URL file contains no answer/article URLs")
    return urls



def load_xhs_detail_urls(path_value: str | Path) -> list[str]:
    path = Path(path_value).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid XHS detail URL file: {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemExit("XHS detail URL file must contain a JSON array")

    urls: list[str] = []
    for value in payload:
        raw_url = str(value or "").strip()
        parsed = urlparse(raw_url)
        if parsed.scheme != "https" or parsed.hostname not in {"xiaohongshu.com", "www.xiaohongshu.com"}:
            raise SystemExit(f"unsupported XHS detail URL: {raw_url!r}")
        if not re.fullmatch(r"/explore/[^/]+/?", parsed.path):
            raise SystemExit(f"unsupported XHS detail URL path: {raw_url!r}")
        query = dict(parse_qsl(parsed.query))
        if not query.get("xsec_token") or not query.get("xsec_source"):
            raise SystemExit(f"XHS detail URL must contain xsec_token and xsec_source: {raw_url!r}")
        normalized = parsed._replace(fragment="").geturl()
        if normalized not in urls:
            urls.append(normalized)
    if not urls:
        raise SystemExit("XHS detail URL file contains no URLs")
    return urls



def load_xhs_repair_target_ids(path_value: str | Path) -> set[str]:
    path = Path(path_value).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid XHS repair target ID file: {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemExit("XHS repair target ID file must contain a JSON array")
    values = {str(value).strip() for value in payload if str(value).strip()}
    if not values:
        raise SystemExit("XHS repair target ID file contains no post IDs")
    return values
