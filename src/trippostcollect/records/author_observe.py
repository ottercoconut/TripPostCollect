"""作者身份观察文件：逐作者原子落盘，不写正文、图片或发现状态。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from trippostcollect.records.identity import platform_nickname, platform_user_id
from trippostcollect.records.sanitization import sanitize_author_avatar_data


OBSERVATIONS_NAME = "author_identity_observations.json"
CONSECUTIVE_NOT_OBSERVED_LIMIT = 30
CONSECUTIVE_NOT_OBSERVED_STOP = "identity_consecutive_not_observed"


class AuthorIdentityBlocked(RuntimeError):
    """平台把这一次请求判定为运行级阻断。当前作者不记为不可达。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def observations_path(save_data_path: str | Path) -> Path:
    return Path(save_data_path) / OBSERVATIONS_NAME


def identity_targets_file() -> Path | None:
    """目标清单放在现有行为证据旁边，不新增环境变量。"""

    evidence = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    if not evidence:
        return None
    path = Path(evidence).with_name("author_identity_targets.json")
    return path if path.is_file() else None


def load_author_targets(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("author identity targets must be a JSON array")
    return [item for item in payload if isinstance(item, dict)]


def write_observations_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = sanitize_author_avatar_data(payload).value
    if not isinstance(cleaned, dict):
        raise RuntimeError("author identity observations must remain an object")
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def empty_observation_document(platform: str) -> dict[str, Any]:
    return {
        "platform": platform,
        "observations": [],
        "stopped_early": False,
        "stop_reason": "",
    }


def missed_identity(reason: str) -> dict[str, str]:
    fields = identity_fields()
    fields["not_observed_reason"] = reason
    return fields


def identity_fields(
    *,
    creator_hash: str = "",
    nickname: str = "",
    user_nickname: str = "",
    creator_url_token: str = "",
    author_profile_url: str = "",
) -> dict[str, str]:
    return {
        "creator_hash": platform_user_id(creator_hash),
        "nickname": platform_nickname(nickname) if nickname else "",
        "user_nickname": platform_nickname(user_nickname) if user_nickname else "",
        "creator_url_token": str(creator_url_token or "").strip(),
        "author_profile_url": str(author_profile_url or "").strip(),
    }


def observation_record(
    *,
    author_key: str,
    platform_post_id: str,
    attempted_post_ids: list[str],
    status: str,
    fields: dict[str, str],
) -> dict[str, Any]:
    return {
        "author_key": author_key,
        "platform_post_id": platform_post_id,
        "attempted_post_ids": list(attempted_post_ids),
        "status": status,
        **fields,
    }


async def collect_author_observations(
    *,
    platform: str,
    targets: list[dict[str, Any]],
    destination: Path,
    fetch_post: Callable[[dict[str, Any]], Awaitable[dict[str, str]]],
    sleep_seconds: float,
) -> None:
    """按作者顺序取帖。运行级阻断时保留已完成作者，不把当前作者写成 not_observed。"""

    document = empty_observation_document(platform)
    observations: list[dict[str, Any]] = document["observations"]
    write_observations_atomic(destination, document)
    consecutive_not_observed = 0
    for author in targets:
        author_key = str(author.get("author_key") or "")
        posts = [item for item in author.get("posts") or [] if isinstance(item, dict)]
        attempted: list[str] = []
        found: dict[str, str] | None = None
        found_post_id = ""
        last_fields = missed_identity("no_post")
        try:
            for post in posts:
                post_id = str(post.get("platform_post_id") or "").strip()
                if post_id:
                    attempted.append(post_id)
                fields = await fetch_post(post)
                last_fields = fields
                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)
                if fields.get("creator_hash") and (
                    fields.get("nickname") or fields.get("user_nickname")
                ):
                    found = fields
                    found_post_id = post_id
                    break
        except AuthorIdentityBlocked as exc:
            document["stopped_early"] = True
            document["stop_reason"] = exc.code
            write_observations_atomic(destination, document)
            raise
        observations.append(
            observation_record(
                author_key=author_key,
                platform_post_id=found_post_id or (attempted[-1] if attempted else ""),
                attempted_post_ids=attempted,
                status="observed" if found else "not_observed",
                fields=found or last_fields,
            )
        )
        if found:
            consecutive_not_observed = 0
        else:
            consecutive_not_observed += 1
            if consecutive_not_observed >= CONSECUTIVE_NOT_OBSERVED_LIMIT:
                del observations[-consecutive_not_observed:]
                document["stopped_early"] = True
                document["stop_reason"] = CONSECUTIVE_NOT_OBSERVED_STOP
                write_observations_atomic(destination, document)
                return
        write_observations_atomic(destination, document)
