# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/main.py
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

# TripPostCollect：改为根解释器入口，仅延迟装配所选站点。
"""根解释器运行的选站 worker 入口。"""

import io
import os
import sys
from importlib import import_module

from trippostcollect.application.worker_inputs import WorkerInputs, apply_to_config, parse_cmd
from trippostcollect.platforms import _fork_bridge
from trippostcollect.runtime import worker


_weibo_post_repair = False
_douyin_browser_detail_fallback = False


def configure(argv) -> WorkerInputs:
    # 保留上游 UTF-8 包装，避免非 UTF-8 终端输出中文失败。
    if sys.stdout and hasattr(sys.stdout, 'buffer'):
        if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if sys.stderr and hasattr(sys.stderr, 'buffer'):
        if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    _fork_bridge.install()
    inputs = parse_cmd(argv)
    config = import_module("config")
    apply_to_config(inputs, config)
    return inputs


def install_hooks() -> None:
    global _weibo_post_repair, _douyin_browser_detail_fallback

    from mediacrawler_export_entrypoint import (
        install_xhs_repair_resilience,
    )
    from trippostcollect.application.worker_inputs import (
        douyin_browser_detail_fallback_reader,
        weibo_input_readers,
    )

    from trippostcollect.application.events import configure_batch_checkpoint
    from trippostcollect.xhs.batch_checkpoint import ENABLED_ENV, publish_batch

    configure_batch_checkpoint(publish_batch if os.environ.get(ENABLED_ENV) == "1" else None)
    install_xhs_repair_resilience()
    # 原 hook 在这里读取修复开关；新客户端只接收该布尔值，不修改 fork 类。
    _weibo_post_repair = weibo_input_readers().post_repair()
    _douyin_browser_detail_fallback = douyin_browser_detail_fallback_reader()()


def weibo_dependencies(config, *, post_repair=False):
    """微博专属装配；配置快照、正式写出与环境读取均经端口提供。"""
    import logging
    from dataclasses import fields
    from pathlib import Path

    from trippostcollect.application.candidates import AdaptiveAccumulator
    from trippostcollect.application.contracts import WeiboClientPorts, WeiboLoginPorts, WeiboPorts
    from trippostcollect.application.events import append_worker_execution_event
    from trippostcollect.application.worker_inputs import weibo_input_readers
    from trippostcollect.artifacts.image_staging import PostImageStager
    from trippostcollect.artifacts.jsonl import AsyncFileWriter, JsonlContentStore
    from trippostcollect.core.paths import MEDIACRAWLER_DIR
    from trippostcollect.db.discovery_read import existing_platform_identities
    from trippostcollect.platforms.weibo.models import WeiboConfig
    from trippostcollect.platforms.weibo.parser import weibo_source_asset_key
    from trippostcollect.runtime import cookies, helpers, http, image_retry, login_helpers
    from trippostcollect.runtime.browser import CDPBrowserManager, CDPBrowserSettings
    from trippostcollect.runtime.behavior import project_browser_args
    # 共享行为桥含尚未迁站的条件调用与 env 读点，保持原位至 T09/T10。
    from tools.trippostcollect_behavior import run_required_human_behavior

    values = {field.name: getattr(config, field.name) for field in fields(WeiboConfig)}
    values["WEIBO_SPECIFIED_ID_LIST"] = tuple(values["WEIBO_SPECIFIED_ID_LIST"])
    options = WeiboConfig(**values)
    browser_settings = CDPBrowserSettings(**{
        field.name: getattr(config, field.name) for field in fields(CDPBrowserSettings)
    })
    readers = weibo_input_readers()
    logger = logging.getLogger("MediaCrawler")

    def accumulator():
        return AdaptiveAccumulator(
            platform="weibo", stagnation_basis="candidate_identity",
            existing_identities=existing_platform_identities("weibo", **readers.identity_scope()),
            event_sink=append_worker_execution_event,
        )

    return options, WeiboPorts(
        client=WeiboClientPorts(
            make_async_client=lambda **kwargs: http.make_async_client(
                disable_ssl_verify=getattr(config, "DISABLE_SSL_VERIFY", False), **kwargs,
            ),
            convert_browser_context_cookies=cookies.convert_browser_context_cookies,
            image_error=image_retry.ImageDownloadFetchError,
            classified_http_image_error=image_retry.classified_http_image_error,
            image_max_bytes=image_retry.IMAGE_DOWNLOAD_MAX_BYTES,
            detail_timeout=readers.detail_timeout,
        ),
        login=WeiboLoginPorts(
            convert_cookies=cookies.convert_cookies,
            convert_str_cookie_to_dict=cookies.convert_str_cookie_to_dict,
            find_login_qrcode=login_helpers.find_login_qrcode,
            show_qrcode=login_helpers.show_qrcode,
        ),
        accumulator=accumulator,
        refresh_max_pages=readers.refresh_max_pages,
        source_exhausted=readers.source_exhausted,
        post_repair=post_repair,
        store_factory=lambda: JsonlContentStore(AsyncFileWriter(
            platform="weibo", crawler_type=options.CRAWLER_TYPE,
            save_data_path=lambda: options.SAVE_DATA_PATH,
        )),
        image_stager=lambda: PostImageStager(
            save_data_root=Path(options.SAVE_DATA_PATH) if options.SAVE_DATA_PATH else MEDIACRAWLER_DIR / "data",
            platform="weibo", source_key="image_list",
            source_asset_key=lambda item: weibo_source_asset_key(item.get("pid"), item["url"]),
            log_saved=lambda count, note_id: logger.info(
                f"[WeiboImageStoreImplement.store_post_images] saved {count} body images for note {note_id}"
            ),
        ),
        current_timestamp=helpers.get_current_timestamp,
        fetch_image_bytes_with_retry=image_retry.fetch_image_bytes_with_retry,
        image_error=image_retry.ImageDownloadFetchError,
        is_runtime_blocking_image_error=image_retry.is_runtime_blocking_image_error,
        browser_manager=lambda: CDPBrowserManager(browser_settings, project_browser_args=project_browser_args),
        project_browser_args=project_browser_args,
        run_required_human_behavior=run_required_human_behavior,
    )


