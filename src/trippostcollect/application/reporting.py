"""采集摘要、行为汇总与报告写出。"""

from __future__ import annotations

from pathlib import Path
from trippostcollect.application.inputs import PLATFORMS as PLATFORMS
from trippostcollect.core.paths import ensure_parent
from trippostcollect.records.formal import is_video_record as is_video_record
from trippostcollect.records.formal import platform_from_path as platform_from_path
from trippostcollect.records.formal import published_at_for_record as published_at_for_record
from trippostcollect.runtime.behavior import behavior_evidence_valid
from trippostcollect.runtime.helpers import _runtime_progress as _runtime_progress
from trippostcollect.runtime.helpers import _runtime_progress_if_due as _runtime_progress_if_due
from typing import Any
from typing import Callable
from urllib.parse import unquote
import json
import os
import time
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".svg", ".img"}


VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}


AUTHOR_FIELD_MARKERS = ("author", "user", "nickname", "avatar", "fans", "follower", "follow", "up")


SAMPLE_KEYS = (
    "title",
    "desc",
    "content",
    "published_at",
    "create_time",
    "publish_time",
    "time",
    "create_date_time",
    "note_id",
    "aweme_id",
    "content_id",
    "content_type",
    "video_id",
    "bvid",
    "aid",
    "note_url",
    "video_url",
    "content_url",
    "created_time",
    "updated_time",
    "user_id",
    "nickname",
    "user_nickname",
    "fans",
    "fans_count",
    "followers_count",
    "following_count",
    "aweme_count",
    "author_liked_count",
    "author_followers_source",
    "liked_count",
    "voteup_count",
    "collected_count",
    "comment_count",
    "comments_count",
    "share_count",
    "shared_count",
)


def item_type_from_path(path: Path) -> str:
    name = path.name
    if "_contents_" in name:
        return "contents"
    if "_comments_" in name:
        return "comments"
    if "_creators_" in name:
        return "creators"
    return "unknown"


def truncate(value: Any, limit: int = 240) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def extract_sample(record: dict[str, Any]) -> dict[str, str]:
    sample = {key: truncate(record[key]) for key in SAMPLE_KEYS if record.get(key) not in (None, "")}
    published_at = published_at_for_record(record)
    if published_at and "published_at" not in sample:
        sample["published_at"] = published_at
    return sample or {key: truncate(value) for key, value in list(record.items())[:8]}


def summarize_jsonl(
    path: Path,
    keyword: str,
    *,
    progress_callback: Callable[[], object] | None = None,
) -> dict[str, Any]:
    item_type = item_type_from_path(path)
    platform_key = platform_from_path(path)
    fields: set[str] = set()
    author_like_fields: set[str] = set()
    samples: list[dict[str, str]] = []
    line_count = 0
    parse_errors = 0
    keyword_hits = 0
    video_like_records = 0
    published_at_records = 0
    _runtime_progress(progress_callback)
    last_checkpoint_at = time.monotonic()
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            last_checkpoint_at = _runtime_progress_if_due(
                progress_callback,
                last_checkpoint_at,
            )
            text = line.strip()
            if not text:
                continue
            line_count += 1
            if keyword in text:
                keyword_hits += 1
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            if isinstance(record, dict):
                fields.update(record)
                author_like_fields.update(
                    key for key in record if any(marker in key.lower() for marker in AUTHOR_FIELD_MARKERS)
                )
                is_video = item_type == "contents" and is_video_record(platform_key, record)
                if is_video:
                    video_like_records += 1
                if item_type == "contents" and not is_video and published_at_for_record(record):
                    published_at_records += 1
                if item_type == "contents" and len(samples) < 3:
                    samples.append(extract_sample(record))
    _runtime_progress(progress_callback)
    return {
        "path": str(path),
        "item_type": item_type,
        "line_count": line_count,
        "keyword_hit_records": keyword_hits,
        "parse_errors": parse_errors,
        "video_like_records": video_like_records,
        "published_at_records": published_at_records,
        "top_level_fields": sorted(fields),
        "author_like_fields": sorted(author_like_fields),
        "samples": samples,
    }


