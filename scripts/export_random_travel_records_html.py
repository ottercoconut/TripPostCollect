#!/usr/bin/env python
"""Export a balanced, rich travel-record sample from the read-only content database.

The generated report uses the same record, author, metrics, image, capture-context,
and raw-JSON fields exposed by the administration client.  It never changes the
database.  Records that do not pass the travel-relevance check are excluded before
the per-platform random sample is selected.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from datetime import datetime
import html
import json
from pathlib import Path
import random
import sqlite3
from typing import Any

from trippostcollect.core.paths import DEFAULT_DB, OUTPUTS_ROOT
from trippostcollect.records.sanitization import sanitize_author_avatar_data


PLATFORMS = ("bilibili", "douyin", "weibo", "xhs", "zhihu")
# These pools are the result of a widened random-candidate pass followed by a
# manual reading of each post body.  A record is listed here only when its body,
# not merely its title or crawl keyword, is substantively about travel.
BODY_REVIEWED_TRAVEL_IDS = {
    "bilibili": (719, 724, 725, 727, 731, 2022, 732, 737, 738, 742, 743, 949, 953, 958, 975, 4819),
    "douyin": (2570, 2573, 2575, 2576, 2577, 2594, 2596, 2598, 2599, 2600, 2602, 2603),
    "weibo": (5986, 13848, 11468, 1082, 5951, 5879, 5988, 7683, 8765, 8909, 7755, 11614),
    "xhs": (
        8294, 3750, 14461, 1983, 3678, 14389, 10386, 4147, 5842, 13163, 1839, 10314,
        5770, 7465, 2849, 10170, 3931, 5626, 7321, 8403, 3859, 7249, 1010,
    ),
    "zhihu": (5445, 6996, 1298, 1767, 613, 2633, 5410, 13813, 9777, 12554, 1771, 2637, 5414, 13817),
}
TRAVEL_TERMS = (
    "旅游", "旅行", "自由行", "攻略", "游玩", "景点", "行程", "路线", "打卡", "旅拍",
    "民宿", "酒店", "住宿", "海滨", "海边", "沙滩", "浴场", "栈桥", "崂山", "八大关",
    "五四广场", "啤酒博物馆", "小麦岛", "琴岛", "金沙滩", "奥帆", "老城", "黄岛", "即墨",
)
NON_TRAVEL_ONLY_TERMS = ("旅游管理", "旅游专业", "旅游学校", "旅游招聘", "导游资格", "旅游局招聘")
DETAIL_TERMS = tuple(term for term in TRAVEL_TERMS if term != "旅游")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出五平台各 10 条旅游相关的完整管理端记录。")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径。")
    parser.add_argument("--per-platform", type=int, default=10, help="每个平台抽取数量，默认 10。")
    parser.add_argument("--seed", type=int, default=None, help="可选随机种子；提供后可复现抽样。")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="HTML 输出路径；默认写入 outputs/admin_exports/。",
    )
    return parser.parse_args()


def _present(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def rich_record(row: sqlite3.Row) -> bool:
    """Require the values that make a record usable in the administration client."""
    required = (
        "content_text",
        "author_display_name",
        "author_followers_count",
        "published_at",
    )
    return (
        row["status"] == "captured"
        and all(_present(row[key]) for key in required)
        and (_present(row["canonical_url"]) or _present(row["source_url"]))
        and int(row["post_images_count"] or 0) > 0
        and any(
        row[key] is not None
        for key in (
            "post_likes_count",
            "post_favorites_count",
            "post_comments_count",
            "post_shares_count",
            "post_reposts_count",
            "post_views_count",
        )
        )
    )


def travel_evidence(row: sqlite3.Row) -> list[str]:
    """Return evidence from the body alone; title and crawl keyword are ignored."""
    text = str(row["content_text"] or "")
    matched = [term for term in TRAVEL_TERMS if term in text]
    if not matched:
        return []
    only_non_travel_context = any(term in text for term in NON_TRAVEL_ONLY_TERMS) and not any(
        term in text for term in DETAIL_TERMS
    )
    return [] if only_non_travel_context else matched


def selected_rows(conn: sqlite3.Connection, *, count: int, seed: int | None) -> dict[str, list[sqlite3.Row]]:
    rng = random.Random(seed)
    selected: dict[str, list[sqlite3.Row]] = {}
    for platform in PLATFORMS:
        reviewed_ids = BODY_REVIEWED_TRAVEL_IDS[platform]
        placeholders = ",".join("?" for _ in reviewed_ids)
        candidates = conn.execute(
            f"""
            SELECT p.*, sp.display_name AS platform_name
            FROM web_posts p
            LEFT JOIN source_platforms sp ON sp.platform_key = p.platform_key
            WHERE p.platform_key = ? AND p.id IN ({placeholders})
            """,
            (platform, *reviewed_ids),
        ).fetchall()
        candidates = [row for row in candidates if rich_record(row) and travel_evidence(row)]
        if len(candidates) < count:
            raise RuntimeError(
                f"{platform} 仅有 {len(candidates)} 条同时通过正文人工复核、完整度检查和正文证据检查的候选，"
                f"无法抽取 {count} 条。"
            )
        rng.shuffle(candidates)
        selected[platform] = candidates[:count]
    return selected


def json_value(value: str | None) -> Any:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"_unparsed_text": value}
    return sanitize_author_avatar_data(parsed).value


def text(value: Any) -> str:
    return "—" if value is None or value == "" else html.escape(str(value))


def link(url: Any) -> str:
    if not _present(url):
        return "—"
    escaped = html.escape(str(url), quote=True)
    return f'<a href="{escaped}" target="_blank" rel="noreferrer">{escaped}</a>'


def json_block(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    return f"<pre>{html.escape(rendered)}</pre>"


def field_rows(fields: Iterable[tuple[str, Any, bool]]) -> str:
    return "".join(
        f"<tr><th>{html.escape(label)}</th><td>{link(value) if is_url else text(value)}</td></tr>"
        for label, value, is_url in fields
    )


def first_content_line(content: str) -> str:
    return next((line.strip() for line in content.splitlines() if line.strip()), "无标题")[:120]


def record_html(conn: sqlite3.Connection, row: sqlite3.Row, ordinal: int) -> str:
    record_id = int(row["id"])
    images = conn.execute(
        "SELECT * FROM web_post_images WHERE web_post_id = ? AND image_role = 'content' ORDER BY image_index, id",
        (record_id,),
    ).fetchall()
    capture = None
    capture_images: list[sqlite3.Row] = []
    if row["source_capture_id"] is not None:
        capture = conn.execute("SELECT * FROM ctf_captures WHERE id = ?", (row["source_capture_id"],)).fetchone()
        if capture:
            capture_images = conn.execute(
                "SELECT * FROM ctf_capture_images WHERE ctf_capture_id = ? ORDER BY image_index, id",
                (capture["id"],),
            ).fetchall()
    title = row["title"] or first_content_line(str(row["content_text"]))
    title_note = "（原标题为空，以下为正文首行阅读替代）" if not _present(row["title"]) else ""
    record_fields = (
        ("记录 ID", row["id"], False), ("平台", row["platform_name"] or row["platform_key"], False),
        ("平台键", row["platform_key"], False), ("平台帖子 ID", row["platform_post_id"], False),
        ("来源类型", row["source_type"], False), ("来源 URL", row["source_url"], True),
        ("规范 URL", row["canonical_url"], True), ("标题", row["title"], False),
        ("检索词", row["keyword"], False), ("原始发布时间", row["published_at"], False),
        ("抓取时间", row["captured_at"], False), ("状态", row["status"], False),
        ("正文长度", row["content_length"], False), ("正文图片数（主表）", row["post_images_count"], False),
        ("正文图片数（关联表）", len(images), False), ("来源证据 ID", row["source_capture_id"], False),
        ("产物目录", row["artifact_dir"], False), ("抓取方式", row["capture_method"], False),
        ("创建时间", row["created_at"], False), ("更新时间", row["updated_at"], False),
    )
    author_fields = (
        ("作者显示名", row["author_display_name"], False), ("作者平台 ID", row["author_platform_id"], False),
        ("作者主页", row["author_profile_url"], True), ("作者简介", row["author_description"], False),
        ("粉丝数", row["author_followers_count"], False), ("关注数", row["author_following_count"], False),
        ("发帖数", row["author_posts_count"], False), ("平台等级", row["author_platform_level"], False),
        ("认证", row["author_verified"], False), ("认证说明", row["author_verified_text"], False),
    )
    metric_fields = (
        ("点赞", row["post_likes_count"], False), ("收藏", row["post_favorites_count"], False),
        ("评论", row["post_comments_count"], False), ("分享", row["post_shares_count"], False),
        ("转发", row["post_reposts_count"], False), ("浏览", row["post_views_count"], False),
    )
    image_html = "".join(
        "<li>"
        f"#{image['image_index']}（ID {image['id']}）：{link(image['image_url'])}<br>"
        f"本地路径：{text(image['local_path'])}；尺寸：{text(image['width'])} × {text(image['height'])}；"
        f"MIME：{text(image['mime_type'])}；SHA-256：{text(image['sha256'])}"
        f"<details><summary>图片原始 JSON</summary>{json_block(json_value(image['raw_image_json']))}</details>"
        "</li>"
        for image in images
    ) or "<li>—</li>"
    raw_html = "".join(
        f"<details><summary>{label}</summary>{json_block(json_value(row[column]))}</details>"
        for label, column in (("原始记录 JSON", "raw_sample_json"), ("互动指标 JSON", "metrics_json"), ("作者 JSON", "author_json"))
    )
    capture_html = ""
    if capture:
        capture_fields = "".join(
            f"<tr><th>{html.escape(key)}</th><td>{text(value) if not key.endswith('_json') else json_block(json_value(value))}</td></tr>"
            for key, value in dict(capture).items()
        )
        capture_image_html = "".join(
            f"<li>#{image['image_index']}（ID {image['id']}）：{link(image['image_url'])}"
            f"<details><summary>图片原始 JSON</summary>{json_block(json_value(image['raw_image_json']))}</details></li>"
            for image in capture_images
        ) or "<li>—</li>"
        capture_html = f"<details><summary>关联页面证据（ID {capture['id']}）</summary><table>{capture_fields}</table><h4>证据图片</h4><ul>{capture_image_html}</ul></details>"
    evidence = "、".join(travel_evidence(row))
    return f"""
    <article class=\"record\" id=\"record-{record_id}\">
      <h3>{ordinal}. {html.escape(str(title))} <small>{html.escape(title_note)}</small></h3>
      <p class=\"meta\">正文语义人工复核：通过；正文旅游证据：{html.escape(evidence)}；记录 ID：{record_id}</p>
      <h4>记录详情</h4><table>{field_rows(record_fields)}</table>
      <h4>作者信息</h4><table>{field_rows(author_fields)}</table>
      <h4>互动指标</h4><table>{field_rows(metric_fields)}</table>
      <h4>正文</h4><div class=\"content\">{html.escape(str(row['content_text']))}</div>
      <h4>正文图片</h4><ul>{image_html}</ul>
      <h4>原始 JSON（已递归移除作者头像字段和其重复 URL）</h4>{raw_html}
      {capture_html}
    </article>
    """


def report_html(conn: sqlite3.Connection, sample: dict[str, list[sqlite3.Row]], seed: int | None) -> str:
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    sections = []
    ordinal = 1
    for platform in PLATFORMS:
        rows = sample[platform]
        platform_name = rows[0]["platform_name"] or platform
        cards = "".join(record_html(conn, row, ordinal + index) for index, row in enumerate(rows))
        ordinal += len(rows)
        sections.append(f"<section><h2>{html.escape(str(platform_name))}（{platform}） · {len(rows)} 条</h2>{cards}</section>")
    seed_display = "系统随机" if seed is None else str(seed)
    return f"""<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>旅游相关记录随机抽样（五平台各十条）</title>
