"""#127 第 2 项有意偏离：通用四平台不再向浏览器注入 stealth.min.js，包内资源随之删除。

固化预期（``tests/fixtures/t14_legacy_expectations/T05``、``T06``）、冻结源码快照与 manifest 不改、不用根实现
重新生成。根侧比较对加载的预期做“钉住旧值 → 单向删除”：先断言旧注入恰好出现在预期位置、恰为旧脚本的
sha256 与长度，再删除；其余部分仍整体 ``==`` 比较。

1. 微博（T05）：标准模式 ``new_context`` 之后的 ``("stealth", <脚本字节>)`` 行删除。
2. 抖音（T06）：标准模式 ``launch`` 之后的 ``("init_script", <脚本 sha256>)`` 行删除。

B站（T08）不需要变换：冻结旧执行器在 ``resources.path("js/stealth.min.js")`` 抛 ``FileNotFoundError`` 时本就跳过注入，
资源删除后旧侧与根侧轨迹同样没有 ``init_script`` 行，冻结源码照原样执行。
"""

from __future__ import annotations

from typing import Any

from support.legacy_expectations import BytesDigest

STEALTH_SHA256 = "02ae012addcdb30b0ed1a512406feb487699b1b217566e87a8086b54dfcc1d4d"
STEALTH_SIZE = 180462


def _drop_after(trace: list, *, kind: str, value: Any, after: str) -> list:
    """删除紧随 ``after`` 行的唯一一条注入行；次数、位置与脚本标识都必须与旧预期一致。"""
    indexes = [index for index, row in enumerate(trace) if row[0] == kind]
    assert len(indexes) == 1, indexes
    (index,) = indexes
    assert index > 0 and trace[index - 1][0] == after, trace[max(0, index - 1):index + 1]
    assert trace[index] == (kind, value), trace[index]
    return trace[:index] + trace[index + 1:]


def weibo(expected: dict[str, Any]) -> dict[str, Any]:
    """1：T05 drive 预期中 new_context 后恰一次 stealth 注入，字节摘要为旧脚本；删除该行。"""
    trace = _drop_after(
        expected["trace"], kind="stealth", value=BytesDigest(STEALTH_SHA256, STEALTH_SIZE), after="new_context",
    )
    result = {**expected, "trace": trace}
    assert "stealth" not in repr(result["trace"])
    return result


def douyin(expected: dict[str, Any]) -> dict[str, Any]:
    """2：T06 drive 预期中 launch 后恰一次 init_script，值为旧脚本 sha256；删除该行。"""
    trace = _drop_after(expected["trace"], kind="init_script", value=STEALTH_SHA256, after="launch")
    result = {**expected, "trace": trace}
    assert STEALTH_SHA256 not in repr(result["trace"])
    return result

