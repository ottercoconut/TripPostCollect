"""#52 有意偏离：根实现的作者页取数改为运行时投影优先、严格静态解析回退。

冻结旧实现在作者辅助页的到达与滚动检查之后、以及人工验证完成后的每轮轮询中，都直接
``page.content()`` 并静态解析。根实现在这两处分别改为：

- ``_read_creator_profile_from_page``：有界等待 domcontentloaded，执行固定投影脚本读取页面已执行
  的作者状态，每次接受前先做可见状态检查，取不到才回退 ``page.content()``，并追加一次
  ``creator_profile_parse`` 导航诊断；
- ``_read_creator_profile_after_verification``：验证完成后“读取一次”（先投影、再静态解析），并在
  接受前复查生命周期、可见状态、人工流程标记与 ticket 预算。

对照测试只把这两个方法换回旧的两行取数，其余请求、页面事件、产物与事件仍逐字节比较；
新取数路径由 ``tests/platforms/xhs/test_xhs_creator_runtime_profile.py`` 单独覆盖。
"""


def use_legacy_creator_page_read(patch, author_module) -> None:
    """只替换作者页取数这两处，不改冻结 fixture 字节。"""
    mixin = author_module.XhsAuthorMixin
    for name in ("_read_creator_profile_from_page", "_read_creator_profile_after_verification"):
        assert hasattr(mixin, name), name

    async def legacy_read(self, page, user_id):
        html_content = await page.content()
        return self.xhs_client.extract_creator_info_from_html(html_content), ""

    async def legacy_read_after_verification(self, page, user_id, ticket):
        creator_info, _reason = await legacy_read(self, page, user_id)
        return ("accept" if creator_info else "wait"), creator_info, ""

    patch.setattr(mixin, "_read_creator_profile_from_page", legacy_read)
    patch.setattr(mixin, "_read_creator_profile_after_verification", legacy_read_after_verification)
