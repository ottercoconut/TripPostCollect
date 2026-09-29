# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/zhihu/help.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

# TripPostCollect：T07 迁入根平台；来源 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303，许可见 resources/licenses/MediaCrawler-LICENSE。

import json
import logging
import re
from hashlib import sha256
from typing import Dict, List, Optional
from urllib.parse import urlsplit, unquote

from parsel import Selector
from trippostcollect.application.contracts import ImageStagingError
from trippostcollect.records.identity import anonymize_user_id, mask_nickname
from trippostcollect.runtime.helpers import extract_text_from_html, normalize_image_url
from . import models as zhihu_constant
from .models import ZhihuContent, ZhihuCreator

logger = logging.getLogger("MediaCrawler")

_ZHIHU_BODY_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "ul",
    }
)


def _first_non_empty(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _normalize_image_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith(("http://", "https://")):
        return url
    return ""


def _find_target_content_entity(
    value,
    target_id: str,
    expected_type: str,
    *,
    depth: int = 0,
) -> Optional[Dict]:
    """Find one exact answer/article entity with a non-empty authoritative body."""

    if depth > 12:
        return None
    if isinstance(value, dict):
        identity = str(value.get("id") or value.get("content_id") or "")
        content_type = str(value.get("type") or value.get("content_type") or "").lower()
        body = value.get("content") or value.get("content_text")
        if (
            identity == str(target_id)
            and content_type == expected_type
            and isinstance(body, str)
            and body.strip()
        ):
            return value
        for nested in value.values():
            found = _find_target_content_entity(
                nested,
                target_id,
                expected_type,
                depth=depth + 1,
            )
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_target_content_entity(
                nested,
                target_id,
                expected_type,
                depth=depth + 1,
            )
            if found is not None:
                return found
    return None


def _detail_json_payloads(html_content: str) -> List[Dict]:
    selector = Selector(text=html_content or "")
    payloads: List[Dict] = []
    for script_text in selector.xpath("//script/text()").getall():
        text = str(script_text or "").strip()
        if not text or text[0] not in "[{":
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            payloads.append(value)
    return payloads


def extract_image_urls_from_html(html_content: str) -> List[str]:
    """
    Extract content image URLs before the HTML is converted to plain text.
    Zhihu lazy-loads images, so prefer original/actual image attributes over
    placeholder data:image values.
    """
    if not html_content:
        return []
    selector = Selector(text=html_content)
    image_urls: List[str] = []
    seen = set()
    for image in selector.css("img"):
        candidates = [
            image.attrib.get("data-original", ""),
            image.attrib.get("data-actualsrc", ""),
            image.attrib.get("src", ""),
        ]
        for candidate in candidates:
            url = _normalize_image_url(candidate)
            if not url or "/equation?" in url or url in seen:
                continue
            seen.add(url)
            image_urls.append(url)
            break
    return image_urls


def extract_zhihu_content_text(html_content: str) -> str:
    """Extract narrative text while preserving block boundaries and omitting figures."""

    if not html_content:
        return ""

    root = Selector(text=html_content, type="html").root
    for node in root.xpath(".//script | .//style"):
        node.drop_tree()
    for node in root.xpath(".//figure"):
        node.tail = "\n" + (node.tail or "")
        node.drop_tree()
    for node in root.xpath(".//figcaption"):
        node.tail = "\n" + (node.tail or "")
        node.drop_tree()

    chunks: List[str] = []

    def append_node_text(node) -> None:
        tag = node.tag.lower() if isinstance(node.tag, str) else ""
        if tag in _ZHIHU_BODY_BLOCK_TAGS:
            chunks.append("\n")
        if node.text:
            chunks.append(node.text)
        for child in node:
            child_tag = child.tag.lower() if isinstance(child.tag, str) else ""
            if child_tag == "br":
                chunks.append("\n")
            else:
                append_node_text(child)
            if child.tail:
                chunks.append(child.tail)
        if tag in _ZHIHU_BODY_BLOCK_TAGS:
            chunks.append("\n")

    append_node_text(root)
    text = "".join(chunks).replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = []
    for line in text.split("\n"):
        normalized = re.sub(r"[^\S\n]+", " ", line).strip()
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def merge_search_content_detail(
    search_content: ZhihuContent,
    detail_content: ZhihuContent,
) -> ZhihuContent:
    """Merge full-page fields without replacing trusted search-author metadata."""
    if detail_content.content_text:
        search_content.content_text = detail_content.content_text
    if detail_content.title and not search_content.title:
        search_content.title = detail_content.title
    if detail_content.desc and not search_content.desc:
        search_content.desc = detail_content.desc
    search_content.image_list = list(detail_content.image_list)
    search_content.image_count = len(search_content.image_list)
    search_content.content_detail_status = "detail_observed"
    search_content.content_detail_source = (
        "answer_detail"
        if detail_content.content_type == zhihu_constant.ANSWER_NAME
        else "article_detail"
    )
    return search_content


class ZhihuExtractor:
    def __init__(self):
        pass

    @staticmethod
    def _apply_author_info(content: ZhihuContent, author_info: ZhihuCreator) -> None:
        content.creator_hash = author_info.creator_hash
        content.creator_url_token = author_info.url_token
        content.user_nickname = author_info.user_nickname
        content.author_profile_url = author_info.profile_url
        content.avatar_url = author_info.avatar_url
        content.followers_count = author_info.fans
        content.followers_observed = author_info.followers_observed
        content.author_followers_source = author_info.author_followers_source
        content.following_count = author_info.follows
        content.author_desc = author_info.headline
        content.verified_text = author_info.verified_text

    def extract_contents_from_search(self, json_data: Dict) -> List[ZhihuContent]:
        """
        extract zhihu contents
        Args:
            json_data: zhihu json data

        Returns:

        """
        if not json_data:
            return []

        search_result: List[Dict] = json_data.get("data", [])
        search_result = [s_item for s_item in search_result if s_item.get("type") in ['search_result', 'zvideo']]
        return self._extract_content_list([sr_item.get("object") for sr_item in search_result if sr_item.get("object")])


    def _extract_content_list(self, content_list: List[Dict]) -> List[ZhihuContent]:
        """
        extract zhihu content list
        Args:
            content_list:

        Returns:

        """
        if not content_list:
            return []

        res: List[ZhihuContent] = []
        for content in content_list:
            if content.get("type") == zhihu_constant.ANSWER_NAME:
                res.append(self._extract_answer_content(content))
            elif content.get("type") == zhihu_constant.ARTICLE_NAME:
                res.append(self._extract_article_content(content))
            elif content.get("type") == zhihu_constant.VIDEO_NAME:
                res.append(self._extract_zvideo_content(content))
            else:
                continue
        return res

    def _extract_answer_content(self, answer: Dict) -> ZhihuContent:
        """
        extract zhihu answer content
        Args:
            answer: zhihu answer

        Returns:
        """
        res = ZhihuContent()
        res.content_id = str(answer.get("id") or "")
        res.content_type = answer.get("type")
        content_html = answer.get("content", "") or ""
        res.content_text = extract_zhihu_content_text(content_html)
        res.image_list = extract_image_urls_from_html(content_html)
        res.image_count = len(res.image_list)
        res.question_id = str(answer.get("question", {}).get("id") or "")
        res.content_url = f"{zhihu_constant.ZHIHU_URL}/question/{res.question_id}/answer/{res.content_id}"
        res.title = extract_text_from_html(answer.get("title", ""))
        res.desc = extract_text_from_html(answer.get("description", "") or answer.get("excerpt", ""))
        res.created_time = answer.get("created_time")
        res.updated_time = answer.get("updated_time")
        res.voteup_count = answer.get("voteup_count", 0)
        res.comment_count = answer.get("comment_count", 0)

        # extract author info
        author_info = self._extract_content_or_comment_author(answer.get("author"))
        self._apply_author_info(res, author_info)
        return res

    def _extract_article_content(self, article: Dict) -> ZhihuContent:
        """
        extract zhihu article content
        Args:
            article: zhihu article

        Returns:

        """
        res = ZhihuContent()
        res.content_id = str(article.get("id") or "")
        res.content_type = article.get("type")
        content_html = article.get("content", "") or ""
        res.content_text = extract_zhihu_content_text(content_html)
        res.image_list = extract_image_urls_from_html(content_html)
        res.image_count = len(res.image_list)
        res.content_url = f"{zhihu_constant.ZHIHU_ZHUANLAN_URL}/p/{res.content_id}"
        res.title = extract_text_from_html(article.get("title"))
        res.desc = extract_text_from_html(article.get("excerpt"))
        res.created_time = article.get("created_time", 0) or article.get("created", 0)
        res.updated_time = article.get("updated_time", 0) or article.get("updated", 0)
        res.voteup_count = article.get("voteup_count", 0)
        res.comment_count = article.get("comment_count", 0)

        # extract author info
        author_info = self._extract_content_or_comment_author(article.get("author"))
        self._apply_author_info(res, author_info)
        return res

    def _extract_zvideo_content(self, zvideo: Dict) -> ZhihuContent:
        """
        extract zhihu zvideo content
        Args:
            zvideo:

        Returns:

        """
        res = ZhihuContent()
        res.content_id = str(zvideo.get("id") or "")
        res.content_type = zvideo.get("type")

        if "video" in zvideo and isinstance(zvideo.get("video"), dict): # This indicates data from the creator's homepage video list API
            res.content_url = f"{zhihu_constant.ZHIHU_URL}/zvideo/{res.content_id}"
            res.created_time = zvideo.get("published_at")
            res.updated_time = zvideo.get("updated_at")
        else:
            res.content_url = zvideo.get("video_url")
            res.created_time = zvideo.get("created_at")
        res.title = extract_text_from_html(zvideo.get("title"))
        res.desc = extract_text_from_html(zvideo.get("description"))
        res.voteup_count = zvideo.get("voteup_count")
        res.comment_count = zvideo.get("comment_count")

        # extract author info
        author_info = self._extract_content_or_comment_author(zvideo.get("author"))
        self._apply_author_info(res, author_info)
        return res

    @staticmethod
    def _extract_content_or_comment_author(author: Dict) -> ZhihuCreator:
        """
        extract zhihu author
        Args:
            author:

        Returns:

        """
        res = ZhihuCreator()
        try:
            if not author:
                return res
            if not author.get("id"):
                author = author.get("member")
            if not author:
                return res
            res.creator_hash = anonymize_user_id(author.get("id"))
            res.url_token = author.get("url_token") or ""
            res.user_nickname = mask_nickname(author.get("name"))
            if res.url_token:
                res.profile_url = f"{zhihu_constant.ZHIHU_URL}/people/{res.url_token}"
            res.avatar_url = author.get("avatar_url") or ""
            res.followers_observed = any(
                key in author and author.get(key) not in (None, "")
                for key in ("follower_count", "followerCount")
            )
            res.fans = _first_non_empty(author.get("follower_count"), author.get("followerCount"), 0)
            res.author_followers_source = "search_author" if res.followers_observed else "missing"
            res.follows = _first_non_empty(author.get("following_count"), author.get("followingCount"), 0)
            res.headline = author.get("headline") or ""
            badge_v2 = author.get("badge_v2") or {}
            res.verified_text = author.get("verify_bayes") or badge_v2.get("title") or ""

        except Exception as e :
            logger.warning(
                f"[ZhihuExtractor._extract_content_or_comment_author] User Maybe Blocked. {e}"
            )
        return res













    def extract_answer_content_from_html(
        self,
        html_content: str,
        answer_id: str = "",
    ) -> Optional[ZhihuContent]:
        """
        extract zhihu answer content from html
        Args:
            html_content:

        Returns:

        """
        payloads = _detail_json_payloads(html_content)
        if not payloads:
            return None
        target = str(answer_id or "")
        if not target:
            answer_info = (
                payloads[0].get("initialState", {}).get("entities", {}).get("answers", {})
            )
            if len(answer_info) == 1:
                target = str(next(iter(answer_info)))
        if not target:
            return None
        for payload in payloads:
            answer = _find_target_content_entity(payload, target, zhihu_constant.ANSWER_NAME)
            if answer is not None:
                return self._extract_answer_content(answer)
        return None

    def extract_article_content_from_html(
        self,
        html_content: str,
        article_id: str = "",
    ) -> Optional[ZhihuContent]:
        """
        extract zhihu article content from html
        Args:
            html_content:

        Returns:

        """
        payloads = _detail_json_payloads(html_content)
        if not payloads:
            return None
        target = str(article_id or "")
        if not target:
            article_info = (
                payloads[0].get("initialState", {}).get("entities", {}).get("articles", {})
            )
            if len(article_info) == 1:
                target = str(next(iter(article_info)))
        if not target:
            return None
        for payload in payloads:
            article = _find_target_content_entity(payload, target, zhihu_constant.ARTICLE_NAME)
            if article is not None:
                return self._extract_article_content(article)
        return None

    def extract_zvideo_content_from_html(self, html_content: str) -> Optional[ZhihuContent]:
        """
        extract zhihu zvideo content from html
        Args:
            html_content:

        Returns:

        """
        js_init_data: str = Selector(text=html_content).xpath("//script[@id='js-initialData']/text()").get(default="")
        if not js_init_data:
            return None
        json_data: Dict = json.loads(js_init_data)
        zvideo_info: Dict = json_data.get("initialState", {}).get("entities", {}).get("zvideos", {})
        users: Dict = json_data.get("initialState", {}).get("entities", {}).get("users", {})
        if not zvideo_info:
            return None

        # handler user info and video info
        video_detail_info: Dict = zvideo_info.get(list(zvideo_info.keys())[0])
        if not video_detail_info:
            return None
        if isinstance(video_detail_info.get("author"), str):
            author_name: str = video_detail_info.get("author")
            video_detail_info["author"] = users.get(author_name)

        return self._extract_zvideo_content(video_detail_info)


def judge_zhihu_url(note_detail_url: str) -> str:
    """
    judge zhihu url type
    Args:
        note_detail_url:
            eg1: https://www.zhihu.com/question/123456789/answer/123456789 # answer
            eg2: https://www.zhihu.com/p/123456789 # article
            eg3: https://www.zhihu.com/zvideo/123456789 # zvideo

    Returns:

    """
    if "/answer/" in note_detail_url:
        return zhihu_constant.ANSWER_NAME
    elif "/p/" in note_detail_url:
        return zhihu_constant.ARTICLE_NAME
    elif "/zvideo/" in note_detail_url:
        return zhihu_constant.VIDEO_NAME
    else:
        return ""


ZHIMG_TRANSFORM_SUFFIX_RE = re.compile(
    r"_(?:[1-9]\d{1,4}w|b|r|qhd|hd|xs|s|m|l|xl|xxl|original|watermark)"
    r"\.(?:avif|gif|jpe?g|png|webp)$",
    re.IGNORECASE,
)

RASTER_SUFFIX_RE = re.compile(r"\.(?:avif|gif|jpe?g|png|webp)$", re.IGNORECASE)


def zhihu_source_asset_key(source_url: str) -> str:
    normalized = normalize_image_url(source_url)
    parsed = urlsplit(normalized)
    hostname = parsed.hostname.lower()
    path = unquote(parsed.path)
    is_zhimg = hostname == "zhimg.com" or hostname.endswith(".zhimg.com")
    if is_zhimg:
        logical_path = ZHIMG_TRANSFORM_SUFFIX_RE.sub("", path)
        identity = RASTER_SUFFIX_RE.sub("", logical_path)
    else:
        identity = f"{hostname}{path}"
    return f"zhihu:urlsha256:{sha256(identity.encode('utf-8')).hexdigest()}"


def zhihu_content_image_assets(content_item: ZhihuContent) -> List[dict]:
    """Project only answer/article body images from an observed content body."""

    if content_item.content_type not in {"answer", "article"}:
        return []
    if content_item.content_detail_status != "detail_observed":
        return []
    excluded_urls = set()
    for value in (content_item.avatar_url, content_item.author_profile_url):
        try:
            excluded_urls.add(normalize_image_url(value))
        except ImageStagingError:
            continue
    assets: List[dict] = []
    seen = set()
    for value in content_item.image_list:
        try:
            url = normalize_image_url(value)
        except ImageStagingError:
            continue
        path = urlsplit(url).path.lower().rstrip("/")
        if url in excluded_urls or path.endswith("/equation") or "/equation/" in f"{path}/":
            continue
        asset_key = zhihu_source_asset_key(url)
        if asset_key in seen:
            continue
        seen.add(asset_key)
        assets.append(
            {"url": url, "source_index": len(assets), "source_asset_key": asset_key}
        )
    return assets


def update_zhihu_content(content_item: ZhihuContent, *, source_keyword: str) -> dict:
    """
    Update Zhihu content
    Args:
        content_item:

    Returns:

    """
    content_item.source_keyword = source_keyword
    content_item.image_assets = zhihu_content_image_assets(content_item)
    content_item.image_list = [asset["url"] for asset in content_item.image_assets]
    content_item.image_count = len(content_item.image_list)
    if content_item.image_assets:
        content_item.image_list_source = "content_html"
    local_db_item = content_item.model_dump()
    return local_db_item
