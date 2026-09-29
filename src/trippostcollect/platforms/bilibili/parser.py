"""B站图文正文与字段解析。"""

from __future__ import annotations

import html
import re
from typing import Any

from trippostcollect.records.images import normalize_image_url
from .models import BilibiliArticleDetailError


BILIBILI_HTML_IMAGE_RE = re.compile(
    r"<(?:img|source)\b[^>]*?\b(?:src|data-src|data-original)\s*=\s*"
    r"(?:[\"']([^\"']+)[\"']|([^\s>]+))",
    re.I,
)


def clean_html_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_bilibili_article_record(item: dict[str, Any], keyword: str) -> dict[str, Any] | None:
    post_id = str(item.get("id") or "").strip()
    if not post_id:
        return None
    title = clean_html_text(item.get("title"))
    desc = clean_html_text(item.get("desc"))
    if not title and not desc:
        return None
    content_url = str(item.get("arcurl") or item.get("url") or f"https://www.bilibili.com/read/cv{post_id}/")
    search_preview_urls = item.get("image_urls") if isinstance(item.get("image_urls"), list) else []
    record = dict(item)
    record.update(
        {
            "id": post_id,
            "content_id": post_id,
            "content_type": "article",
            "title": title,
            "desc": desc,
            "search_desc": desc,
            "search_excerpt_length": len(desc),
            "search_preview_urls": search_preview_urls,
            "search_preview_count": len(search_preview_urls),
            "content_text": "",
            "content_detail_status": "search_only",
            "content_detail_source": "article_search_api",
            "content_images_detail_status": "search_only",
            "content_url": content_url,
            "image_urls": [],
            "source_keyword": keyword,
            "created_time": item.get("pubdate") or item.get("pub_time"),
            "published_at": item.get("pubdate") or item.get("pub_time"),
            "liked_count": item.get("like"),
            "comment_count": item.get("reply"),
            "view_count": item.get("view"),
            "nickname": item.get("author"),
            "user_id": item.get("mid"),
            "raw_bilibili_type": item.get("type"),
        }
    )
    return record


def clean_bilibili_article_body(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|li|blockquote|h[1-6]|section|article)\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_bilibili_detail_image_url(value: Any) -> str | None:
    url = normalize_image_url(value)
    if url and url.startswith("http://"):
        return "https://" + url.removeprefix("http://")
    return url


def extract_bilibili_detail_images(detail: dict[str, Any]) -> tuple[list[str], list[str]]:
    images: list[str] = []
    sources: list[str] = []
    seen: set[str] = set()

    def add(value: Any, source: str) -> None:
        url = normalize_bilibili_detail_image_url(value)
        if not url or url in seen:
            return
        seen.add(url)
        images.append(url)
        sources.append(source)

    opus = detail.get("opus") if isinstance(detail.get("opus"), dict) else {}
    opus_content = opus.get("content") if isinstance(opus.get("content"), dict) else {}
    paragraphs = opus_content.get("paragraphs") if isinstance(opus_content.get("paragraphs"), list) else []
    for paragraph in paragraphs:
        if not isinstance(paragraph, dict):
            continue
        pic = paragraph.get("pic") if isinstance(paragraph.get("pic"), dict) else {}
        pics = pic.get("pics") if isinstance(pic.get("pics"), list) else []
        for item in pics:
            if isinstance(item, dict):
                add(item.get("url") or item.get("src"), "opus_paragraph_pic")
            else:
                add(item, "opus_paragraph_pic")

    content = str(detail.get("content") or "")
    for match in BILIBILI_HTML_IMAGE_RE.finditer(content):
        add(match.group(1) or match.group(2), "content_html_img")

    for key in ("content_pic_list",):
        values = detail.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, dict):
                add(
                    item.get("url") or item.get("src") or item.get("image_url"),
                    f"detail_{key}",
                )
            else:
                add(item, f"detail_{key}")

    if not images:
        for key in ("origin_image_urls", "image_urls"):
            values = detail.get(key)
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, dict):
                    add(
                        item.get("url") or item.get("src") or item.get("image_url"),
                        f"detail_{key}",
                    )
                else:
                    add(item, f"detail_{key}")
    return images, sources


def hydrate_bilibili_article_record(
    search_record: dict[str, Any],
    detail: dict[str, Any],
    *,
    attempts: int,
    retry_wait_seconds: float = 0.0,
    pacing_wait_seconds: float = 0.0,
) -> dict[str, Any]:
    post_id = str(search_record.get("content_id") or search_record.get("id") or "")
    body = clean_bilibili_article_body(detail.get("content"))
    if not body:
        raise BilibiliArticleDetailError(
            f"bilibili article detail {post_id} has no body",
            retryable=True,
            code=0,
            attempts=attempts,
        )
    image_urls, image_sources = extract_bilibili_detail_images(detail)
    opus = detail.get("opus") if isinstance(detail.get("opus"), dict) else {}
    opus_content = opus.get("content") if isinstance(opus.get("content"), dict) else {}
    paragraphs = opus_content.get("paragraphs") if isinstance(opus_content.get("paragraphs"), list) else []
    hydrated = dict(search_record)
    hydrated.update(
        {
            "title": clean_html_text(detail.get("title")) or search_record.get("title"),
            "content_text": body,
            "content_length": len(body),
            "content_detail_status": "detail_observed",
            "content_detail_source": "article_view_api",
            "content_detail_attempts": attempts,
            "content_detail_retry_wait_seconds": round(retry_wait_seconds, 3),
            "content_detail_pacing_wait_seconds": round(pacing_wait_seconds, 3),
            "content_images_detail_status": "detail_observed",
            "detail_image_urls": image_urls,
            "image_urls": image_urls,
            "detail_image_count": len(image_urls),
            "detail_image_sources": image_sources,
            "detail_opus_observed": bool(opus),
            "detail_opus_paragraph_count": len(paragraphs),
            "detail_content_image_token_count": body.count("图片"),
            "content_detail_evidence": {
                "source": "article_view_api",
                "attempts": attempts,
                "retry_wait_seconds": round(retry_wait_seconds, 3),
                "pacing_wait_seconds": round(pacing_wait_seconds, 3),
                "content_length": len(body),
                "image_count": len(image_urls),
                "image_sources": image_sources,
                "opus_observed": bool(opus),
                "opus_paragraph_count": len(paragraphs),
            },
        }
    )
    return hydrated

