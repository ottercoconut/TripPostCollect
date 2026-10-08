"""#52 有意偏离：根实现的作者页取数改为运行时投影优先、严格静态解析回退。

冻结旧实现在作者辅助页的到达与滚动检查之后直接 ``page.content()`` 并静态解析。根实现在同一位置
改为有界等待 domcontentloaded、执行固定投影脚本读取页面已执行的作者状态，取不到才回退
``page.content()``，并追加一次 ``creator_profile_parse`` 导航诊断。对照测试只把根实现的
``_read_creator_profile_from_page`` 换回旧的两行取数，其余请求、页面事件、产物与事件仍逐字节比较；
新取数路径由 ``tests/platforms/xhs/test_xhs_creator_runtime_profile.py`` 单独覆盖。
"""


def use_legacy_creator_page_read(patch, author_module) -> None:
    """只替换作者页取数这一处，不改冻结 fixture 字节。"""
    mixin = author_module.XhsAuthorMixin
    assert hasattr(mixin, "_read_creator_profile_from_page"), mixin

    async def legacy_read(self, page, user_id):
        html_content = await page.content()
        return self.xhs_client.extract_creator_info_from_html(html_content), ""

    patch.setattr(mixin, "_read_creator_profile_from_page", legacy_read)