def summarize_output(
    save_path: Path,
    keyword: str,
    *,
    progress_callback: Callable[[], object] | None = None,
) -> dict[str, Any]:
    _runtime_progress(progress_callback)
    last_checkpoint_at = time.monotonic()
    files: list[Path] = []
    if save_path.exists():
        for path in save_path.rglob("*"):
            last_checkpoint_at = _runtime_progress_if_due(
                progress_callback,
                last_checkpoint_at,
            )
            if path.is_file():
                files.append(path)
    files.sort()
    all_jsonl_files = [path for path in files if path.suffix.lower() == ".jsonl"]
    image_manifest_paths = [path for path in all_jsonl_files if path.name == "image_manifest.jsonl"]
    jsonl_files = [path for path in all_jsonl_files if path.name != "image_manifest.jsonl"]
    jsonl = [
        summarize_jsonl(
            path,
            keyword,
            progress_callback=progress_callback,
        )
        for path in jsonl_files
    ]
    counts = {"contents": 0, "comments": 0, "creators": 0, "unknown": 0}
    fields: set[str] = set()
    author_like_fields: set[str] = set()
    samples: list[dict[str, str]] = []
    keyword_hits = 0
    parse_errors = 0
    video_like_records = 0
    published_at_records = 0
    for item in jsonl:
        last_checkpoint_at = _runtime_progress_if_due(
            progress_callback,
            last_checkpoint_at,
        )
        counts[item["item_type"]] = counts.get(item["item_type"], 0) + item["line_count"]
        fields.update(item["top_level_fields"])
        author_like_fields.update(item["author_like_fields"])
        keyword_hits += item["keyword_hit_records"]
        parse_errors += item["parse_errors"]
        video_like_records += int(item.get("video_like_records") or 0)
        published_at_records += int(item.get("published_at_records") or 0)
        samples.extend(item["samples"])

    image_files = [path for path in files if path.suffix.lower() in IMAGE_SUFFIXES]
    video_files = [path for path in files if path.suffix.lower() in VIDEO_SUFFIXES]
    _runtime_progress(progress_callback)
    return {
        "save_path": str(save_path),
        "jsonl_files": [str(path) for path in jsonl_files],
        "image_manifest_paths": [str(path) for path in image_manifest_paths],
        "jsonl_file_count": len(jsonl_files),
        "content_records": counts.get("contents", 0),
        "non_video_content_records": max(0, counts.get("contents", 0) - video_like_records),
        "video_like_records": video_like_records,
        "comment_records": counts.get("comments", 0),
        "creator_records": counts.get("creators", 0),
        "unknown_records": counts.get("unknown", 0),
        "total_jsonl_records": sum(counts.values()),
        "keyword_hit_records": keyword_hits,
        "parse_errors": parse_errors,
        "image_file_count": len(image_files),
        "video_file_count": len(video_files),
        "published_at_records": published_at_records,
        "top_level_fields": sorted(fields)[:120],
        "author_like_fields": sorted(author_like_fields),
        "samples": samples[:5],
        "files": jsonl,
    }


def summarize_output_with_progress(
    save_path: Path,
    keyword: str,
    progress_callback: Callable[[], object] | None,
) -> dict[str, Any]:
    if progress_callback is None:
        return summarize_output(save_path, keyword)
    return summarize_output(
        save_path,
        keyword,
        progress_callback=progress_callback,
    )


