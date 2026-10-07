"""T00 迁移台账工具的验收测试：规则完整性、可复现与产物 schema。"""

from __future__ import annotations

import hashlib
import json
import sys
from importlib import import_module
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "scripts" / "dev"
if str(DEV) not in sys.path:
    sys.path.insert(0, str(DEV))

ledger = import_module("adapter_ledger")
LEDGER_DIR = ROOT / "docs" / "adapter-ledger"
DISPOSITIONS = {"迁", "拆", "并", "薄", "退"}
CARDS = {f"T{n:02d}" for n in range(15)}


# ---------- 符号账：规则与枚举 ----------

def test_current_baseline_has_no_unmapped_definitions() -> None:
    result = ledger.load_symbols(ROOT)
    assert result["unmapped"] == []
    assert result["stat"]["total"] == sum(result["stat"][d] for d in DISPOSITIONS)
    assert result["stat"]["total"] == 1234


def test_unmapped_definition_is_reported(tmp_path: Path) -> None:
    # 在临时源码树中增加一个没有规则的定义，生成必须报告而不是静默归类
    source = tmp_path / "src"
    target = source / "tools" / "MediaCrawler" / "tools"
    target.mkdir(parents=True)
    (target / "brand_new_helper.py").write_text("def brand_new_helper():\n    return 1\n")
    result = ledger.build_symbols(source, files=["tools/MediaCrawler/tools/brand_new_helper.py"])
    assert result["unmapped"] == [
        {"file": "tools/MediaCrawler/tools/brand_new_helper.py", "line": 1, "qualname": "brand_new_helper"}
    ]


def test_every_symbol_row_has_complete_fields() -> None:
    result = ledger.load_symbols(ROOT)
    for row in result["rows"]:
        assert set(row) >= {"file", "line", "qualname", "target", "disposition", "card", "tests", "note"}
        assert row["disposition"] in DISPOSITIONS
        assert row["card"] in CARDS
        if row["disposition"] == "退":
            assert row["target"] == "—"
        else:
            assert row["target"] and row["target"] != "—"


def test_exit_references_are_listed() -> None:
    # 退出符号被保留代码引用的位置必须全部列出，供 T12 切断
    result = ledger.load_symbols(ROOT)
    names = {item["symbol"] for item in result["exit_references"]}
    assert {"ProxyRefreshMixin", "AbstractCrawler", "create_ip_pool"} <= names
    for item in result["exit_references"]:
        assert item["referenced_by"], item


def test_committed_artifacts_are_reproducible() -> None:
    # 已提交的 JSON 渲染 C8 附录，无需 Git 历史且必须逐字节一致。
    rendered = ledger.render_symbols(ledger.load_symbols(ROOT))
    assert (ROOT / ledger.SYMBOL_MARKDOWN).read_bytes() == rendered.encode("utf-8")


def test_check_mode_detects_stale_artifact(tmp_path: Path) -> None:
    stale = tmp_path / "symbols.json"
    stale.write_text("{}\n")
    assert ledger.check_file(stale, "{\"rows\": []}\n") is False
    assert ledger.check_file(stale, "{}\n") is True


# ---------- 基线与资源散列 ----------

def test_baseline_records_three_heads_and_resource_hashes() -> None:
    baseline = json.loads((LEDGER_DIR / "baseline.json").read_text())
    for key in ("root_head", "fork_head", "upstream_head", "fork_upstream_merge_base"):
        assert len(baseline[key]) == 40
    resources = baseline["resources"]
    assert set(resources) == {"libs/douyin.js", "libs/zhihu.js", "libs/stealth.min.js", "LICENSE"}
    fork = ROOT / "tools" / "MediaCrawler"
    for relative, digest in resources.items():
        assert hashlib.sha256((fork / relative).read_bytes()).hexdigest() == digest


