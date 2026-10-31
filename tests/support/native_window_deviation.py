"""#139 有意偏离：通用平台持久 context 不再用 viewport 做设备模拟，与小红书一样按原生窗口启动。

#136 的共享启动配置（``support.shared_launch_deviation``）带 ``viewport=PROFILE_VIEWPORT``（1280×900），Playwright
据此模拟设备：页面读到 ``screen`` 1280×900、DPR 1，且 ``outerHeight`` 小于 ``innerHeight``。现改为不传
``viewport``、传 ``no_viewport=True``，启动参数末尾追加 ``--window-size=1450,900``（``NATIVE_WINDOW_SIZE``，
与小红书 ``XHS_NATIVE_WINDOW_SIZE`` 相同），``screen``、DPR 与窗口尺寸由浏览器按真实值给出。

固化预期与冻结源码快照不改、不用根实现重新生成。根侧比较在 #136 变换之后再做一步“钉住 #136 值 → 单向替换”：

1. 抖音（T06）、B站（T08）：#136 替换后的唯一 ``launch`` 行 kwargs 恰有 ``viewport`` 1280×900、没有
   ``no_viewport`` 与 ``--window-size``，换成原生窗口配置。
2. warmup（T10）：冻结 ``launch_login_context`` 在 #136 取值点替换后，``viewport`` 行换成 ``no_viewport``、
   ``args`` 末尾追加 ``--window-size``、删除 ``if native_window_size:`` 分支，再与共享函数逐语句比较；
   冻结 warmup 执行轨迹中的每条 ``launch`` 行按第 1 项替换。
"""

from __future__ import annotations

import copy
from typing import Any

PROFILE_VIEWPORT = {"width": 1280, "height": 900}
NATIVE_WINDOW_ARG = "--window-size=1450,900"


def launch_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """钉住 #136 共享配置的 1280×900 视口，换成 ``no_viewport=True`` 并在启动参数末尾追加原生窗口尺寸。"""
    assert kwargs["viewport"] == PROFILE_VIEWPORT, kwargs
    assert "no_viewport" not in kwargs, kwargs
    assert not [arg for arg in kwargs["args"] if arg.startswith("--window-size=")], kwargs["args"]
    replaced = {key: value for key, value in kwargs.items() if key != "viewport"}
    return {**replaced, "no_viewport": True, "args": [*kwargs["args"], NATIVE_WINDOW_ARG]}


def douyin(expected: dict[str, Any]) -> dict[str, Any]:
    """1：T06 drive 预期（已经 #136 变换）中恰一条 launch，替换其 kwargs。"""
    trace = list(expected["trace"])
    indexes = [index for index, row in enumerate(trace) if row[0] == "launch"]
    assert len(indexes) == 1, indexes
    (index,) = indexes
    trace[index] = ("launch", launch_kwargs(trace[index][1]))
    return {**expected, "trace": trace}


def bilibili(old: dict[str, Any]) -> dict[str, Any]:
    """1：T08 旧侧（已 JSON 归一并经 #136 变换）恰一条 launch，替换其 kwargs。"""
    value = copy.deepcopy(old)
    launches = [row for row in value["trace"] if row[0] == "launch"]
    assert len(launches) == 1, launches
    launches[0][1] = launch_kwargs(launches[0][1])
    return value


def warmup_trace(trace: list) -> list:
    """2：冻结 warmup 执行轨迹中每条 launch 行（至少一条）都按第 1 项替换，其余行不变。"""
    assert [row for row in trace if row[0] == "launch"], trace
    return [("launch", launch_kwargs(row[1])) if row[0] == "launch" else row for row in trace]


# 2. warmup（T10）：#136 取值点替换后的冻结 launch_login_context 源码中恰各一处。
T10_VIEWPORT_LINE = '        "viewport": PROFILE_VIEWPORT,\n'
T10_NO_VIEWPORT_LINE = '        "no_viewport": True,\n'
T10_ARGS_TAIL = "            *runtime_args,\n        ],\n"
T10_WINDOW_ARGS_TAIL = (
    "            *runtime_args,\n"
    '            f"--window-size={native_window_size[0]},{native_window_size[1]}",\n'
    "        ],\n"
)
T10_NATIVE_BRANCH = (
    "    if native_window_size:\n"
    "        width, height = native_window_size\n"
    '        kwargs.pop("viewport")\n'
    '        kwargs["no_viewport"] = True\n'
    '        kwargs["args"].append(f"--window-size={width},{height}")\n'
)


def t10_launch_body(moved: str) -> str:
    """2：在 #136 取值点替换之后、逐语句比较之前，把冻结函数的视口配置换成原生窗口。"""
    for anchor in (T10_VIEWPORT_LINE, T10_ARGS_TAIL, T10_NATIVE_BRANCH):
        assert moved.count(anchor) == 1, anchor
    moved = moved.replace(T10_VIEWPORT_LINE, T10_NO_VIEWPORT_LINE)
    moved = moved.replace(T10_ARGS_TAIL, T10_WINDOW_ARGS_TAIL)
    return moved.replace(T10_NATIVE_BRANCH, "")