<style>
body {{ max-width: 1180px; margin: 32px auto; padding: 0 18px 50px; color: #172033; background: #f6f8fb; font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
h1 {{ margin-bottom: 4px; }} h2 {{ margin-top: 38px; border-bottom: 2px solid #2667b5; padding-bottom: 7px; }} h3 {{ margin: 0; font-size: 18px; }} h4 {{ margin: 22px 0 8px; }} small {{ font-size: 12px; color: #687386; font-weight: normal; }}
.intro, .record {{ background: white; border: 1px solid #dbe2ec; border-radius: 10px; padding: 20px; box-shadow: 0 1px 2px #182a4410; }} .record {{ margin: 16px 0; }} .meta {{ color: #3d5878; }}
table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }} th, td {{ border: 1px solid #dce3ec; padding: 7px 9px; vertical-align: top; overflow-wrap: anywhere; }} th {{ width: 27%; text-align: left; background: #f1f5f9; }}
a {{ color: #0759a5; }} .content {{ white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; border-left: 3px solid #87aeda; background: #f8fbff; }} pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #111827; color: #e5e7eb; padding: 12px; border-radius: 6px; }} details {{ margin: 8px 0; }} summary {{ cursor: pointer; color: #0759a5; }} ul {{ padding-left: 24px; }}
</style></head><body>
<h1>旅游相关记录随机抽样</h1>
<div class=\"intro\"><p>从 <code>web_posts</code> 只读抽取，共 {sum(map(len, sample.values()))} 条：五个平台各 10 条。</p>
<p>生成时间：{html.escape(created_at)}；随机种子：{html.escape(seed_display)}。先扩大随机候选池并逐条阅读正文，只有正文实质涉及旅行攻略、行程路线、景点游览、住宿交通、亲子游或真实旅行记录的内容才进入合格池；再从字段完整（正文、作者、粉丝、发布时间、URL、至少一张正文图片及互动指标）的合格池中按平台随机抽取。标题和检索词不作为正文相关性的通过依据。仅排除导出候选，不修改数据库。</p>
<p>所有管理端可展示的记录、作者、指标、内容图片、关联证据及 JSON 已列出；数据库中本来为空的可选字段显示为“—”。</p></div>
{''.join(sections)}
</body></html>"""


def main() -> None:
    args = parse_args()
    if args.per_platform < 1:
        raise SystemExit("--per-platform 必须大于 0。")
    db_path = args.db.expanduser().resolve()
    if not db_path.is_file():
        raise SystemExit(f"数据库不存在：{db_path}")
    output = args.output or OUTPUTS_ROOT / "admin_exports" / "旅游相关记录_五平台各10条.html"
    output = output.expanduser().resolve()
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        sample = selected_rows(conn, count=args.per_platform, seed=args.seed)
        rendered = report_html(conn, sample, args.seed)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(f"已导出 {sum(map(len, sample.values()))} 条记录：{output}")
    for platform in PLATFORMS:
        print(f"{platform}: {[int(row['id']) for row in sample[platform]]}")


if __name__ == "__main__":
    main()
