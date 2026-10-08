"""T01 验收：包内资源为唯一真源、路径集中、台账冻结于基线并跟踪迁移进度。"""

from __future__ import annotations

import hashlib
import json
import sys
from importlib import import_module
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "scripts", ROOT / "scripts" / "dev"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

resources = import_module("trippostcollect.core.resources")
paths = import_module("trippostcollect.core.paths")
ledger = import_module("adapter_ledger")
BASELINE = json.loads((ROOT / "docs" / "adapter-ledger" / "baseline.json").read_text())
PACKAGE_RESOURCES = {
    "js/douyin.js": "libs/douyin.js",
    "js/zhihu.js": "libs/zhihu.js",
    "js/stealth.min.js": "libs/stealth.min.js",
    "licenses/MediaCrawler-LICENSE": "LICENSE",
}


# ---------- 包内资源 ----------

@pytest.mark.parametrize(("name", "baseline_key"), sorted(PACKAGE_RESOURCES.items()))
def test_package_resource_bytes_match_baseline(name: str, baseline_key: str) -> None:
    data = resources.read_bytes(name)
    assert hashlib.sha256(data).hexdigest() == BASELINE["resources"][baseline_key]


def test_resource_path_is_inside_package_not_fork() -> None:
    with resources.path("js/stealth.min.js") as location:
        assert location.is_file()
        assert "MediaCrawler" not in location.parts
        assert "trippostcollect" in location.parts


def test_unknown_resource_is_rejected() -> None:
    with pytest.raises(FileNotFoundError):
        resources.read_bytes("js/missing.js")
    with pytest.raises(ValueError):
        resources.read_bytes("../core/paths.py")


def test_resource_text_preserves_bom_handling() -> None:
    # 原调用以 utf-8-sig 读取 JS；读取接口必须给出与之相同的文本
    for name in ("js/douyin.js", "js/zhihu.js"):
        raw = resources.read_bytes(name)
        assert resources.read_text(name) == raw.decode("utf-8-sig")


# ---------- 路径集中 ----------

def test_platform_profile_paths_are_centralized() -> None:
    # T14：非小红书 profile 定义只在 core.paths，位于 data/runtime/platform_sessions/<platform>/
    for platform in ("bilibili", "weibo", "douyin", "zhihu"):
        session = paths.RUNTIME_ROOT / "platform_sessions" / platform
        assert paths.platform_profile_dir(platform) == session / "profile"
        assert paths.platform_cookie_snapshot_path(platform) == session / "trippostcollect_cookie_snapshot.json"


def test_unknown_platform_profile_is_rejected() -> None:
    with pytest.raises(KeyError):
        paths.platform_profile_dir("kuaishou")


def test_executor_and_warmup_share_profile_definition() -> None:
    crawl = import_module("mediacrawler_crawl")
    warmup = import_module("mediacrawler_login_warmup")
    for platform in ("bilibili", "weibo", "douyin", "zhihu"):
        assert crawl.profile_dir_for(platform) == paths.platform_profile_dir(platform)
        assert warmup.profile_dir_for(platform) == paths.platform_profile_dir(platform)
        assert crawl.cookie_snapshot_path(platform) == warmup.cookie_snapshot_path(platform)


def test_browser_discovery_has_single_definition() -> None:
    launcher = import_module("trippostcollect.runtime.browser_launcher")
    crawl = import_module("mediacrawler_crawl")
    warmup = import_module("mediacrawler_login_warmup")
    assert crawl.discover_cdp_browser_path is launcher.discover_cdp_browser_path
    assert warmup.discover_cdp_browser_path is launcher.discover_cdp_browser_path


def test_browser_discovery_prefers_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = import_module("trippostcollect.runtime.browser_launcher")
    fake = tmp_path / "chrome"
    fake.write_text("")
    monkeypatch.setenv("TRIPPOSTCOLLECT_CUSTOM_BROWSER_PATH", str(fake))
    assert launcher.discover_cdp_browser_path() == str(fake)


def test_prerequisites_check_resources_not_fork_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl = import_module("mediacrawler_crawl")
    calls = []
    monkeypatch.setattr(crawl, "verify_package_resources", lambda: calls.append(True))
    crawl.ensure_prerequisites()
    assert calls == [True]


def test_bilibili_stealth_comes_from_package(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl = import_module("mediacrawler_crawl")
    source = Path(crawl.__file__).read_text()
    assert 'MEDIACRAWLER_DIR / "libs"' not in source


# ---------- 台账：基线冻结与迁移进度 ----------

def test_committed_symbols_are_frozen_baseline() -> None:
    # 纯源码副本只核对已提交基线，逐字节重建由 CI 的 Git checkout 负责。
    result = ledger.load_symbols(ROOT)
    assert result["stat"]["total"] == 1234
    assert len(BASELINE["root_head"]) == 40
    assert len(BASELINE["fork_head"]) == 40


def test_progress_reports_every_baseline_row() -> None:
    report = ledger.build_progress(ROOT)
    assert report["total"] == 1234
    assert set(report["counts"]) <= {"pending", "moved", "exited", "missing"}
    assert sum(report["counts"].values()) == 1234
    assert report["missing"] == []


def test_progress_detects_moved_definition() -> None:
    report = ledger.build_progress(ROOT)
    moved = {(row["file"], row["qualname"]) for row in report["rows"] if row["state"] == "moved"}
    assert ("scripts/mediacrawler_crawl.py", "discover_cdp_browser_path") in moved


def test_inputs_are_unchanged_during_migration() -> None:
    # CLI 参数与默认值、env 名称在迁移全程必须与基线一致；
    # 唯一例外是规格 D2 授权删除的 6 个父发名字（ledger.AUTHORIZED_ENV_REMOVALS），已在漂移计算中排除。
    report = ledger.build_input_drift(ROOT)
    assert set(report) == {"cli_changed", "env_added", "env_removed"}
    assert report["cli_changed"] == {}
    assert report["env_added"] == []
    assert report["env_removed"] == []


@pytest.mark.parametrize("has_constructor", [True, False])
def test_progress_verifies_reexported_class_member(tmp_path, monkeypatch, has_constructor):
    """类的一跳重导出必须实际找到成员，不能仅凭类名算迁移完成。"""
    row = {"file": "old.py", "qualname": "Failure.__init__", "card": "T04",
           "disposition": "迁", "target": "artifacts/staging.py"}
    monkeypatch.setattr(ledger, "load_symbols", lambda _: {"rows": [row]})
    (tmp_path / "old.py").write_text("")
    target = tmp_path / "src/trippostcollect/artifacts/staging.py"
    target.parent.mkdir(parents=True)
    target.write_text("from trippostcollect.application.contracts import Failure\n")
    implementation = tmp_path / "src/trippostcollect/application/contracts.py"
    implementation.parent.mkdir(parents=True)
    body = "    def __init__(self): pass" if has_constructor else "    pass"
    implementation.write_text(f"class Failure(ValueError):\n{body}\n")
    report = ledger.build_progress(tmp_path)
    assert report["rows"][0]["state"] == ("moved" if has_constructor else "missing")
