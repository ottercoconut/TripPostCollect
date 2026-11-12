"""抖音作者资料回填的冻结清单、观测和 child。

不写内容记录、不下载正文图、不写 checkpoint。作品只接受与既有 aweme_id 完全相同的数字 ID。
观测值是回填当时的作者资料，不是原始抓取时刻的。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from trippostcollect.platforms.douyin import parser
from trippostcollect.records.identity import platform_user_id
from trippostcollect.records.sanitization import public_payload


AWEME_ID_RE = re.compile(r"[0-9]+")
# 连续这么多位作者都只得到空详情时，按运行级阻断停止，避免签名或接口整体失效时把全部作者误记为取不回。
CONSECUTIVE_EMPTY_DETAIL_STOP = 10
EMPTY_DETAIL_BLOCKER = "consecutive_empty_detail"
FILLABLE_COLUMNS = (
    "author_profile_url",
    "author_description",
    "author_verified",
    "author_verified_text",
)


def author_profile_fields(author: dict[str, Any], creator_profile: dict[str, Any]) -> dict[str, Any]:
    """用入库同一组解析函数得到简介、认证两列和主页链接。"""

    extras = parser._profile_author_extras(creator_profile if isinstance(creator_profile, dict) else {})
    profile_url = parser._author_profile_url(
        author if isinstance(author, dict) else {},
        creator_profile if isinstance(creator_profile, dict) else {},
    )
    fields: dict[str, Any] = {}
    if extras.get("author_desc"):
        fields["author_description"] = extras["author_desc"]
    if "author_verified" in extras:
        fields["author_verified"] = extras["author_verified"]
    if extras.get("author_verified_text"):
        fields["author_verified_text"] = extras["author_verified_text"]
    if profile_url:
        fields["author_profile_url"] = profile_url
    evidence = extras.get("author_verify_evidence")
    if isinstance(evidence, dict) and evidence:
        fields["author_verify_evidence"] = evidence
    cleaned = public_payload(fields)
    return cleaned if isinstance(cleaned, dict) else {}


def detail_failure_reason(detail: Any, aweme_id: str) -> str:
    if not isinstance(detail, dict) or not detail:
        return "empty_detail"
    if str(detail.get("aweme_id") or "") != str(aweme_id):
        return "empty_detail"
    status = detail.get("status")
    deleted = detail.get("is_delete") in (1, True, "1")
    if isinstance(status, dict) and status.get("is_delete") in (1, True, "1"):
        deleted = True
    if deleted:
        return "aweme_deleted"
    author = detail.get("author")
    if not isinstance(author, dict) or not author:
        return "no_author"
    return ""


def has_fillable_fields(fields: dict[str, Any]) -> bool:
    return any(fields.get(column) is not None for column in FILLABLE_COLUMNS)


def validate_author_works(authors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只接受 aweme_id 与 platform_post_id 完全相同的数字作品 ID。"""

    validated: list[dict[str, Any]] = []
    for index, author in enumerate(authors):
        if not isinstance(author, dict):
            raise ValueError(f"author profile target {index} is not an object")
        author_id = str(author.get("author_platform_id") or "").strip()
        works_in = author.get("works")
        if not author_id or not isinstance(works_in, list) or not works_in:
            raise ValueError(f"author profile target {index} requires author_platform_id and works")
        works: list[dict[str, str]] = []
        for work in works_in:
            if not isinstance(work, dict):
                raise ValueError(f"author profile work for {author_id} is not an object")
            aweme_id = str(work.get("aweme_id") or "")
            post_id = str(work.get("platform_post_id") or "")
            if aweme_id != post_id or AWEME_ID_RE.fullmatch(aweme_id) is None:
                raise ValueError(f"author profile work does not match aweme_id: {aweme_id!r}")
            works.append({"aweme_id": aweme_id, "platform_post_id": post_id})
        validated.append({"author_platform_id": author_id, "works": works})
    return validated


def load_manifest(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    authors = payload.get("authors") if isinstance(payload, dict) else None
    if not isinstance(authors, list):
        raise ValueError("author profile manifest requires an authors list")
    return validate_author_works(authors)


def _profile_reason(aweme: dict[str, Any]) -> str:
    error = str(aweme.get("creator_profile_error") or "")
    if error == "creator_profile_empty" or not error:
        return "profile_empty"
    return "profile_unavailable"


async def observe_work(crawler: Any, author_id: str, aweme_id: str, semaphore: asyncio.Semaphore) -> dict[str, Any]:
    detail = await crawler.get_aweme_detail(aweme_id, semaphore)
    reason = detail_failure_reason(detail, aweme_id)
    if reason:
        return {"reason": reason}
    author = detail.get("author") or {}
    if platform_user_id(author.get("uid")) != platform_user_id(author_id):
        return {"reason": "author_mismatch"}
    enriched = await crawler.enrich_aweme_creator(detail)
    blocker = str(enriched.get("creator_profile_runtime_error") or "")
    if blocker:
        return {"runtime_blocker": blocker}
    profile = enriched.get("creator_profile")
    if not isinstance(profile, dict) or not profile:
        return {"reason": _profile_reason(enriched if isinstance(enriched, dict) else {})}
    fields = author_profile_fields(author, profile)
    if not has_fillable_fields(fields):
        return {"reason": "profile_insufficient"}
    return {
        "observation": {
            "author_platform_id": platform_user_id(author_id),
            "source_aweme_id": aweme_id,
            "fields": {key: fields[key] for key in FILLABLE_COLUMNS if key in fields},
            "author_verify_evidence": fields.get("author_verify_evidence") or {},
        }
    }


async def collect_author_profiles(
    crawler: Any,
    authors: list[dict[str, Any]],
    *,
    progress: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """按冻结顺序取作品。运行级错误立刻返回已完成的观测，不继续后面的作者。"""

    observations: list[dict[str, Any]] = []
    unrecovered: list[dict[str, str]] = []
    semaphore = asyncio.Semaphore(1)
    consecutive_empty = 0
    for author in validate_author_works(authors):
        if progress is not None:
            progress()
        author_id = author["author_platform_id"]
        failure = "empty_detail"
        chosen: dict[str, Any] | None = None
        for work in author["works"]:
            try:
                outcome = await observe_work(crawler, author_id, work["aweme_id"], semaphore)
            except Exception as exc:
                code = str(getattr(exc, "code", "") or "")
                if code:
                    return _stopped(observations, unrecovered, code)
                return _stopped(observations, unrecovered, "browser_launch_failed")
            blocker = str(outcome.get("runtime_blocker") or "")
            if blocker:
                return _stopped(observations, unrecovered, blocker)
            observation = outcome.get("observation")
            if isinstance(observation, dict):
                chosen = observation
                break
            failure = str(outcome.get("reason") or failure)
        if chosen is None:
            unrecovered.append({"author_platform_id": author_id, "reason": failure})
            consecutive_empty = consecutive_empty + 1 if failure == "empty_detail" else 0
            if consecutive_empty >= CONSECUTIVE_EMPTY_DETAIL_STOP:
                return _stopped(observations, unrecovered, EMPTY_DETAIL_BLOCKER)
        else:
            observations.append(chosen)
            consecutive_empty = 0
    return {"observations": observations, "unrecovered": unrecovered, "runtime_blocker": ""}


def _stopped(
    observations: list[dict[str, Any]],
    unrecovered: list[dict[str, str]],
    blocker: str,
) -> dict[str, Any]:
    return {
        "observations": observations,
        "unrecovered": unrecovered,
        "runtime_blocker": blocker,
    }

