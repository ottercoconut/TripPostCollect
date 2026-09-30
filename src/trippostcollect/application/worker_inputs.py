# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/cmd_arg/arg.py
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

# TripPostCollect：以 argparse 收窄为父侧输入，并迁入 tools/utils.py 的 str2bool。
"""私有 worker 输入；将上游解析收窄为父侧实际生成的参数。"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping, Iterable, Optional, Sequence, Type, TypeVar


class PlatformEnum(str, Enum):
    XHS = "xhs"
    DOUYIN = "dy"
    WEIBO = "wb"
    ZHIHU = "zhihu"


class LoginTypeEnum(str, Enum):
    QRCODE = "qrcode"
    PHONE = "phone"
    COOKIE = "cookie"


class CrawlerTypeEnum(str, Enum):
    SEARCH = "search"
    DETAIL = "detail"


class SaveDataOptionEnum(str, Enum):
    JSONL = "jsonl"


class InitDbOptionEnum(str, Enum):
    """来自上游初始化枚举；建表属退出切片，已无存活成员。"""


EnumT = TypeVar("EnumT", bound=Enum)


def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def _to_bool(value: bool | str) -> bool:
    if isinstance(value, bool):
        return value
    return str2bool(value)


def _coerce_enum(enum_cls: Type[EnumT], value: EnumT | str, default: EnumT) -> EnumT:
    """保留上游配置枚举收窄辅助；CLI 非法值由 argparse 拒绝。"""
    try:
        return enum_cls(value)
    except ValueError:
        return default


def _normalize_argv(argv: Optional[Sequence[str]]) -> Iterable[str]:
    if argv is None:
        return list(sys.argv[1:])
    return list(argv)


@dataclass(frozen=True)
class WorkerInputs:
    platform: str
    lt: str
    type: str
    keywords: str
    get_comment: bool
    get_sub_comment: bool
    get_media: bool
    headless: bool
    enable_cdp_mode: bool
    save_data_option: str
    save_data_path: str
    start: int
    max_concurrency_num: int
    enable_ip_proxy: bool
    specified_id: str


def parse_cmd(argv: Sequence[str] | None = None) -> WorkerInputs:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--platform", choices=[e.value for e in PlatformEnum], required=True)
    parser.add_argument("--lt", choices=[e.value for e in LoginTypeEnum], required=True)
    parser.add_argument("--type", choices=[e.value for e in CrawlerTypeEnum], required=True)
    parser.add_argument("--keywords", required=True)
    parser.add_argument("--get_comment", type=_to_bool, required=True)
    parser.add_argument("--get_sub_comment", type=_to_bool, required=True)
    parser.add_argument("--get_media", type=_to_bool, required=True)
    parser.add_argument("--headless", type=_to_bool, required=True)
    parser.add_argument("--enable_cdp_mode", type=_to_bool, default=False)
    parser.add_argument("--save_data_option", choices=[e.value for e in SaveDataOptionEnum], required=True)
    parser.add_argument("--save_data_path", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--max_concurrency_num", type=int, required=True)
    parser.add_argument("--enable_ip_proxy", type=_to_bool, required=True)
    parser.add_argument("--specified_id", default="")
    return WorkerInputs(**vars(parser.parse_args(_normalize_argv(argv))))


def apply_to_config(inputs: WorkerInputs, config_module) -> None:
    """按旧解析器写回配置；未选平台的 ID 与退出参数保留当前值。"""
    fields = {
        "PLATFORM": "platform", "LOGIN_TYPE": "lt", "CRAWLER_TYPE": "type",
        "START_PAGE": "start", "KEYWORDS": "keywords",
        "ENABLE_GET_COMMENTS": "get_comment", "ENABLE_GET_SUB_COMMENTS": "get_sub_comment",
        "ENABLE_GET_MEIDAS": "get_media", "HEADLESS": "headless", "CDP_HEADLESS": "headless",
        "ENABLE_CDP_MODE": "enable_cdp_mode", "SAVE_DATA_OPTION": "save_data_option",
        "MAX_CONCURRENCY_NUM": "max_concurrency_num", "SAVE_DATA_PATH": "save_data_path",
        "ENABLE_IP_PROXY": "enable_ip_proxy",
    }
    for key, field in fields.items():
        setattr(config_module, key, getattr(inputs, field))
    for key in (
        "COOKIES", "CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES", "CRAWLER_MAX_NOTES_COUNT",
        "IP_PROXY_POOL_COUNT", "IP_PROXY_PROVIDER_NAME", "STATIC_PROXY_URL",
    ):
        setattr(config_module, key, getattr(config_module, key))
    specified_id = inputs.specified_id
    specified_id_list = [id.strip() for id in specified_id.split(",") if id.strip()] if specified_id else []
    if specified_id_list:
        field = {
            "xhs": "XHS_SPECIFIED_NOTE_URL_LIST", "dy": "DY_SPECIFIED_ID_LIST",
            "wb": "WEIBO_SPECIFIED_ID_LIST", "zhihu": "ZHIHU_SPECIFIED_ID_LIST",
        }[inputs.platform]
        setattr(config_module, field, specified_id_list)


def env_int_reader(
    name: str, default: int, *, environ: Mapping[str, str] = os.environ,
) -> Callable[[], int]:
    """绑定输入映射，调用时解析非负整数；不提前缓存操作起点的值。"""
    def read() -> int:
        try:
            return max(0, int(environ.get(name, default)))
        except (TypeError, ValueError):
            return max(0, default)

    return read


def env_int(name: str, default: int, *, environ: Mapping[str, str]) -> int:
    """供旧 fork 单条委托使用，读取与原调用发生在同一时点。"""
    return env_int_reader(name, default, environ=environ)()


def _enabled() -> bool:
    return os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_ENABLED", "").strip() == "1"


def weibo_input_readers(*, environ: Mapping[str, str] = os.environ):
    """绑定微博原输入映射，各 reader 只在原操作起点读取。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        post_repair=lambda: environ.get("TRIPPOSTCOLLECT_POST_REPAIR") == "1",
        detail_timeout=lambda: max(
            5_000, int(environ.get("TRIPPOSTCOLLECT_WEIBO_BROWSER_DETAIL_TIMEOUT_MS", "30000")),
        ),
        refresh_max_pages=env_int_reader(
            "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", 0, environ=environ,
        ),
        source_exhausted=lambda: environ.get("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED") == "1",
        identity_scope=lambda: dict(
            db_path=environ.get("TRIPPOSTCOLLECT_DB_PATH", ""),
            xhs_target_key=environ.get("TRIPPOSTCOLLECT_XHS_DISCOVERY_TARGET_KEY", ""),
            xhs_account_id=environ.get("TRIPPOSTCOLLECT_XHS_ACCOUNT_ID", ""),
            xhs_fingerprint=environ.get("TRIPPOSTCOLLECT_XHS_DISCOVERY_QUERY_FINGERPRINT", ""),
            job_id=environ.get("TRIPPOSTCOLLECT_DISCOVERY_JOB_ID", ""),
            fingerprint=environ.get("TRIPPOSTCOLLECT_DISCOVERY_QUERY_FINGERPRINT", ""),
            resume_identities_path=environ.get("TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH", ""),
        ),
    )


