"""覆盖守护收集探针：仅在子进程 `--collect-only` 中以 `-p support.coverage_probe` 装载。

把每个已选中的测试项写成 JSON（nodeid、标记名、skip/xfail 标记、skipif 条件），
供 `tests/test_t13_coverage.py` 核对五站责任 node 是否落在声明的 lane 且不被跳过。
只用 pytest 公开的 item 接口，不修改收集结果。
"""

import json
import os
from pathlib import Path


PROBE_ENV = "TPC_COVERAGE_PROBE"


def _skipif_conditions(item):
    conditions = []
    for marker in item.iter_markers("skipif"):
        values = list(marker.args) or [marker.kwargs.get("condition")]
        # 字符串条件需要求值上下文，守护一律视为不可接受。
        conditions.extend(value if isinstance(value, bool) else "unsupported" for value in values)
    return conditions


def pytest_collection_finish(session):
    path = os.environ.get(PROBE_ENV)
    if not path:
        return
    items = [{
        "nodeid": item.nodeid,
        "markers": sorted({marker.name for marker in item.iter_markers()}),
        "skip": item.get_closest_marker("skip") is not None,
        "xfail": item.get_closest_marker("xfail") is not None,
        "skipif": _skipif_conditions(item),
    } for item in session.items]
    Path(path).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
