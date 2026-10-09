"""局部启用业务观测；除 T14 登录资料根隔离外，路径夹具和真实 OS 测试不自动注入。"""

from types import SimpleNamespace

import pytest

from support.platform_sessions import redirect_platform_session_roots
from support.xhs_process_fakes import OWNER, FakeInspector
from trippostcollect.xhs.leases import LeaseGuard


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
