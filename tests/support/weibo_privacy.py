"""微博作者字段边界用例的窄装配：只经根实现 update_weibo_note 验证。"""

from dataclasses import replace
from types import SimpleNamespace

from support.weibo_adapter import crawler, settings


config = settings()


class Factory:
    @staticmethod
    def create_store():
        raise AssertionError("隐私用例必须显式装配 FakeStore")


async def update_weibo_note(note_item):
    instance = crawler(config)
    instance.ports = replace(instance.ports, store_factory=Factory.create_store)
    await instance.update_weibo_note(note_item)


wb = SimpleNamespace(WeibostoreFactory=Factory, update_weibo_note=update_weibo_note)


