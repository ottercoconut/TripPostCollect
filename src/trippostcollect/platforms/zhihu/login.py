# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/zhihu/login.py
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

import asyncio
import functools
import logging
import sys
from typing import Optional
from playwright.async_api import BrowserContext, Page
from tenacity import RetryError, retry, retry_if_result, stop_after_attempt, wait_fixed
from trippostcollect.application.contracts import ZhihuLoginPorts
from trippostcollect.runtime.cookies import convert_cookies, convert_str_cookie_to_dict

logger = logging.getLogger("MediaCrawler")

class ZhiHuLogin:

    def __init__(self,
                 login_type: str,
                 browser_context: BrowserContext,
                 context_page: Page,
                 login_phone: Optional[str] = "",
                 cookie_str: str = "",
                 *, ports: ZhihuLoginPorts
                 ):
        self.ports = ports
        self.login_type = login_type
        self.browser_context = browser_context
        self.context_page = context_page
        self.login_phone = login_phone
        self.cookie_str = cookie_str

    @retry(stop=stop_after_attempt(600), wait=wait_fixed(1), retry=retry_if_result(lambda value: value is False))
    async def check_login_state(self) -> bool:
        """
        Check if the current login status is successful and return True otherwise return False
        Returns:

        """
        current_cookie = await self.browser_context.cookies()
        _, cookie_dict = convert_cookies(current_cookie)
        current_web_session = cookie_dict.get("z_c0")
        if current_web_session:
            return True
        return False

    async def begin(self):
        """Start login zhihu"""
        logger.info("[ZhiHu.begin] Begin login zhihu ...")
        if self.login_type == "qrcode":
            await self.login_by_qrcode()
        elif self.login_type == "phone":
            await self.login_by_mobile()
        elif self.login_type == "cookie":
            await self.login_by_cookies()
        else:
            raise ValueError("[ZhiHu.begin]I nvalid Login Type Currently only supported qrcode or phone or cookies ...")

    async def login_by_mobile(self):
        """Login zhihu by mobile"""
        # todo implement login by mobile

    async def login_by_qrcode(self):
        """login zhihu website and keep webdriver login state"""
        logger.info("[ZhiHu.login_by_qrcode] Begin login zhihu by qrcode ...")
        qrcode_img_selector = "canvas.Qrcode-qrcode"
        # find login qrcode
        base64_qrcode_img = await self.ports.find_qrcode_img_from_canvas(
            self.context_page,
            canvas_selector=qrcode_img_selector
        )
        if not base64_qrcode_img:
            logger.info("[ZhiHu.login_by_qrcode] login failed , have not found qrcode please check ....")
            if not base64_qrcode_img:
                sys.exit()

        # show login qrcode
        # fix issue #12
        # we need to use partial function to call show_qrcode function and run in executor
        # then current asyncio event loop will not be blocked
        partial_show_qrcode = functools.partial(self.ports.show_qrcode, base64_qrcode_img)
        asyncio.get_running_loop().run_in_executor(executor=None, func=partial_show_qrcode)

        logger.info("[ZhiHu.login_by_qrcode] waiting for scan code login, remaining time is 120s")
        try:
            await self.check_login_state()

        except RetryError:
            logger.info("[ZhiHu.login_by_qrcode] Login zhihu failed by qrcode login method ...")
            sys.exit()

        wait_redirect_seconds = 5
        logger.info(
            f"[ZhiHu.login_by_qrcode] Login successful then wait for {wait_redirect_seconds} seconds redirect ...")
        await asyncio.sleep(wait_redirect_seconds)

    async def login_by_cookies(self):
        """login zhihu website by cookies"""
        logger.info("[ZhiHu.login_by_cookies] Begin login zhihu by cookie ...")
        for key, value in convert_str_cookie_to_dict(self.cookie_str).items():
            await self.browser_context.add_cookies([{
                'name': key,
                'value': value,
                'domain': ".zhihu.com",
                'path': "/"
            }])
