"""#139：通用平台持久 context 不再用 viewport 做设备模拟，与小红书一样按原生窗口启动。

``viewport=PROFILE_VIEWPORT``（1280×900）让 Playwright 模拟设备：页面读到 ``screen`` 1280×900、DPR 1，且
``outerHeight`` 被 macOS 收进可用区域后小于 ``innerHeight``，前后矛盾。现在共享函数缺省不传 ``viewport``、传
``no_viewport=True``，启动参数末尾追加 ``--window-size=1450,900``，``screen``、DPR 与窗口尺寸由浏览器给出真实值。

全部离线：复用 #136 用例的替身 chromium，只记录 ``launch_persistent_context(**kwargs)``，不访问网络、不启动浏览器。
"""

from __future__ import annotations

import asyncio

import pytest

from test_shared_launch_issue136 import FakeChromium, _all_launches, shared  # noqa: F401  shared 是 fixture
from trippostcollect.runtime import browser_runtime

# 真机屏幕宽 1470；窗口宽度不得超过屏幕，否则 macOS 会再缩窗口，``outerWidth`` 与请求值不一致。
MAX_SCREEN_WIDTH = 1470


def _window_size(kwargs: dict) -> tuple[int, int]:
    sizes = [arg for arg in kwargs["args"] if arg.startswith("--window-size=")]
    assert len(sizes) == 1 and kwargs["args"][-1] == sizes[0], kwargs["args"]
    width, height = sizes[0].removeprefix("--window-size=").split(",")
    return int(width), int(height)


def _assert_native_window(kwargs: dict, label: str) -> None:
    assert "viewport" not in kwargs, label
    assert "screen" not in kwargs and "device_scale_factor" not in kwargs, label
    assert kwargs["no_viewport"] is True, label
    width, height = _window_size(kwargs)
    assert (width, height) == browser_runtime.NATIVE_WINDOW_SIZE, label
    assert 0 < width <= MAX_SCREEN_WIDTH and height > 0, label


def test_native_window_size_matches_xhs() -> None:
    assert browser_runtime.NATIVE_WINDOW_SIZE == browser_runtime.XHS_NATIVE_WINDOW_SIZE == (1450, 900)


def test_shared_launcher_defaults_to_native_window(shared, tmp_path) -> None:  # noqa: F811
    chromium = FakeChromium()
    asyncio.run(shared(chromium, tmp_path, headless=False))
    (kwargs,) = chromium.launches
    _assert_native_window(kwargs, "shared")


@pytest.mark.parametrize("entry", ["warmup", "douyin", "weibo", "bilibili"])
def test_every_entry_launches_a_native_window(entry, monkeypatch, tmp_path, shared) -> None:  # noqa: F811
    launches = _all_launches(monkeypatch, tmp_path, shared)
    _assert_native_window(launches[entry], entry)
