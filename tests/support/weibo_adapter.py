"""微博离线测试装配，不调用生产 worker。"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

from trippostcollect.application.worker_inputs import worker_config
from trippostcollect.platforms import entry
from trippostcollect.platforms.weibo import core, models


ROOT = Path(__file__).resolve().parents[2]


def settings(**overrides):
    """只复制静态配置；所有输出由测试的临时目录覆盖。

    默认值取根配置对象 worker_config()。
    """
    from trippostcollect.runtime.browser import CDPBrowserSettings

    config = worker_config()

    keys = {field.name for cls in (models.WeiboConfig, CDPBrowserSettings) for field in fields(cls)}
    result = SimpleNamespace(**{key: getattr(config, key) for key in keys})
    result.CRAWLER_MAX_SLEEP_SEC = 0
    result.SAVE_DATA_OPTION = "jsonl"
    result.CRAWLER_TYPE = "search"
    result.PLATFORM = "wb"
    result.KEYWORDS = "青岛旅游"
    result.COOKIES = ""
    result.__dict__.update(overrides)
    return result


def crawler(config):
    return core.WeiboCrawler(*entry.weibo_dependencies(config))


def client_ports(**overrides):
    from dataclasses import replace
    return replace(entry.weibo_dependencies(settings())[1].client, **overrides)