def test_resource_hashes_match_spec_c0() -> None:
    spec = (ROOT / "docs" / "platform-adapter-specification.md").read_text()
    baseline = json.loads((LEDGER_DIR / "baseline.json").read_text())
    for digest in baseline["resources"].values():
        assert digest in spec


# ---------- 输入展开 ----------

ENTRYPOINTS = {
    "scripts/crawl_runner.py", "scripts/mediacrawler_crawl.py", "scripts/xhs_runner.py",
    "scripts/repair_xhs_posts.py", "scripts/repair_post_details.py", "scripts/login_warmup.py",
    "scripts/mediacrawler_login_warmup.py",
}


def test_inputs_cover_all_entrypoints_and_match_source() -> None:
    inputs = ledger.load_inputs(ROOT)
    assert ENTRYPOINTS <= set(inputs["cli"])
    assert ledger.build_input_drift(ROOT) == {"cli_changed": {}, "env_added": [], "env_removed": []}


def test_cli_defaults_are_preserved_verbatim() -> None:
    cli = json.loads((LEDGER_DIR / "inputs.json").read_text())["cli"]
    crawl = {arg["flags"][0]: arg for arg in cli["scripts/mediacrawler_crawl.py"]}
    assert crawl["--keyword"]["default"] == "'青岛旅游'"
    assert crawl["--timeout-per-platform"]["default"] == "180"
    assert crawl["--xhs-post-interaction"]["choices"] == "('none', 'comment-scroll', 'like-one', 'random')"
    runner = {arg["flags"][0]: arg for arg in cli["scripts/crawl_runner.py"]}
    assert runner["--max-jobs"]["default"] == "3"


def test_moved_cli_definitions_keep_entrypoint_contract(tmp_path: Path) -> None:
    entry = "scripts/mediacrawler_crawl.py"
    source = ledger.CLI_DEFINITION_SOURCES[entry][0]
    definition = 'parser.add_argument("--keyword", default="青岛旅游")\n'
    baseline = ledger.extract_inputs(
        [entry], lambda relative: definition if relative == entry else ""
    )
    ledger_dir = tmp_path / ledger.LEDGER_DIR
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "inputs.json").write_text(ledger.json_text(baseline), encoding="utf-8")
    entry_path = tmp_path / entry
    entry_path.parent.mkdir(parents=True)
    entry_path.write_text(
        "from trippostcollect.application.inputs import parse_args\nparse_args()\n",
        encoding="utf-8",
    )
    source_path = tmp_path / source
    source_path.parent.mkdir(parents=True)
    source_path.write_text(definition, encoding="utf-8")
    assert ledger.build_input_drift(tmp_path) == {
        "cli_changed": {}, "env_added": [], "env_removed": [],
    }
    source_path.write_text(definition.replace("青岛旅游", "崂山旅游"), encoding="utf-8")
    drift = ledger.build_input_drift(tmp_path)
    assert set(drift["cli_changed"]) == {entry}
    assert len(drift["cli_changed"][entry]) == 2
    assert all("default" in change for change in drift["cli_changed"][entry])
    assert drift["env_added"] == drift["env_removed"] == []


def test_env_names_have_producers_or_consumers() -> None:
    env = json.loads((LEDGER_DIR / "inputs.json").read_text())["env"]
    assert "TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS" in env
    for name, item in env.items():
        assert name.startswith("TRIPPOSTCOLLECT_") or name == "TRIPPOST_PROJECT_ROOT"
        assert item["files"], name


# ---------- 测试节点台账 ----------

def test_tests_ledger_schema() -> None:
    data = json.loads((LEDGER_DIR / "tests.json").read_text())
    assert set(data) >= {"root", "fork", "collected_at_root_head"}
    for side in ("root", "fork"):
        assert data[side]["nodes"], side
        for node in data[side]["nodes"]:
            assert set(node) >= {"node_id", "lane", "markers", "source_file", "target_file", "protects", "card"}
            assert node["card"] in CARDS
    lanes = {node["lane"] for node in data["root"]["nodes"]}
    assert lanes <= {"component", "socket", "installation", "os"}
    assert {node["lane"] for node in data["fork"]["nodes"]} == {"fork"}


