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
from trippostcollect.records.identity import platform_nickname, platform_user_id
from trippostcollect.runtime.helpers import extract_text_from_html, normalize_image_url
from . import models as zhihu_constant
from .models import ZhihuContent, ZhihuCreator, ZhihuSearchPage

logger = logging.getLogger("TripPostCollect")

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


def _optional_count(*values):
    """第一个既不是 None 也不是空串的值；都没有时返回 None。数值 0 视为已取到。"""
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _metric(source: Dict, *keys: str):
    """这些键里第一个既非 None 也非空串的值，包含 0。键都缺失或为空时返回 None，不补 0。"""

    if not isinstance(source, dict):
        return None
    for key in keys:
        if key not in source:
            continue
        value = source.get(key)
        if value in (None, ""):
            continue
        return value
    return None


def _present_int(source: Dict, *keys: str) -> int:
    value = _metric(source, *keys)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)




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


def _entity_in_bucket(payload: Dict, bucket: str, content_id: str) -> Optional[Dict]:
    """详情页实体在 ``initialState.entities.answers|articles.{id}``。"""

    entities = payload.get("initialState", {})
    entities = entities.get("entities", {}) if isinstance(entities, dict) else {}
    group = entities.get(bucket) if isinstance(entities, dict) else None
    entity = group.get(str(content_id)) if isinstance(group, dict) else None
    if isinstance(entity, dict):
        if not entity.get("id"):
            entity = {**entity, "id": str(content_id)}
        if not entity.get("type"):
            entity = {
                **entity,
                "type": zhihu_constant.ANSWER_NAME if bucket == "answers" else zhihu_constant.ARTICLE_NAME,
            }
        return entity
    return None