def load_crawler(code: str) -> type:
    if code == "wb":
        from trippostcollect.platforms.weibo.core import bind_weibo_crawler
        return bind_weibo_crawler(
            import_module("config"),
            lambda config: weibo_dependencies(config, post_repair=_weibo_post_repair),
        )
    if code == "dy":
        worker.init_loging_config()
        from trippostcollect.platforms.douyin.core import DouYinCrawler
        return DouYinCrawler.bind(lambda: douyin_dependencies(import_module("config")))
    if code == "zhihu":
        from media_platform.zhihu import ZhihuCrawler
        return ZhihuCrawler
    if code == "xhs":
        from media_platform.xhs import XiaoHongShuCrawler
        return XiaoHongShuCrawler
    raise ValueError(f"不支持的 worker 平台：{code}")


def douyin_dependencies(config):
    """仅选中抖音时装配；正式写出不经过 fork store 工厂。"""
    from dataclasses import fields
    from functools import partial
    import logging
    from pathlib import Path
    import random
    import time

    from playwright.async_api import async_playwright
    from trippostcollect.application.contracts import (
        DouyinSettings, DouyinClientPorts, DouyinLoginPorts, DouyinCrawlerPorts,
    )
    from trippostcollect.application.candidates import AdaptiveAccumulator
    from trippostcollect.application.events import append_worker_execution_event as append_execution_event
    from trippostcollect.application.worker_inputs import douyin_readers, _enabled
    from trippostcollect.artifacts.evidence import write_evidence
    from trippostcollect.artifacts.image_staging import PostImageStager
    from trippostcollect.artifacts.jsonl import AsyncFileWriter, JsonlContentStore
    from trippostcollect.core.paths import MEDIACRAWLER_DIR
    from trippostcollect.db.discovery_read import existing_platform_identities
    from trippostcollect.platforms.douyin.parser import douyin_source_asset_key
    from trippostcollect.runtime import behavior, login_helpers
    from trippostcollect.runtime.browser import CDPBrowserManager, CDPBrowserSettings
    from trippostcollect.runtime.cookies import convert_browser_context_cookies
    from trippostcollect.runtime.helpers import get_user_agent
    from trippostcollect.runtime.http import make_async_client
    from trippostcollect.runtime.image_retry import fetch_image_bytes_with_retry

    values = {field.name: getattr(config, field.name) for field in fields(DouyinSettings)}
    values["DY_SPECIFIED_ID_LIST"] = tuple(values["DY_SPECIFIED_ID_LIST"])
    settings = DouyinSettings(**values)
    browser_settings = CDPBrowserSettings(**{
        field.name: getattr(config, field.name) for field in fields(CDPBrowserSettings)
    })
    client_factory = partial(make_async_client, disable_ssl_verify=settings.DISABLE_SSL_VERIFY)

    def candidates():
        identities = existing_platform_identities(
            "douyin", db_path=os.environ.get("TRIPPOSTCOLLECT_DB_PATH", ""),
            xhs_target_key=os.environ.get("TRIPPOSTCOLLECT_XHS_DISCOVERY_TARGET_KEY", ""),
            xhs_account_id=os.environ.get("TRIPPOSTCOLLECT_XHS_ACCOUNT_ID", ""),
            xhs_fingerprint=os.environ.get("TRIPPOSTCOLLECT_XHS_DISCOVERY_QUERY_FINGERPRINT", ""),
            job_id=os.environ.get("TRIPPOSTCOLLECT_DISCOVERY_JOB_ID", ""),
            fingerprint=os.environ.get("TRIPPOSTCOLLECT_DISCOVERY_QUERY_FINGERPRINT", ""),
            resume_identities_path=os.environ.get("TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH", ""),
        )
        accumulator = AdaptiveAccumulator.for_platform("douyin", existing_identities=identities)
        accumulator.event_sink = append_execution_event
        return accumulator

    async def run_behavior(page, platform_key):
        if not _enabled():
            return {"status": "disabled", "platform": platform_key}
        scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
        evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
        profile_name = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE", "social_high_risk").strip()
        if not scripts_dir.is_dir() or not evidence_path:
            raise RuntimeError("required TripPostCollect human behavior configuration is incomplete")
        return await behavior.run_required_human_behavior(
            page, platform_key=platform_key, evidence_path=evidence_path,
            profile_name=profile_name, xhs_search_ready=None, write_evidence=write_evidence,
        )

    def content_sink(crawler_type):
        return JsonlContentStore(AsyncFileWriter(
            platform="douyin", crawler_type=crawler_type,
            save_data_path=lambda: settings.SAVE_DATA_PATH,
        ), writer_attribute="file_writer")

    def image_stager():
        return PostImageStager(
            save_data_root=Path(settings.SAVE_DATA_PATH) if settings.SAVE_DATA_PATH else MEDIACRAWLER_DIR / "data",
            platform="douyin", source_key="note_download_url",
            source_asset_key=lambda item: douyin_source_asset_key(item.get("uri"), item["url"]),
            log_saved=lambda count, aweme_id: logging.getLogger("MediaCrawler").info(
                f"[DouYinImage.store_post_images] saved {count} body images for aweme {aweme_id}"
            ),
        )

    return {
        "settings": settings,
        "inputs": douyin_readers(settings.START_PAGE),
        "ports": DouyinCrawlerPorts(
            client=DouyinClientPorts(client_factory, convert_browser_context_cookies, random.random),
            login=DouyinLoginPorts(partial(
                login_helpers.find_login_qrcode, make_async_client=client_factory,
                get_user_agent=get_user_agent,
            ), login_helpers.show_qrcode),
            browser_detail_fallback=_douyin_browser_detail_fallback,
            async_playwright=async_playwright,
            cdp_manager=partial(CDPBrowserManager, browser_settings, project_browser_args=behavior.project_browser_args),
            project_browser_args=behavior.project_browser_args,
            run_required_human_behavior=run_behavior,
            candidates=candidates, append_execution_event=append_execution_event,
            current_timestamp=lambda: int(time.time() * 1000),
            content_sink=content_sink, image_stager=image_stager,
            fetch_image_bytes_with_retry=fetch_image_bytes_with_retry,
        ),
    }


def main(argv=None) -> int:
    inputs = configure(argv)
    install_hooks()
    state = {"crawler": None}

    async def app_main():
        crawler = load_crawler(inputs.platform)()
        state["crawler"] = crawler
        await crawler.start()

    async def app_cleanup():
        await worker.async_cleanup(state["crawler"], inputs.platform)

    worker.run(
        app_main, app_cleanup,
        cleanup_timeout_seconds=15.0,
        on_first_interrupt=lambda: worker.force_stop(state["crawler"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
