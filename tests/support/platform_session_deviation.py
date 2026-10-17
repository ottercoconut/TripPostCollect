"""#59 有意偏离：登录资料改存 data/runtime/platform_sessions，worker 不再回落 fork 数据目录。

T14-A（PR #59）在固化预期生成之后改变了两处根实现行为：

1. 抖音持久 profile 从 fork 固定位置 ``tools/MediaCrawler/browser_data/dy_user_data_dir`` 迁到
   ``trippostcollect.core.paths.platform_profile_dir("douyin")``；
2. 根暂存 stager 缺省暂存根不再回落 ``tools/MediaCrawler/data``，``entry._save_data_root("")`` 抛
   ``RuntimeError("worker_save_data_path_required")``。

固化文件与 manifest 不改、不用根实现重新生成。根侧比较对加载的预期做“钉住旧值 → 单向替换为新值”的
变换：先断言预期中确实是被偏离的旧值（且只出现在应出现的位置），再换成调用时求值的新值，其余部分
仍按原对照整体 ``==`` 比较。drive 记录原始 ``user_data_dir``，根实现若仍用旧路径会比较失败。
"""

from __future__ import annotations

from typing import Any

from support import legacy_expectations as expectations


LEGACY_DOUYIN_PROFILE = "<ROOT>/tools/MediaCrawler/browser_data/dy_user_data_dir"
LEGACY_DEFAULT_SAVE_DATA_ROOT = "<ROOT>/tools/MediaCrawler/data"
SAVE_DATA_PATH_REQUIRED = "worker_save_data_path_required"


def douyin_profile(expected: dict[str, Any], current_profile: str) -> dict[str, Any]:
    """T06 drive 预期：每条 ("launch", kwargs) 的 user_data_dir 恰为旧 fork 位置，且旧值在整个预期中
    出现次数等于 launch 次数；之后只把这些位置替换为 ``current_profile``（已按根侧同样规则 scrub）。"""
    launches = [row for row in expected["trace"] if row[0] == "launch"]
    for row in launches:
        assert row[1]["user_data_dir"] == LEGACY_DOUYIN_PROFILE, row
    assert expectations.dumps(expected).count(LEGACY_DOUYIN_PROFILE) == len(launches)
    trace = [("launch", {**row[1], "user_data_dir": current_profile}) if row[0] == "launch" else row
             for row in expected["trace"]]
    return {**expected, "trace": trace}
