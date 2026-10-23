"""局部启用业务观测；除 T14 登录资料根隔离外，路径夹具和真实 OS 测试不自动注入。"""

import signal
import threading
from types import SimpleNamespace

import pytest

from support.platform_sessions import redirect_platform_session_roots
from support.xhs_process_fakes import OWNER, FakeInspector
from trippostcollect.xhs.leases import LeaseGuard


# 每个用例的执行上限（#105）：超时即在当前执行位置抛 TimeoutError，让卡住的用例以失败结束，
# CI 的 failure_diagnostics 才能给出用例与行号，而不是整个 lane 卡到工作流上限被取消。
PER_TEST_TIMEOUT_SECONDS = 300


@pytest.fixture(autouse=True)
def per_test_watchdog():
    """SIGALRM 只能在主线程安装；没有 SIGALRM 的平台或非主线程不设上限。"""
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def expire(signum, frame):
        raise TimeoutError(f"test exceeded {PER_TEST_TIMEOUT_SECONDS}s (per-test watchdog, #105)")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, PER_TEST_TIMEOUT_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture(autouse=True)
def isolated_platform_sessions(tmp_path_factory, monkeypatch):
    """T14：把 platform_sessions 根重定向到临时目录。

    用例不得在真实 data/runtime/platform_sessions 下创建 profile，也不得受其中 `.partial` 残留影响。
    子进程用例用 support.platform_sessions.child_redirect_source 在子进程内做同样的重定向。
    """
    from trippostcollect.core import paths

    original = SimpleNamespace(sessions=paths.PLATFORM_SESSIONS_ROOT)
    root = tmp_path_factory.mktemp("platform_sessions_isolation")
    sessions = redirect_platform_session_roots(monkeypatch, root)
    return SimpleNamespace(root=root, sessions=sessions, original=original)


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
