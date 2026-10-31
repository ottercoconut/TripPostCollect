"""#136 有意偏离：warmup、通用平台 crawl 标准模式与 B站行为会话共用同一套持久 context 启动配置。

共享函数是 ``trippostcollect.runtime.browser_runtime.launch_profile_context``：显式开启沙箱（否则 Playwright 自动
追加 ``--no-sandbox``）、视口 ``PROFILE_VIEWPORT``（1280×900）、``locale=zh-CN``、``timezone_id=Asia/Shanghai``、
``accept_downloads``、``ignore_https_errors``、固定启动参数加运行时参数、运行时环境；不传 UA，不传代理。

固化预期（``tests/fixtures/t14_legacy_expectations/T06``）、冻结源码快照（``tests/fixtures/adapter_t08``、
``adapter_t10``、``adapter_t11``）与 manifest 不改、不用根实现重新生成。根侧比较对加载的预期或冻结源码做
“钉住旧值 → 单向替换”：先断言旧启动参数恰在预期位置、恰为旧值，再换成新值；其余部分仍整体 ``==`` 比较。

1. 抖音（T06）：标准模式 ``launch`` 行的 kwargs 由旧的 1920×1080 视口、``user_agent=None``、``proxy=None``、
   ``args=project_browser_args()`` 且未开沙箱，改为共享配置；启动后复用持久 context 自带的第一页，紧随其后的
   ``("new_page",)`` 行删除。须在 #59 profile 与 #127 stealth 偏离之后应用。
2. B站（T08）：冻结执行器行为会话的 ``launch`` 行由 1440×900 视口与 ``screen``、两项启动参数、
   ``ignore_default_args=["--enable-automation"]`` 且未开沙箱，改为共享配置；``executable_path`` 照旧传入。
3. warmup（T10）：``launch_login_context`` 的 kwargs 构造原样下沉到共享函数，自身改为委托调用；冻结函数体
   必须与共享函数逐语句相同（只差 ``headless``、运行时参数与环境的取值点）。
4. T11 冻结执行器：B站行为会话薄包装向 ``BilibiliBehaviorPorts`` 多传 ``launch_profile_context``。
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any

from support import native_window_deviation

ROOT = Path(__file__).resolve().parents[2]

PROFILE_VIEWPORT = {"width": 1280, "height": 900}
LEGACY_DOUYIN_VIEWPORT = {"width": 1920, "height": 1080}
LEGACY_BILIBILI_VIEWPORT = {"width": 1440, "height": 900}
# 共享配置的固定启动参数，运行时参数（crash dump 等）接在其后。
SHARED_LAUNCH_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]


def shared_launch_kwargs(
    user_data_dir: str,
    *,
    headless: bool,
    runtime_args: list[str],
    env: dict[str, str],
    executable_path: str | None = None,
) -> dict[str, Any]:
    """共享函数交给 ``launch_persistent_context`` 的完整 kwargs（不含 native_window_size 分支）。"""
    kwargs: dict[str, Any] = {
        "user_data_dir": user_data_dir,
        "headless": headless,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "viewport": dict(PROFILE_VIEWPORT),
        "accept_downloads": True,
        "ignore_https_errors": True,
        "chromium_sandbox": True,
        "args": [*SHARED_LAUNCH_ARGS, *runtime_args],
        "env": env,
    }
    if executable_path:
        kwargs["executable_path"] = executable_path
    return kwargs


def douyin(expected: dict[str, Any]) -> dict[str, Any]:
    """1：T06 drive 预期中恰一条 launch，kwargs 恰为旧标准模式参数，其后紧跟唯一的 new_page；替换并删除。"""
    trace = list(expected["trace"])
    indexes = [index for index, row in enumerate(trace) if row[0] == "launch"]
    assert len(indexes) == 1, indexes
    (index,) = indexes
    kwargs = trace[index][1]
    assert kwargs == {
        "accept_downloads": True, "args": [], "headless": False, "proxy": None, "user_agent": None,
        "user_data_dir": kwargs["user_data_dir"], "viewport": LEGACY_DOUYIN_VIEWPORT,
    }, kwargs
    trace[index] = ("launch", shared_launch_kwargs(kwargs["user_data_dir"], headless=False, runtime_args=[], env={}))
    new_pages = [position for position, row in enumerate(trace) if row[0] == "new_page"]
    assert new_pages == [index + 1] and trace[index + 1] == ("new_page",), new_pages
    del trace[index + 1]
    return {**expected, "trace": trace}


def bilibili(old: dict[str, Any]) -> dict[str, Any]:
    """2：T08 旧侧（已 JSON 归一）恰一条 launch，kwargs 恰为旧行为会话参数；替换为共享配置。"""
    value = copy.deepcopy(old)
    launches = [row for row in value["trace"] if row[0] == "launch"]
    assert len(launches) == 1, launches
    ((_, kwargs),) = launches
    legacy_args = kwargs["args"]
    assert legacy_args[:2] == ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"], legacy_args
    assert kwargs == {
        "user_data_dir": kwargs["user_data_dir"], "headless": kwargs["headless"],
        "executable_path": kwargs["executable_path"], "locale": "zh-CN", "timezone_id": "Asia/Shanghai",
        "viewport": LEGACY_BILIBILI_VIEWPORT, "screen": LEGACY_BILIBILI_VIEWPORT, "args": legacy_args,
        "env": kwargs["env"], "ignore_default_args": ["--enable-automation"],
    }, kwargs
    launches[0][1] = shared_launch_kwargs(
        kwargs["user_data_dir"], headless=kwargs["headless"], runtime_args=legacy_args[2:], env=kwargs["env"],
        executable_path=kwargs["executable_path"],
    )
    return value


# 3. warmup（T10）：冻结 launch_login_context 与共享函数之间只差下列取值点。
T10_VALUE_SITES = (
    ('"headless": False', '"headless": headless'),
    ("*browser_runtime_args()", "*runtime_args"),
    ('"env": browser_launch_environment()', '"env": env'),
    ("playwright.chromium.launch_persistent_context(", "chromium.launch_persistent_context("),
)


def _function(source: str, name: str) -> ast.AsyncFunctionDef:
    return next(node for node in ast.parse(source).body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)


def t10_launch_login_context(source: str) -> str:
    """3：在 #83、#127 变换之后的冻结 warmup 源码上，把 launch_login_context 换成现行委托实现。

    先证明冻结函数体在四个取值点替换后与 ``launch_profile_context`` 去掉缺省求值的函数体逐语句相同，
    即启动配置原样下沉；再换成现行 warmup.py 中的同名定义。
    """
    old = ast.get_source_segment(source, _function(source, "launch_login_context"))
    assert old and source.count(old) == 1
    moved = old
    for legacy, shared in T10_VALUE_SITES:
        assert moved.count(legacy) == 1, legacy
        moved = moved.replace(legacy, shared)
    # #139：再把视口配置单向换成原生窗口（见 support.native_window_deviation），之后才与共享函数比较。
    moved = native_window_deviation.t10_launch_body(moved)
    runtime = (ROOT / "src/trippostcollect/runtime/browser_runtime.py").read_text()
    shared_body = _function(runtime, "launch_profile_context").body
    # 共享函数开头是 docstring 与两处缺省求值（runtime_args / env 为 None 时取运行时值），其后与冻结体相同。
    assert isinstance(shared_body[0], ast.Expr) and all(isinstance(node, ast.If) for node in shared_body[1:3])
    assert [ast.dump(node) for node in _function(moved, "launch_login_context").body] == [
        ast.dump(node) for node in shared_body[3:]
    ]
    current = (ROOT / "src/trippostcollect/application/warmup.py").read_text()
    new = ast.get_source_segment(current, _function(current, "launch_login_context"))
    return source.replace(old, new)


# 4. T11 冻结执行器：B站行为会话薄包装多传一个端口。
T11_BEHAVIOR_PORTS_TAIL = (
    "            required_cookie_names=required_cookie_names,\n        ),\n    )\n"
)


def t11_source(text: str) -> str:
    """4：冻结执行器的 ``BilibiliBehaviorPorts(...)`` 末尾恰一处，追加 ``launch_profile_context``。"""
    assert text.count(T11_BEHAVIOR_PORTS_TAIL) == 1
    return text.replace(T11_BEHAVIOR_PORTS_TAIL, (
        "            required_cookie_names=required_cookie_names,\n"
        "            launch_profile_context=launch_profile_context,\n        ),\n    )\n"
    ))
