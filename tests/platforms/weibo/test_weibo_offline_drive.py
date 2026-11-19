"""微博离线边界：依赖方向、worker 装配与配置读取。

#211 起整轮搜索驱动与移动详情修复链不再收集。见 tests/support/weibo_issue211_deviation.py。
"""

from __future__ import annotations

import ast
import asyncio

import pytest

from support import worker_scenarios
from support.weibo_adapter import ROOT, settings
from trippostcollect.platforms import entry


def test_dependency_direction():
    for path in (ROOT / "src/trippostcollect/platforms/weibo").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = [node.module or ""] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            for module in modules:
                assert module.split(".")[0] not in {"scripts", "config", "tools", "media_platform", "store", "base", "var", "model", "proxy"}, (path, module)
                assert not module.startswith(("trippostcollect.artifacts", "trippostcollect.db", "trippostcollect.scheduler")), (path, module)
                if module.startswith("trippostcollect.application"):
                    assert module == "trippostcollect.application.contracts"
                if module.startswith("trippostcollect.runtime"):
                    assert module in {
                        "trippostcollect.runtime.helpers",
                        "trippostcollect.runtime.page_capture",
                        "trippostcollect.runtime.visit_ledger",
                        "trippostcollect.runtime.human_flow",
                    }, (path, module)
                if module.startswith("trippostcollect.platforms."):
                    assert module.startswith("trippostcollect.platforms.weibo")


def test_root_repair_attempts_and_exception_chain():
    """#211：移动详情修复链已删除。保留覆盖声明里的函数名，断言旧入口不存在。"""

    from trippostcollect.platforms.weibo import client, core

    assert not hasattr(client.WeiboClient, "get_note_info_by_id")
    assert not hasattr(client.WeiboClient, "_get_note_info_repair")
    assert not hasattr(core.WeiboCrawler, "get_note_full_text")
    text = (ROOT / "src/trippostcollect/platforms/weibo/client.py").read_text(encoding="utf-8")
    assert "m.weibo.cn" not in text


@pytest.mark.parametrize(
    "status,code",
    [(401, "login_required"), (403, "login_required"), (429, "rate_limited"), (418, "blocked_or_forbidden")],
)
def test_root_repair_page_status_is_classified(status, code):
    """详情 show 的阻断状态由桌面详情测试覆盖；这里确认修复入口不再导航到移动详情。"""

    from trippostcollect.platforms.weibo.detail_page import SHOW_STATUS_CODES
    from trippostcollect.platforms.weibo import models as weibo_models

    assert SHOW_STATUS_CODES[status] == code
    assert not hasattr(weibo_models, "WeiboRepairNotImplemented")


@pytest.mark.parametrize("post_repair", ["0", "1"])
def test_worker_assembly_uses_root_weibo(tmp_path, post_repair):
    from support.isolated_python import json_tail as _json_tail, run_python as _run_python

    result = _json_tail(_run_python(
        "import os, sys, json\n"
        f"os.environ['TRIPPOSTCOLLECT_POST_REPAIR'] = {post_repair!r}\n"
        "from trippostcollect.platforms.entry import configure, install_hooks, load_crawler\n"
        f"configure({worker_scenarios.worker_argv('weibo_search', tmp_path)!r})\n"
        "install_hooks()\n"
        "cls = load_crawler('wb')\n"
        "instance = cls()\n"
        "assert not any(name == p or name.startswith(p + '.') for name in sys.modules for p in ('media_platform.weibo', 'store.weibo'))\n"
        "assert instance.__class__.__module__ == 'trippostcollect.platforms.weibo.core'\n"
        "others = [load_crawler(code).__name__ for code in ('dy', 'zhihu', 'xhs')]\n"
        "print(json.dumps({'repair': instance.ports.post_repair, 'others': others}))\n",
        cwd=tmp_path,
    ))
    assert result == {"repair": post_repair == "1", "others": ["DouYinCrawler", "ZhihuCrawler", "XiaoHongShuCrawler"]}


@pytest.mark.parametrize(
    "exists,truthy,expected",
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_warmup_requires_desktop_config_uid(monkeypatch, exists, truthy, expected):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    import mediacrawler_login_warmup as warmup

    class Context:
        def on(self, _event, _callback=None):
            return None

    class Page:
        url = "https://weibo.com/"

        def on(self, _event, _callback=None):
            return None

        async def evaluate(self, script, arg=None):
            assert "$CONFIG" in script
            return {"exists": exists, "truthy": truthy}

    state = asyncio.run(warmup.current_state(Context(), Page(), "weibo"))
    assert state["ok"] is expected
    assert state["markers"] == {"uid_exists": exists, "uid_truthy": truthy}
    assert "uid" not in state["api"]


def test_configuration_snapshot_and_operation_readers(monkeypatch):
    from trippostcollect.application.worker_inputs import weibo_input_readers

    config = settings()
    snapshot, ports = entry.weibo_dependencies(config)
    config.KEYWORDS = "崂山旅游"
    assert snapshot.KEYWORDS == "青岛旅游"
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "2")
    assert ports.refresh_max_pages() == 2
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "invalid")
    assert ports.refresh_max_pages() == 0
    mapping = {"TRIPPOSTCOLLECT_WEIBO_BROWSER_DETAIL_TIMEOUT_MS": "bad"}
    reader = weibo_input_readers(environ=mapping).detail_timeout
    with pytest.raises(ValueError):
        reader()
    mapping["TRIPPOSTCOLLECT_WEIBO_BROWSER_DETAIL_TIMEOUT_MS"] = "12345"
    assert reader() == 12345
