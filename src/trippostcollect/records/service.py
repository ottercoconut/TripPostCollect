"""Record aggregation service."""

from __future__ import annotations

from typing import Any

from trippostcollect.core.json_utils import parse_json_text
from trippostcollect.records.repository import RecordFilters, RecordListResult, RecordRepository


class RecordService:
    def __init__(self, repository: RecordRepository) -> None:
        self.repository = repository

    def list_records(
        self,
        filters: RecordFilters,
        *,
        page: int,
        page_size: int,
        sort: str,
    ) -> RecordListResult:
        result = self.repository.list_records(filters, page=page, page_size=page_size, sort=sort)
        return RecordListResult(
            items=[self._record_summary(row) for row in result.items],
            meta=result.meta,
        )

    def get_record_detail(self, record_id: int) -> dict[str, Any] | None:
        record = self.repository.get_record(record_id)
        if record is None:
            return None
        return self._record_detail(record)

    def get_record_context(self, record_id: int) -> dict[str, Any] | None:
        record = self.repository.get_record(record_id)
        if record is None:
            return None
        images = self.repository.list_record_images(record_id)
        capture = self.repository.get_capture_for_record(record)
        capture_images = self.repository.list_capture_images(int(capture["id"])) if capture else []
        return {
            **self._record_detail(record),
            "images": images,
            "capture": capture,
            "capture_images": capture_images,
            "artifacts": self._artifact_summary(capture),
        }

    def get_record_raw(self, record_id: int) -> dict[str, Any] | None:
        record = self.repository.get_record(record_id)
        if record is None:
            return None
        capture = self.repository.get_capture_for_record(record)
        return {
            "record": {
                "raw_sample_json": parse_json_text(record.get("raw_sample_json"), default={}),
                "metrics_json": parse_json_text(record.get("metrics_json"), default={}),
                "author_json": parse_json_text(record.get("author_json"), default={}),
            },
            "capture": None if capture is None else {
                "raw_meta_json": parse_json_text(capture.get("raw_meta_json"), default={}),
                "navigation_json": parse_json_text(capture.get("navigation_json"), default={}),
                "image_summary_json": parse_json_text(capture.get("image_summary_json"), default={}),
                "validation_json": parse_json_text(capture.get("validation_json"), default={}),
            },
        }

    def _record_summary(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": record["id"],
            "platform_key": record["platform_key"],
            "platform_name": record.get("platform_name"),
            "platform_post_id": record.get("platform_post_id"),
            "source_type": record.get("source_type"),
            "source_url": record.get("source_url"),
            "canonical_url": record.get("canonical_url"),
            "title": record.get("title"),
            "author_display_name": record.get("author_display_name"),
            "published_at": record.get("published_at"),
            "captured_at": record.get("captured_at"),
            "city_name": record.get("city_name"),
            "keyword": record.get("keyword"),
            "content_text": record.get("content_text"),
            "post_images_count": int(record.get("image_count") or record.get("post_images_count") or 0),
            "post_likes_count": record.get("post_likes_count"),
            "post_comments_count": record.get("post_comments_count"),
            "status": record.get("status"),
        }

    def _record_detail(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "record": self._record_summary(record) | {
                "author_description": record.get("author_description"),
                "content_length": record.get("content_length"),
                "artifact_dir": record.get("artifact_dir"),
                "capture_method": record.get("capture_method"),
                "source_capture_id": record.get("source_capture_id"),
                "created_at": record.get("created_at"),
                "updated_at": record.get("updated_at"),
            },
            "author": {
                "display_name": record.get("author_display_name"),
                "platform_id": record.get("author_platform_id"),
                "profile_url": record.get("author_profile_url"),
                "avatar_url": record.get("author_avatar_url"),
                "description": record.get("author_description"),
                "followers_count": record.get("author_followers_count"),
                "following_count": record.get("author_following_count"),
                "posts_count": record.get("author_posts_count"),
                "platform_level": record.get("author_platform_level"),
                "verified": bool(record.get("author_verified")) if record.get("author_verified") is not None else None,
                "verified_text": record.get("author_verified_text"),
            },
            "metrics": {
                "likes": record.get("post_likes_count"),
                "favorites": record.get("post_favorites_count"),
                "comments": record.get("post_comments_count"),
                "shares": record.get("post_shares_count"),
                "reposts": record.get("post_reposts_count"),
                "views": record.get("post_views_count"),
            },
            "raw_summary": {
                "record_json_fields": ["raw_sample_json", "metrics_json", "author_json"],
                "has_capture_raw_meta": bool(record.get("source_capture_id")),
            },
        }

    @staticmethod
    def _artifact_summary(capture: dict[str, Any] | None) -> dict[str, Any]:
        if not capture:
            return {}
        return {
            "artifact_dir": capture.get("artifact_dir"),
            "capture_meta_path": capture.get("capture_meta_path"),
            "screenshot_path": capture.get("screenshot_path"),
            "visible_text_path": capture.get("visible_text_path"),
            "rendered_html_path": capture.get("rendered_html_path"),
        }