def test_fork_node_count_matches_ci_expectation() -> None:
    data = json.loads((LEDGER_DIR / "tests.json").read_text())
    run_matrix = (ROOT / "scripts" / "ci" / "run_matrix.py").read_text()
    expected = int(run_matrix.split("FORK_EXPECTED_TESTS = ", 1)[1].split("\n", 1)[0])
    moved_files = {"tests/runtime/test_cdp_browser.py", "tests/runtime/test_cdp_browser_lifecycle.py"}
    moved_files.update({
        "tests/platforms/weibo/test_weibo_empty_search.py",
        "tests/platforms/weibo/test_weibo_image_download.py",
        "tests/platforms/weibo/test_weibo_no_user_info.py",
        "tests/platforms/weibo/test_weibo_store.py",
        "tests/platforms/douyin/test_douyin_image_only.py",
        "tests/platforms/douyin/test_douyin_no_user_info.py",
        "tests/platforms/douyin/test_douyin_search_safety.py",
        "tests/platforms/douyin/test_douyin_store.py",
        "tests/platforms/zhihu/test_zhihu_detail_images.py",
        "tests/platforms/zhihu/test_zhihu_image_download.py",
        "tests/platforms/zhihu/test_zhihu_search_detail.py",
    })
    moved_files.update(f"tests/platforms/xhs/test_xhs_{name}.py" for name in (
        "core_access_error", "creator_enrichment", "discovery_memory", "image_download",
        "login_contract", "manual_wait_budget", "media_policy", "midrun_login_recovery",
        "network_recovery", "popup_guard", "qrcode_login", "qrcode_preview", "raw_response_errors",
        "shutdown_error_priority", "store_provenance",
    ))
    moved = [node for node in data["fork"]["nodes"] if node["target_file"] in moved_files]
    assert len(moved) == 383
    for node in moved:
        assert not (ROOT / node["source_file"]).exists()
        assert (ROOT / node["target_file"]).is_file()
    assert len(data["fork"]["nodes"]) - len(moved) == expected


def test_root_lane_assignment_follows_markers() -> None:
    data = json.loads((LEDGER_DIR / "tests.json").read_text())
    for node in data["root"]["nodes"]:
        markers = set(node["markers"])
        if "macos_process" in markers:
            assert node["lane"] == "os"
        elif "local_socket" in markers:
            assert node["lane"] == "socket"
        elif "installation" in markers:
            assert node["lane"] == "installation"
        else:
            assert node["lane"] == "component"


def test_tests_collector_refuses_production_checkout() -> None:
    with pytest.raises(SystemExit):
        ledger.main(["tests", "--source", str(ROOT)])


def test_async_return_await_delegation_is_moved() -> None:
    import ast

    tree = ast.parse(
        "from trippostcollect.runtime import worker\n"
        "async def async_cleanup():\n"
        "    return await worker.async_cleanup(crawler, config.PLATFORM)\n"
    )
    assert ledger.delegated_definition(
        tree.body[1], ledger.imported_names(tree), "trippostcollect.runtime.worker"
    ) == "async_cleanup"