def write_json_with_progress(
    path: Path,
    value: Any,
    *,
    progress_callback: Callable[[], object] | None = None,
    trailing_newline: bool = False,
) -> None:
    """Atomically stream JSON while keeping synchronous supervision alive."""

    ensure_parent(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoder = json.JSONEncoder(ensure_ascii=False, indent=2)
    _runtime_progress(progress_callback)
    last_checkpoint_at = time.monotonic()
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            for chunk in encoder.iterencode(value):
                for start in range(0, len(chunk), 1024 * 1024):
                    handle.write(chunk[start : start + 1024 * 1024])
                    last_checkpoint_at = _runtime_progress_if_due(
                        progress_callback,
                        last_checkpoint_at,
                    )
            if trailing_newline:
                handle.write("\n")
        _runtime_progress(progress_callback)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def terminal_summary_envelope(
    *,
    summary_path: Path,
    report_path: Path,
    batch_dir: Path,
    summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "summary": str(summary_path),
        "report": str(report_path),
        "batch_dir": str(batch_dir),
        "status": "completed" if summary["import_completion_met"] else "failed",
        "import_completion_met": summary["import_completion_met"],
        "failure_reason": summary.get("failure_reason"),
    }


def collect_behavior_validation(
    records: list[dict[str, Any]],
    platforms: list[str],
    keyword: str,
    xhs_post_interaction: str = "none",
    repair_mode: bool = False,
) -> dict[str, Any]:
    latest_by_platform: dict[str, dict[str, Any]] = {}
    for record in reversed(records):
        platform_key = str(record.get("platform") or "")
        if platform_key in platforms and platform_key not in latest_by_platform:
            latest_by_platform[platform_key] = record

    platform_results: dict[str, Any] = {}
    for platform_key in platforms:
        record = latest_by_platform.get(platform_key) or {}
        evidence = record.get("behavior_evidence") or {}
        behavior_profile = str(evidence.get("profile") or "")
        expected_profile = "xhs_guarded" if platform_key == "xhs" else "social_high_risk"
        profile_ok = behavior_profile == expected_profile
        pacing_events = [
            item
            for item in evidence.get("request_pacing_events") or []
            if isinstance(item, dict)
        ]
        pacing_stages = {str(item.get("stage") or "") for item in pacing_events}
        continuity_events = [
            item
            for item in evidence.get("continuity_events") or []
            if isinstance(item, dict)
        ]
        continuity_stages = {
            str(item.get("stage") or "")
            for item in continuity_events
            if item.get("status") == "completed"
        }
        required_pacing_stages = set()
        if platform_key == "xhs":
            required_pacing_stages = (
                {"note_detail", "creator_profile"}
                if repair_mode
                else {"search_results", "note_detail", "creator_profile"}
            )
        pacing_ok = required_pacing_stages.issubset(pacing_stages)
        continuity_ok = (
            platform_key != "xhs"
            or repair_mode
            or "search_results" in continuity_stages
        )
        post_interactions = [
            item
            for item in evidence.get("post_interactions") or []
            if isinstance(item, dict)
        ]
        interaction_requested = platform_key == "xhs" and xhs_post_interaction != "none"
        interaction_ok = (not interaction_requested) or any(
            item.get("requested_mode") == xhs_post_interaction and item.get("status") == "completed"
            for item in post_interactions
        )
        policy_events = record.get("policy_events") or []
        policy_allowed = any(
            isinstance(event, dict)
            and event.get("allowed") is True
            and (platform_key == "xhs" or event.get("disabled") is not True)
            for event in policy_events
        )
        behavior_url = str(evidence.get("url") or "")
        target_url_ok = bool(keyword) and keyword in unquote(behavior_url)
        platform_results[platform_key] = {
            "behavior_ok": (
                behavior_evidence_valid(evidence)
                and target_url_ok
                and profile_ok
                and pacing_ok
                and continuity_ok
            ),
            "behavior_status": str(evidence.get("status") or "missing"),
            "behavior_profile": behavior_profile,
            "behavior_profile_ok": profile_ok,
            "behavior_event_count": len(evidence.get("events") or []),
            "request_pacing_event_count": len(pacing_events),
            "request_pacing_stages": sorted(pacing_stages),
            "request_pacing_ok": pacing_ok,
            "continuity_event_count": len(continuity_events),
            "continuity_stages": sorted(continuity_stages),
            "continuity_ok": continuity_ok,
            "post_interaction_requested": interaction_requested,
            "post_interaction_mode": xhs_post_interaction if platform_key == "xhs" else "none",
            "post_interaction_ok": interaction_ok,
            "post_interactions": post_interactions,
            "behavior_url": behavior_url,
            "target_url_ok": target_url_ok,
            "policy_allowed": policy_allowed,
            "policy_event_count": len(policy_events),
            "evidence_path": str(evidence.get("evidence_path") or ""),
        }

    behavior_ok = bool(platform_results) and all(item["behavior_ok"] for item in platform_results.values())
    policy_ok = bool(platform_results) and all(item["policy_allowed"] for item in platform_results.values())
    return {
        "required": True,
        "ok": behavior_ok and policy_ok,
        "behavior_ok": behavior_ok,
        "policy_ok": policy_ok,
        "required_platforms": platforms,
        "platforms": platform_results,
    }


def latest_platform_result_counts(
    records: list[dict[str, Any]],
    platforms: list[str],
) -> dict[str, int]:
    latest_by_platform: dict[str, dict[str, Any]] = {}
    for record in records:
        platform_key = str(record.get("platform") or "")
        if platform_key in platforms:
            latest_by_platform[platform_key] = record
    latest_records = list(latest_by_platform.values())
    return {
        "ok_count": sum(1 for record in latest_records if record.get("ok")),
        "skipped_video_only_count": sum(
            1 for record in latest_records if record.get("status") == "skipped_video_only"
        ),
        "failed_count": sum(1 for record in latest_records if not record.get("ok")),
    }


def write_markdown(summary: dict[str, Any], path: Path) -> None:
    lines = [
        "# MediaCrawler 结构化抓取摘要",
        "",
        f"- 时间：`{summary['captured_at']}`",
        f"- 关键词：`{summary['keyword']}`",
        f"- 输出目录：`{summary['batch_dir']}`",
        "",
        "| 平台 | 状态 | 图文内容记录 | 发帖时间记录 | 跳过视频记录 | 图片文件 | 视频文件(应为0) | 作者字段 |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for record in summary["records"]:
        output = record["output"]
        lines.append(
            "| {label} | {status} | {contents} | {published_at} | {skipped_videos} | {images} | {videos} | {fields} |".format(
                label=record.get("label") or PLATFORMS[record["platform"]]["label"],
                status=record["status"],
                contents=output["non_video_content_records"],
                published_at=output["published_at_records"],
                skipped_videos=output["video_like_records"],
                images=output["image_file_count"],
                videos=output["video_file_count"],
                fields=", ".join(output["author_like_fields"]) or "无",
            )
        )
    lines.extend(["", "## 样本", ""])
    for record in summary["records"]:
        label = record.get("label") or PLATFORMS[record["platform"]]["label"]
        lines.append(f"### {label}")
        samples = record["output"].get("samples") or []
        if not samples:
            lines.append("")
            lines.append("无")
            lines.append("")
            continue
        for sample in samples[:2]:
            lines.append(f"- `{json.dumps(sample, ensure_ascii=False)}`")
        lines.append("")
    validation = summary.get("formal_validation") or {}
    if validation:
        pagination = validation.get("pagination_evidence") or {}
        stop_event = pagination.get("stop_event") or {}
        lines.extend(
            [
                "## 正式校验",
                "",
                "- 完成策略：`source-exhausted`（唯一正式策略）",
                f"- 实际候选：`{validation.get('candidate_count', 0)}`",
                f"- 主题相关有效新增图文：`{validation.get('valid_new_count', 0)}`",
                f"- 主题相关有效旧记录：`{validation.get('valid_existing_count', 0)}`（更新统计）",
                f"- 主题不相关结构有效新增：`{validation.get('topic_irrelevant_new_count', 0)}`（入库审计）",
                f"- 主题不相关结构有效旧记录：`{validation.get('topic_irrelevant_existing_count', 0)}`",
                f"- 来源耗尽达成：`{validation.get('source_exhausted_met', False)}`",
                f"- 修复成功子集可入库：`{validation.get('repair_import_met', False)}`",
                f"- 修复选中目标全部有效：`{validation.get('all_repair_targets_valid')}`",
                f"- 本轮完成门禁达成：`{validation.get('completion_met', False)}`",
                f"- 停止原因：`{validation.get('stop_reason', '')}`",
                f"- 停止细节：`{validation.get('stop_detail', '')}`",
                f"- 已处理分页批次：`{pagination.get('batch_count', 0)}`",
                f"- 正常停止事件：`{pagination.get('stopped', False)}`",
                f"- 已记录跳过候选：`{stop_event.get('skipped_candidate_count', 0)}`",
                f"- 无效原因计数：`{json.dumps(validation.get('invalid_reason_counts') or {}, ensure_ascii=False, sort_keys=True)}`",
                "",
            ]
        )
    behavior_validation = summary.get("behavior_validation") or {}
    if behavior_validation:
        lines.extend(
            [
                "## 行为与策略门禁",
                "",
                f"- 总体通过：`{behavior_validation.get('ok', False)}`",
                f"- 人类行为证据通过：`{behavior_validation.get('behavior_ok', False)}`",
                f"- 请求预算与冷却门禁通过：`{behavior_validation.get('policy_ok', False)}`",
                f"- 平台证据：`{json.dumps(behavior_validation.get('platforms') or {}, ensure_ascii=False, sort_keys=True)}`",
                "",
            ]
        )
    image_materialization = summary.get("image_materialization") or {}
    if image_materialization:
        lines.extend(
            [
                "## 图片本地化",
                "",
                f"- 正式要求：`{image_materialization.get('required', False)}`",
                f"- 候选帖子：`{image_materialization.get('candidate_posts', 0)}`",
                f"- 预期正文图：`{image_materialization.get('expected_images', 0)}`",
                f"- staging 下载：`{image_materialization.get('downloaded_images', 0)}`",
                f"- 根项目字节复验：`{image_materialization.get('validated_images', 0)}`",
                f"- SHA-256 唯一正文图：`{image_materialization.get('unique_images', 0)}`",
                f"- SHA-256 重复来源：`{image_materialization.get('sha256_duplicate_images', 0)}`",
                f"- 新晋升文件：`{image_materialization.get('promoted_images', 0)}`",
                f"- 失败回滚文件：`{image_materialization.get('rolled_back_images', 0)}`",
                f"- 复用文件：`{image_materialization.get('reused_images', 0)}`",
                f"- 可恢复失败：`{image_materialization.get('retryable_failures', 0)}`",
                f"- 终态失败：`{image_materialization.get('terminal_failures', 0)}`",
                f"- 完整：`{image_materialization.get('complete', False)}`",
                "",
            ]
        )
    import_result = summary.get("import_result") or {}
    if import_result:
        db_sync = import_result.get("db_sync") or {}
        db_value = import_result.get("db") or db_sync.get("db", "")
        lines.extend(
            [
                "## 入库",
                "",
                f"- 数据库：`{db_value}`",
                f"- JSONL 文件数：`{import_result.get('jsonl_files', 0)}`",
                f"- 处理行：`{import_result.get('processed_rows', 0)}`",
                f"- 新增行：`{import_result.get('inserted_rows', 0)}`",
                f"- 更新行：`{import_result.get('updated_rows', 0)}`",
                f"- 相关新增/更新：`{import_result.get('topic_relevant_inserted_rows', 0)}` / `{import_result.get('topic_relevant_updated_rows', 0)}`",
                f"- 不相关新增/更新：`{import_result.get('topic_irrelevant_inserted_rows', 0)}` / `{import_result.get('topic_irrelevant_updated_rows', 0)}`",
                f"- 跳过记录：`{import_result.get('skipped', 0)}`",
                f"- 跳过视频记录：`{import_result.get('skipped_video', 0)}`",
                f"- 解析错误：`{import_result.get('parse_errors', 0)}`",
                f"- 同步任务：`{db_sync.get('synced_jobs', '')}`",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")

