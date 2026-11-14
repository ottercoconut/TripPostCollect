"""父进程构造 worker 子命令的离线场景：父侧入参与预期的 worker argv，全部写成字面值。

`parent_args` 是 `_run_platform_without_policy` 的入参；`worker_argv` 是父侧据此生成、交给
`python -P -m trippostcollect.platforms.entry` 的参数，也供 worker 侧装配与解析用例直接复用。
"""

from __future__ import annotations

import argparse
from pathlib import Path

ENTRY_MODULE = "trippostcollect.platforms.entry"
KEYWORD = "青岛旅游"

_PARENT_DEFAULTS = {
    "behavior_profile": "social_high_risk", "db": "<TMP>/content.sqlite", "discovery_job_id": None,
    "discovery_query_fingerprint": None, "discovery_run_id": None, "discovery_source_exhausted": False,
    "download_images": True, "headed": False, "keyword": KEYWORD, "login_type": "cookie",
    "no_checkpoint_write": False, "post_repair": False, "post_repair_detail_targets": [],
    "resume_identities_path": None, "resume_summary": None, "start_cursor": "", "start_offset": None,
    "start_page": 1, "timeout_per_platform": 180, "top_refresh_max_pages": 3, "xhs_account_id": "xhs-a01",
    "xhs_detail_urls": [], "xhs_discovery_query_fingerprint": "fp", "xhs_discovery_target_key": "target",
    "xhs_post_interaction": "none", "xhs_profile_dir": "<TMP>/xhs-profile", "xhs_repair": False,
    "xhs_repair_batch_size": 5, "zhihu_detail_urls": [],
}

# 场景名 → (父侧平台, 相对默认值改动的入参, worker 平台代码, 登录方式, 抓取类型, 有头, 起始页, 指定 ID, CDP 模式)
SCENARIOS = {
    "douyin_post_repair": ("douyin", {"post_repair": True, "post_repair_detail_targets": ["7000000000000000001"]},
                           "dy", "cookie", "detail", False, 1, "7000000000000000001", None),
    "douyin_search_discovery": ("douyin", {"discovery_job_id": 7, "discovery_query_fingerprint": "qfp",
                                           "discovery_run_id": "run-1", "headed": True, "start_cursor": "c1",
                                           "start_offset": 20, "start_page": 3},
                                "dy", "cookie", "search", True, 3, None, None),
    "weibo_post_repair": ("weibo", {"post_repair": True, "post_repair_detail_targets": ["5000000000000001"]},
                          "wb", "cookie", "detail", False, 1, "5000000000000001", None),
    "weibo_search": ("weibo", {}, "wb", "cookie", "search", False, 1, None, None),
    "xhs_repair": ("xhs", {"xhs_detail_urls": ["https://www.xiaohongshu.com/explore/abc"], "xhs_repair": True},
                   "xhs", "cookie", "detail", False, 1, "https://www.xiaohongshu.com/explore/abc", "true"),
    "xhs_search_qrcode": ("xhs", {"headed": True, "login_type": "qrcode", "resume_summary": "<TMP>/resume.json"},
                          "xhs", "qrcode", "search", True, 1, None, "true"),
    "zhihu_detail": ("zhihu", {"zhihu_detail_urls": ["https://www.zhihu.com/question/1/answer/2"]},
                     "zhihu", "cookie", "detail", False, 1, "https://www.zhihu.com/question/1/answer/2", "true"),
    "zhihu_search": ("zhihu", {}, "zhihu", "cookie", "search", False, 1, None, "true"),
}
NAMES = sorted(SCENARIOS)


def _tmp(value, tmp: Path):
    return value.replace("<TMP>", str(tmp)) if isinstance(value, str) else value


def platform(name: str) -> str:
    return SCENARIOS[name][0]


def worker_code(name: str) -> str:
    return SCENARIOS[name][2]


def parent_args(name: str, tmp: Path) -> argparse.Namespace:
    overrides = SCENARIOS[name][1]
    return argparse.Namespace(**{key: _tmp(value, tmp) for key, value in {**_PARENT_DEFAULTS, **overrides}.items()})


def worker_argv(name: str, tmp: Path) -> list[str]:
    parent, _, code, login, crawler_type, headed, start, specified, cdp = SCENARIOS[name]
    argv = [
        "--platform", code, "--lt", login, "--type", crawler_type, "--keywords", KEYWORD,
        "--get_comment", "false", "--get_sub_comment", "false", "--get_media", "true",
        "--headless", "false" if headed else "true", "--save_data_option", "jsonl",
        "--save_data_path", f"{tmp}/{name}/{parent}/data", "--start", str(start),
        "--max_concurrency_num", "1", "--enable_ip_proxy", "false",
    ]
    if specified:
        argv += ["--specified_id", specified]
    if cdp:
        argv += ["--enable_cdp_mode", cdp]
    return argv
