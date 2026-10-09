"""T09 旧桥 E 路径：小红书批次 checkpoint 的发布次数与旧实现一致。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from support import legacy_expectations as expectations

# T14：本文件只做旧桥（fork 工厂/E）与根的双轨对照或旧桥自测，T14-C 随旧桥整体删除。
pytestmark = list(expectations.legacy_only_marks())


ROOT = Path(__file__).resolve().parents[1]

# 子进程内按 E.main 的顺序安装导出与批次 hook，再分别经旧式 fork 累加器、
# E 实际装载的 fork 薄子类和正式 entry 装配发出同一批次事件，统计发布次数。
CHILD = r"""
import json, sys
import trippostcollect.xhs.batch_checkpoint as checkpoint

published = []
checkpoint.publish_batch = lambda details: published.append(details)

import mediacrawler_export_entrypoint as E
E.install_export_hook()

from tools import trippostcollect_adaptive as adaptive
from trippostcollect.application.events import append_worker_execution_event
from trippostcollect.platforms import entry

event = {"platform": "xhs", "batch_no": 1, "batch_complete": True}
counts = {}

legacy = adaptive.AdaptiveAccumulator.from_environment("xhs")
legacy.event_sink("adaptive_batch_completed", dict(event))
counts["legacy"] = len(published)

from media_platform.xhs.core import XiaoHongShuCrawler
crawler = XiaoHongShuCrawler()
bridge = crawler.ports.accumulator_factory()
bridge.event_sink("adaptive_batch_completed", dict(event))
counts["bridge"] = len(published) - counts["legacy"]

import config
formal = entry.xhs_dependencies(config)["ports"].accumulator_factory()
counts["formal_sink_is_worker_exit"] = formal.event_sink is append_worker_execution_event
counts["bridge_accumulator_is_fork"] = isinstance(bridge, adaptive.AdaptiveAccumulator)
print(json.dumps(counts))
"""


def test_e_bridge_publishes_xhs_batch_like_legacy(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("TRIPPOSTCOLLECT_")}
    environment.update({
        "PYTHONPATH": os.pathsep.join(str(ROOT / name) for name in ("src", "scripts")),
        "TRIPPOSTCOLLECT_XHS_BATCH_CHECKPOINT_ENABLED": "1",
        "TRIPPOSTCOLLECT_EXECUTION_STATE_PATH": str(state_path),
        "TRIPPOSTCOLLECT_XHS_REPAIR": "0",
    })
    result = subprocess.run(
        [sys.executable, "-P", "-c", CHILD], cwd=tmp_path, env=environment,
        text=True, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    counts = json.loads(result.stdout.splitlines()[-1])
    assert counts["legacy"] == 1
    assert counts["bridge"] == counts["legacy"]
    assert counts["bridge_accumulator_is_fork"] is True
    # 正式 entry 装配不经旧桥包装，仍由 configure_batch_checkpoint 绑定的出口发布。
    assert counts["formal_sink_is_worker_exit"] is True