def douyin_browser_detail_fallback_reader(*, environ=os.environ):
    """在旧 install_hooks 时点读取修复开关，不随详情请求重新取值。"""
    return lambda: environ.get("TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_FALLBACK") == "1"


def douyin_readers(start_page: int, *, environ=os.environ):
    """保留抖音每个 env 的默认值、转换及读取时点。"""
    from trippostcollect.application.contracts import DouyinReaders

    return DouyinReaders(
        refresh_max_pages=env_int_reader("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", 0, environ=environ),
        source_exhausted=lambda: environ.get("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED"),
        resume_offset=env_int_reader("TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET", max(0, (start_page - 1) * 10), environ=environ),
        resume_cursor=lambda: environ.get("TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR", ""),
        enrich_creators=lambda: environ.get("TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS"),
        enrich_only_images=lambda: environ.get("TRIPPOSTCOLLECT_DOUYIN_ENRICH_ONLY_IMAGES", "1"),
        max_creator_enrich=lambda: environ.get("TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH", "30"),
        creator_sleep_seconds=lambda: environ.get("TRIPPOSTCOLLECT_DOUYIN_CREATOR_SLEEP_SECONDS", "0.25"),
        browser_detail_timeout=lambda: environ.get("TRIPPOSTCOLLECT_DOUYIN_BROWSER_DETAIL_TIMEOUT_MS", "30000"),
    )
