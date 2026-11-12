"""#127 有意偏离：知乎 client 在一次运行内复用同一个 ``httpx.AsyncClient``，工厂只调用一次。

固化预期（``tests/fixtures/t14_legacy_expectations/T07``）与 manifest 不改、不用根实现重新生成。
旧实现每次请求新建 client，固化轨迹在每条 ``request``/``image`` 前都有一条 ``http_client``。根侧比较
对加载的预期做“钉住旧值 → 单向替换”：先断言旧轨迹里工厂调用与请求一一对应、参数都是同一组，再只保留
首条；``request``/``image`` 的内容与次序不动，其余部分仍整体 ``==`` 比较。微博、抖音的 drive 工厂不进入
轨迹，不受影响。
"""

from __future__ import annotations

from typing import Any

FACTORY_KWARGS = {"proxy": None}


def zhihu(expected: dict[str, Any]) -> dict[str, Any]:
    trace = []
    factories = 0
    requests = 0
    for row in expected["trace"]:
        if row[0] == "http_client":
            assert row[1] == FACTORY_KWARGS, row
            factories += 1
            if factories > 1:
                continue
        elif row[0] in {"request", "image"}:
            assert factories > requests, row
            requests += 1
        trace.append(row)
    assert factories == requests, (factories, requests)
    return {**expected, "trace": trace}
