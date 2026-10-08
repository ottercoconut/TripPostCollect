"""#49 有意偏离：冻结旧实现与根实现的对照只允许作者身份转换不同。

冻结旧实现把作者用户 ID 哈希为 creator_hash、把昵称脱敏；根实现改为保存平台原始值。
对照测试在已装载的旧模块上把两个转换函数换成根的原值归一，其余请求、产物与事件仍逐字节
比较。小红书旧 store 还按 TRIPPOSTCOLLECT_XHS_KEEP_AUTHOR_DETAIL 决定是否写出原始
user_id/昵称；对照时按正式 worker 的取值（"1"）设置。
"""

from trippostcollect.records.identity import platform_nickname, platform_user_id


XHS_KEEP_AUTHOR_DETAIL_ENV = "TRIPPOSTCOLLECT_XHS_KEEP_AUTHOR_DETAIL"


def use_raw_author_identity(patch, *modules) -> None:
    """只替换旧模块全局里的身份转换名，不改冻结 fixture 字节。"""
    for module in modules:
        assert hasattr(module, "anonymize_user_id") and hasattr(module, "mask_nickname"), module
        patch.setattr(module, "anonymize_user_id", platform_user_id)
        patch.setattr(module, "mask_nickname", platform_nickname)
