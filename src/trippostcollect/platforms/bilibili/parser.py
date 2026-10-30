"""B站图文正文与字段解析。"""

from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit

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


# 新编辑器（template_id=4）文章的 content 是 Quill delta：{"ops": [{"insert": ...}, ...]}。
BILIBILI_DELTA_IMAGE_EMBED = "native-image"


def _bilibili_delta_ops(value: Any) -> list[Any] | None:
    """content 是 delta JSON 时返回 ops 列表，否则返回 None（按 HTML 处理）。"""

    if not isinstance(value, str) or not value.lstrip().startswith("{"):
        return None
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("ops"), list):
        return None
    return payload["ops"]


def _normalize_bilibili_body_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def bilibili_delta_to_text(raw: str) -> str | None:
    """把 delta JSON 正文拼成纯文本；raw 不是 delta 或拼不出文本时返回 None。

    字符串 insert 原样拼接，保留其中的换行与空行段落；对象 insert（图片、分割线、卡片）不进入文本。
    纯函数，不做网络与数据库访问，供正式抓取和库内回填共用。
    """

    ops = _bilibili_delta_ops(raw)
    if ops is None:
        return None
    pieces = [
        op["insert"]
        for op in ops
        if isinstance(op, dict) and isinstance(op.get("insert"), str)
    ]
    text = _normalize_bilibili_body_text("".join(pieces).replace("\r\n", "\n").replace("\r", "\n"))
    return text or None


def bilibili_delta_image_urls(value: Any) -> list[str]:
    """delta 正文里 native-image 嵌入的图片地址，按出现顺序；不是 delta 时为空。"""

    urls: list[str] = []
    for op in _bilibili_delta_ops(value) or []:
        embed = op.get("insert") if isinstance(op, dict) else None
        image = embed.get(BILIBILI_DELTA_IMAGE_EMBED) if isinstance(embed, dict) else None
        if isinstance(image, dict) and image.get("url"):
            urls.append(str(image["url"]))
    return urls


def clean_bilibili_article_body(value: Any) -> str:
    if _bilibili_delta_ops(value) is not None:
        return bilibili_delta_to_text(value) or ""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|li|blockquote|h[1-6]|section|article)\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return _normalize_bilibili_body_text(html.unescape(text))


def normalize_bilibili_detail_image_url(value: Any) -> str | None:
    url = normalize_image_url(value)
    if url and url.startswith("http://"):
        return "https://" + url.removeprefix("http://")
    return url


def bilibili_bfs_asset_key(url: str) -> str:
    """BFS 图片的稳定身份：同一张图在 hdslb/biliimg、不同变换后缀和扩展名下取同一个键。"""

    path = re.sub(r"@.*$", "", unquote(urlsplit(url).path))
    if "/bfs/" not in path:
        return url
    asset = path.split("/bfs/", 1)[1].lstrip("/")
    return re.sub(r"\.(?:avif|gif|jpe?g|png|webp)$", "", asset, flags=re.I)


def extract_bilibili_detail_images(detail: dict[str, Any]) -> tuple[list[str], list[str]]:
    images: list[str] = []
    sources: list[str] = []
    seen: set[str] = set()

    def add(value: Any, source: str) -> None:
        url = normalize_bilibili_detail_image_url(value)
        if not url:
            return
        # Opus 段落图与 delta 嵌入图常是同一张图的不同主机/变换地址，按 BFS 资产去重。
        key = bilibili_bfs_asset_key(url)
        if key in seen:
            return
        seen.add(key)
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
    if _bilibili_delta_ops(content) is not None:
        for url in bilibili_delta_image_urls(content):
            add(url, "content_delta_image")
    else:
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
    # origin_image_urls / image_urls 是文章封面（banner），不属于正文图；没有正文图就是没有。
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

