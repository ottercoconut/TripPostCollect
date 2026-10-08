"""局部启用业务观测；除 T14 登录资料根隔离外，路径夹具和真实 OS 测试不自动注入。"""

from types import SimpleNamespace

import pytest

from support import legacy_expectations
from support.platform_sessions import redirect_platform_session_roots
from support.xhs_process_fakes import OWNER, FakeInspector
from trippostcollect.xhs.leases import LeaseGuard


@pytest.fixture(autouse=True)
def isolated_platform_sessions(tmp_path_factory, monkeypatch):
    """T14：把旧 fork profile 根与新 platform_sessions 根重定向到临时目录。

    主 checkout 中可能仍有未迁移的 tools/MediaCrawler/browser_data，失败关闭检查不得因此让无关用例失败，
    用例也不得在真实 data/runtime/platform_sessions 下创建 profile。子进程用例用
    support.platform_sessions.child_redirect_source 在子进程内做同样的重定向。
    """
    from trippostcollect.core import paths

    original = SimpleNamespace(
        legacy=paths.LEGACY_FORK_PROFILE_ROOT, sessions=paths.PLATFORM_SESSIONS_ROOT,
    )
    root = tmp_path_factory.mktemp("platform_sessions_isolation")
    legacy, sessions = redirect_platform_session_roots(monkeypatch, root)
    return SimpleNamespace(root=root, legacy=legacy, sessions=sessions, original=original)


def pytest_addoption(parser):
    # T14：仅守卫测试使用；给出目录时把旧实现当场结果写出而不断言（见 support/legacy_expectations.py）。
    parser.addoption(legacy_expectations.WRITE_OPTION, default=None, metavar="DIR",
                     help="T14 守卫测试把旧实现当场结果写到 DIR，供人工审阅后替换固化预期")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", f"{legacy_expectations.GUARD_MARKER}: T14 守卫——fork/E 存在时旧侧当场结果须与固化预期逐字节一致",
    )
    config.addinivalue_line(
        "markers", f"{legacy_expectations.LEGACY_ONLY_MARKER}: 只测 fork/E 自身行为，T14-C 随旧桥删除",
    )


def pytest_sessionfinish(session, exitstatus):
    target = session.config.getoption(legacy_expectations.WRITE_OPTION)
    if target:
        legacy_expectations.write_manifest(target)


@pytest.fixture
def business_inspector(monkeypatch):
    # 明确证明此用例没有 child/group/profile 残留，不代表宿主扫描结果。
    inspector = FakeInspector(
        current=OWNER, identities={OWNER.pid: OWNER},
        presences={OWNER.pid: True}, groups={}, profile_processes=[],
    )

    def reject_real_signal(*args):
        pytest.fail(f"业务观测中的 PID 不得进入真实信号接口: {args}")

    monkeypatch.setattr("os.kill", reject_real_signal)
    monkeypatch.setattr("os.killpg", reject_real_signal)
    return inspector


@pytest.fixture
def inject_business_guard(monkeypatch, business_inspector):
    def inject(module):
        class ObservedLeaseGuard(LeaseGuard):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs, inspector=business_inspector)

        monkeypatch.setattr(module, "LeaseGuard", ObservedLeaseGuard)
        return business_inspector

    return inject