@pytest.mark.parametrize(
    ("target_source", "shared_source", "expected"),
    [
        ("from trippostcollect.records.x import f as f\n", "def f():\n    return 1\n", "moved"),
        ("from trippostcollect.records.x import f as f\n", "def other():\n    return 1\n", "missing"),
        ("from trippostcollect.records.x import f as f\n", "from trippostcollect.records.y import f\n", "missing"),
        ("from scripts.shared import f as f\n", "def f():\n    return 1\n", "missing"),
        ("from tools.MediaCrawler.tools.shared import f as f\n", "def f():\n    return 1\n", "missing"),
        ("import trippostcollect.records.x.f as f\n", "def f():\n    return 1\n", "missing"),
    ],
    ids=["defined", "undefined", "second-hop", "scripts", "fork", "module-import"],
)
def test_progress_checks_one_package_reexport(
    tmp_path: Path, target_source: str, shared_source: str, expected: str,
) -> None:
    # 原文件只留转发；目标可将纯定义下沉，但不能用多跳或外部导入冒充迁移。
    files = {
        "scripts/original.py": "from trippostcollect.application.target import f\n",
        "src/trippostcollect/application/target.py": target_source,
        "src/trippostcollect/records/x.py": shared_source,
        "src/trippostcollect/records/y.py": "def f():\n    return 1\n",
        "scripts/shared.py": "def f():\n    return 1\n",
        "tools/MediaCrawler/tools/shared.py": "def f():\n    return 1\n",
    }
    for relative, source in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    row = {
        "file": "scripts/original.py", "qualname": "f", "card": "T04",
        "disposition": "迁", "target": "application/target.py",
    }
    ledger_path = tmp_path / ledger.LEDGER_DIR / "symbols.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(ledger.json_text({"rows": [row]}), encoding="utf-8")

    progress = ledger.build_progress(tmp_path)

    assert progress["rows"] == [{**row, "state": expected}]
    assert progress["counts"][expected] == 1
    assert progress["counts"]["missing"] == (expected == "missing")


@pytest.mark.parametrize("case,expected", [
    ("direct", "moved"), ("async", "moved"), ("alias", "moved"),
    ("no-method", "missing"), ("no-inheritance", "missing"),
    ("wrong-module", "missing"), ("relative", "missing"),
    ("attribute", "missing"), ("indirect", "missing"),
    ("no-class-row", "missing"), ("exited-class", "missing"),
    ("no-class", "missing"), ("second-hop", "missing"),
    ("pending", "pending"), ("real-t09", "moved"),
])
def test_progress_direct_mixin(tmp_path: Path, case: str, expected: str) -> None:
    # 使用冻结账的真实 T09 方法和组合类行，复现轮前阻塞。
    rows = ledger.load_symbols(ROOT)["rows"]
    original = "tools/MediaCrawler/media_platform/xhs/core.py"
    class_name = "XiaoHongShuCrawler"
    method = "_get_manual_wait_budget"
    method_row = next(r for r in rows if r["file"] == original
                      and r["qualname"] == f"{class_name}.{method}")
    class_row = next(r.copy() for r in rows if r["file"] == original
                     and r["qualname"] == class_name)
    assert method_row["target"] == "platforms/xhs/session.py"
    assert class_row["target"] == "platforms/xhs/core.py"
    definition = f"    {'async ' if case == 'async' else ''}def {method}(self):\n        return 1\n"
    mixin = "class XhsSessionMixin:\n" + definition
    binding = "from trippostcollect.platforms.xhs.session import XhsSessionMixin\n"
    base = "XhsSessionMixin"
    if case == "alias":
        binding = binding.rstrip() + " as Session\n"
        base = "Session"
    elif case == "wrong-module":
        binding = binding.replace("xhs.session", "xhs.other")
    elif case == "relative":
        binding = "from .session import XhsSessionMixin\n"
    elif case == "attribute":
        binding = "import trippostcollect.platforms.xhs.session as session\n"
        base = "session.XhsSessionMixin"
    elif case == "indirect":
        binding += "class A(XhsSessionMixin):\n    pass\n"
        base = "A"
    elif case == "no-inheritance":
        base = "object"
    if case == "no-method":
        mixin = "class XhsSessionMixin:\n    pass\n"
    elif case == "second-hop":
        mixin = "from trippostcollect.platforms.xhs.other import XhsSessionMixin\n"
    if case == "exited-class":
        class_row["disposition"] = "退"
    selected = [method_row] + ([] if case == "no-class-row" else [class_row])
    files = {
        original: f"class {class_name}:\n{definition}" if case == "pending" else "",
        "src/trippostcollect/platforms/xhs/session.py": mixin,
        "src/trippostcollect/platforms/xhs/other.py": "class XhsSessionMixin:\n" + definition,
        "src/trippostcollect/platforms/xhs/core.py": binding + (
            "" if case == "no-class" else f"class {class_name}({base}):\n    pass\n"
        ),
        "docs/adapter-ledger/symbols.json": ledger.json_text({"rows": selected}),
    }
    for relative, source in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    assert ledger.build_progress(tmp_path)["rows"][0]["state"] == expected