def _find_entity_by_identity(value, target_id: str, expected_type: str, *, depth: int = 0) -> Optional[Dict]:
    """按 id 与类型找实体，不要求正文非空，用来区分“没有实体”和“实体没有正文”。"""

    if depth > 12:
        return None
    if isinstance(value, dict):
        identity = str(value.get("id") or value.get("content_id") or "")
        content_type = str(value.get("type") or value.get("content_type") or "").lower()
        if identity == str(target_id) and content_type == expected_type:
            return value
        for nested in value.values():
            found = _find_entity_by_identity(nested, target_id, expected_type, depth=depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_entity_by_identity(nested, target_id, expected_type, depth=depth + 1)
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
    search_content.voteup_count = detail_content.voteup_count
    search_content.comment_count = detail_content.comment_count
    search_content.collected_count = detail_content.collected_count
    search_content.created_time = detail_content.created_time
    search_content.updated_time = detail_content.updated_time
    if detail_content.content_visit_refs:
        search_content.content_visit_refs = dict(detail_content.content_visit_refs)
    # 只有作者页补全过的详情才覆盖搜索作者；未进作者页时保留搜索侧粉丝证据。
    if detail_content.author_followers_source in {"author_page", "missing"}:
        search_content.followers_count = detail_content.followers_count
        search_content.followers_observed = detail_content.followers_observed
        search_content.author_followers_source = detail_content.author_followers_source
        search_content.following_count = detail_content.following_count
        search_content.author_posts_count = detail_content.author_posts_count
        search_content.author_page_counts = dict(detail_content.author_page_counts)
        if detail_content.creator_url_token:
            search_content.creator_url_token = detail_content.creator_url_token
    search_content.content_detail_status = "detail_observed"
    search_content.content_detail_source = (
        "answer_detail"
        if detail_content.content_type == zhihu_constant.ANSWER_NAME
        else "article_detail"
    )
    return search_content


_AUTHOR_PAGE_COUNT_KEYS = ("answerCount", "articlesCount", "pinsCount", "questionCount")


def user_from_initial_data(state_text: str, url_token: str) -> dict | None:
    """作者页 ``initialState.entities.users.{url_token}``。没有该用户时返回 None。"""

    text = str(state_text or "").strip()
    token = str(url_token or "")
    if not text or not token or text[0] not in "{[":
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    users = payload.get("initialState", {}).get("entities", {}).get("users", {})
    user = users.get(token) if isinstance(users, dict) else None
    return user if isinstance(user, dict) else None


def apply_author_page(content: ZhihuContent, user: dict, url_token: str) -> None:
    """用作者页用户实体覆盖粉丝、关注和作品数。认证判定保持不动。

    粉丝数只认 ``followerCount``，来源标为作者页。关注数缺键为 None。作品数是
    ``answerCount + articlesCount``，任一缺失则为 None。四个原值缺键不写入。
    """

    content.creator_url_token = url_token
    if "followerCount" in user and type(user.get("followerCount")) is int:
        content.followers_count = user["followerCount"]
        content.followers_observed = True
        content.author_followers_source = "author_page"
    else:
        content.followers_observed = False
        content.author_followers_source = "missing"
    content.following_count = _optional_count(user.get("followingCount")) if "followingCount" in user else None
    answer = user.get("answerCount") if "answerCount" in user else None
    articles = user.get("articlesCount") if "articlesCount" in user else None
    if type(answer) is int and type(articles) is int:
        content.author_posts_count = answer + articles
    else:
        content.author_posts_count = None
    content.author_page_counts = {
        key: user[key]
        for key in _AUTHOR_PAGE_COUNT_KEYS
        if key in user and user[key] is not None
    }


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
        return self.extract_search_page(json_data).contents

    def extract_search_page(self, json_data: Dict) -> ZhihuSearchPage:
        """Project one search_v3 page; zvideo is dropped here and only its ID is kept.

        `raw_count` and `is_end` come from the raw response so the caller can tell a
        filtered-out page from a verified source end.
        """
        if not json_data:
            return ZhihuSearchPage()

        raw_items = json_data.get("data") or []
        paging = json_data.get("paging") or {}
        objects = [
            s_item.get("object")
            for s_item in raw_items
            if isinstance(s_item, dict)
            and s_item.get("type") in ("search_result", "zvideo")
            and isinstance(s_item.get("object"), dict)
        ]
        video_ids = [
            str(item.get("id") or "")
            for item in objects
            if item.get("type") == zhihu_constant.VIDEO_NAME
        ]
        return ZhihuSearchPage(
            contents=self._extract_content_list(
                [item for item in objects if item.get("type") != zhihu_constant.VIDEO_NAME]
            ),
            video_ids=video_ids,
            raw_count=len(raw_items),
            is_end=isinstance(paging, dict) and paging.get("is_end") is True,
        )


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
        question = answer.get("question")
        if isinstance(question, dict):
            res.question_id = str(question.get("id") or "")
        elif question not in (None, ""):
            res.question_id = str(question)
        else:
            res.question_id = ""
        res.content_url = f"{zhihu_constant.ZHIHU_URL}/question/{res.question_id}/answer/{res.content_id}"
        res.title = extract_text_from_html(answer.get("title", ""))
        res.desc = extract_text_from_html(answer.get("description", "") or answer.get("excerpt", ""))
        res.created_time = _present_int(answer, "created_time", "createdTime")
        res.updated_time = _present_int(answer, "updated_time", "updatedTime")
        res.voteup_count = _metric(answer, "voteup_count", "voteupCount")
        res.comment_count = _metric(answer, "comment_count", "commentCount")
        res.collected_count = _metric(answer, "favlists_count", "favlistsCount")

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
        res.created_time = _present_int(article, "created_time", "createdTime", "created")
        res.updated_time = _present_int(article, "updated_time", "updatedTime", "updated")
        res.voteup_count = _metric(article, "voteup_count", "voteupCount")
        res.comment_count = _metric(article, "comment_count", "commentCount")
        res.collected_count = _metric(article, "favlists_count", "favlistsCount")

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
        res.voteup_count = _metric(zvideo, "voteup_count", "voteupCount")
        res.comment_count = _metric(zvideo, "comment_count", "commentCount")

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
            # 作者平台 ID 取原始 id；主页链接仍由 url_token 拼接。
            res.creator_hash = platform_user_id(author.get("id"))
            res.url_token = author.get("url_token") or author.get("urlToken") or ""
            res.user_nickname = platform_nickname(author.get("name"))
            if res.url_token:
                res.profile_url = f"{zhihu_constant.ZHIHU_URL}/people/{res.url_token}"
            res.avatar_url = author.get("avatar_url") or ""
            res.followers_observed = any(
                key in author and author.get(key) not in (None, "")
                for key in ("follower_count", "followerCount")
            )
            res.fans = _first_non_empty(author.get("follower_count"), author.get("followerCount"), 0)
            res.author_followers_source = "search_author" if res.followers_observed else "missing"
            # 关注数缺键或空串表示没取到，不能补 0；键上的 0 仍是 0（#182）。
            res.follows = _optional_count(author.get("following_count"), author.get("followingCount"))
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

    def content_from_initial_data(
        self,
        state_text: str,
        content_id: str,
        content_type: str,
    ) -> tuple[Optional[ZhihuContent], str]:
        """解析 ``script#js-initialData`` 文本里的目标实体。

        返回 ``(内容, "")``，或 ``(None, 失败码)``。失败码是 ``detail_entity_missing``
        （页面状态里没有该 id 的回答/文章）或 ``detail_content_missing``（实体没有正文）。
        """

        text = str(state_text or "").strip()
        if not text or text[0] not in "{[":
            return None, "detail_entity_missing"
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None, "detail_entity_missing"
        if not isinstance(payload, dict):
            return None, "detail_entity_missing"
        bucket = "answers" if content_type == zhihu_constant.ANSWER_NAME else "articles"
        entity = _entity_in_bucket(payload, bucket, content_id)
        if entity is None:
            entity = _find_entity_by_identity(payload, content_id, content_type)
        if entity is None:
            return None, "detail_entity_missing"
        body = entity.get("content") if isinstance(entity.get("content"), str) else ""
        if not body.strip():
            return None, "detail_content_missing"
        if content_type == zhihu_constant.ANSWER_NAME:
            return self._extract_answer_content(entity), ""
        return self._extract_article_content(entity), ""

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
