"""局部启用业务观测；路径夹具和真实 OS 测试不自动注入。"""

import pytest

from support.xhs_process_fakes import OWNER, FakeInspector
from trippostcollect.xhs.leases import LeaseGuard


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