@pytest.mark.parametrize("case,expected", [
    ("own-method", "missing"), ("own-attribute", "missing"),
    ("earlier-method", "missing"), ("earlier-relative", "missing"),
    ("earlier-attribute", "missing"), ("earlier-ancestor", "missing"),
    ("earlier-clean", "moved"), ("earlier-object", "moved"),
    ("t09-navigation", "moved"), ("target-ancestor", "moved"),
])
def test_progress_mixin_shadowing(tmp_path: Path, case: str, expected: str) -> None:
    original = "tools/MediaCrawler/media_platform/xhs/core.py"
    method = "navigate"
    row = {"file": original, "qualname": f"XiaoHongShuCrawler.{method}",
           "card": "T09", "disposition": "拆", "target": "platforms/xhs/navigation.py"}
    rows = [row, {**row, "qualname": "XiaoHongShuCrawler", "target": "platforms/xhs/core.py"}]
    definition = f"    def {method}(self):\n        return 1\n"
    imports = ("from trippostcollect.platforms.xhs.navigation import XhsNavigationMixin\n"
               "from trippostcollect.platforms.xhs.session import XhsSessionMixin\n")
    bases = "XhsSessionMixin, XhsNavigationMixin"
    session = "class XhsSessionMixin:\n    pass\n"
    own = "    pass\n"
    if case == "own-method":
        own = definition
    elif case == "own-attribute":
        own = "    navigate = None\n"
    elif case == "earlier-method":
        session = "class XhsSessionMixin:\n" + definition
    elif case == "earlier-relative":
        imports = imports.replace("from trippostcollect.platforms.xhs.session", "from .session")
    elif case == "earlier-attribute":
        imports += "import trippostcollect.platforms.xhs.session as session\n"
        bases = "session.XhsSessionMixin, XhsNavigationMixin"
    elif case == "earlier-ancestor":
        session = "class Parent:\n" + definition + "class XhsSessionMixin(Parent):\n    pass\n"
    elif case == "earlier-object":
        session = session.replace("XhsSessionMixin:", "XhsSessionMixin(object):")
    elif case == "t09-navigation":
        imports += "from trippostcollect.platforms.xhs.errors import XhsErrorsMixin as Errors\n"
        bases = "XhsSessionMixin, Errors, XhsNavigationMixin"
    navigation = "class XhsNavigationMixin" + ("(Parent)" if case == "target-ancestor" else "")
    files = {
        original: "",
        "src/trippostcollect/platforms/xhs/navigation.py": navigation + ":\n" + definition,
        "src/trippostcollect/platforms/xhs/session.py": session,
        "src/trippostcollect/platforms/xhs/errors.py": "class XhsErrorsMixin(object):\n    pass\n",
        "src/trippostcollect/platforms/xhs/core.py": imports + f"class XiaoHongShuCrawler({bases}):\n" + own,
        "docs/adapter-ledger/symbols.json": ledger.json_text({"rows": rows}),
    }
    for relative, source in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    located = ledger.locate_definition(tmp_path, row, rows=rows)
    assert located.state == expected
    if expected == "moved":
        assert located.qualname == "XhsNavigationMixin.navigate"
