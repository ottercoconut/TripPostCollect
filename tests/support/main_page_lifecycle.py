"""#55 有意偏离：根实现把主页面意外关闭改为浏览器生命周期异常子类。

冻结旧实现在主页面关闭时抛普通 ``RuntimeError("xhs_main_page_closed_unexpected:stage=…")``。根实现
改抛 ``XHSMainPageClosedUnexpected(CDPBrowserLifecycleError)``，消息逐字不变，使作者补全、登录恢复
等阶段按本轮生命周期失败处理，而不是候选跳过。

对照测试只在根侧 session 模块把抛出的类换成一个名为 ``RuntimeError`` 的同行为子类：它仍是
``XHSMainPageClosedUnexpected``/``CDPBrowserLifecycleError``，根实现的抛出与处理路径照常执行，只有记录的
异常类名与旧实现一致；请求、页面事件、异常消息、产物与事件仍逐字节比较。新行为由
``tests/platforms/xhs/test_xhs_creator_runtime_profile.py`` 与 ``test_xhs_discovery_memory.py`` 单独覆盖。
"""


def use_legacy_main_page_closed_name(patch, errors_module, session_module) -> None:
    """只改根侧抛出异常的类名，不改冻结 fixture 字节，也不改变根实现的异常层级与处理路径。"""
    assert hasattr(errors_module, "XHSMainPageClosedUnexpected")
    cls = errors_module.XHSMainPageClosedUnexpected
    assert session_module.XHSMainPageClosedUnexpected is cls
    assert issubclass(cls, RuntimeError) and cls.__name__ == "XHSMainPageClosedUnexpected"
    assert str(cls(stage="probe")) == "xhs_main_page_closed_unexpected:stage=probe"
    legacy_named = type("RuntimeError", (cls,), {"__module__": cls.__module__})
    patch.setattr(session_module, "XHSMainPageClosedUnexpected", legacy_named)
