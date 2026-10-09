"""T11 冻结源码与执行器共用离线驱动，核对编排、提交和产物。"""

from __future__ import annotations

import ast
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import importlib
import importlib.util
import inspect
import io
import json
from pathlib import Path
import random
import re
import socket
import sqlite3
import subprocess
import sys
from types import ModuleType, SimpleNamespace

from PIL import Image
import pytest

from support import fork_removal_deviation
from trippostcollect.artifacts.image_candidates import content_image_candidates
from trippostcollect.artifacts.image_manifest import ImageManifestEntry, write_manifest_atomic
from trippostcollect.artifacts.image_materialization import write_staging_image
from trippostcollect.core.execution_state import FrozenExecutionState
from trippostcollect.db.bootstrap import bootstrap_connection
from trippostcollect.platforms.bilibili import core as bilibili_core
from trippostcollect.runtime import process

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/adapter_t11"
ROWS = [r for r in json.loads((ROOT / "docs/adapter-ledger/symbols.json").read_text())["rows"] if r["card"] == "T11"]
sys.path.insert(0, str(ROOT / "scripts"))
executor = importlib.import_module("mediacrawler_crawl")

# 第一阶段每项均为零差异；第二阶段只能逐项登记并审阅，不允许通配规则。
# 可用规则键：leading_ports_bindings（名称元组）、trailing_keyword_ports（布尔值）、
# injected_keyword_params（名称元组：签名新增的必填仅关键字注入参数，按原全局名命名）、
# ports_call_sites（被调名称元组：对这些名字的每一处直接调用都必须恰好追加 ports=ports，归一化时剥离）、
# annotation_names（旧名到新名的映射，仅遍历注解）。控制流和其他调用表达式不归一化。
AST_RULES = {
    "item_type_from_path": {},
    "truncate": {},
    "extract_sample": {},
    "summarize_jsonl": {},
    "summarize_output": {},
    "summarize_output_with_progress": {},
    "write_json_with_progress": {},
    "terminal_summary_envelope": {},
    "ensure_web_schema": {},
    "stable_douyin_search_id": {},
    "effective_discovery_checkpoint_event": {},
    "persist_discovery_checkpoint": {},
    "load_pagination_evidence": {},
    "attach_skipped_candidate_evidence": {},
    "collect_formal_records": {},
    "resolve_media_root": {},
    "_project_relative_evidence_path": {},
    "_load_manifest_with_evidence": {},
    "_staging_root_for_manifest_entry": {},
    "rollback_newly_promoted_images": {},
    "formal_media_persistence_lock": {},
    "_validated_manifest_rows_for_post": {},
    "materialize_formal_record_images": {},
    "find_existing_post": {},
    "upsert_web_post": {},
    "FormalImportBeforeCommitError": {},
    "FormalImportBeforeCommitError.__init__": {},
    "commit_formal_import": {},
    "import_valid_records": {},
    "import_valid_records_with_media_rollback": {},
    "_run_platform_without_policy": {'trailing_keyword_ports': True, 'leading_ports_bindings': ('run_bilibili_article_search',)},
    "effective_attempt_exit_code": {},
    "run_platform": {'trailing_keyword_ports': True, 'ports_call_sites': ('_run_platform_without_policy',)},
    "collect_behavior_validation": {},
    "latest_platform_result_counts": {},
    "write_markdown": {},
    "apply_formal_completion_gates": {},
    "formal_import_gate_met": {},
    "formal_image_promotion_allowed": {},
    "runtime_blocker_stop_reason": {},
    "runtime_blocker_from_terminal_event": {},
    "runtime_blocker_from_pagination_evidence": {},
    "latest_runtime_blocker": {},
    "apply_runtime_blocker": {},
    "_run_main": {'trailing_keyword_ports': True, 'leading_ports_bindings': ('ensure_prerequisites',), 'ports_call_sites': ('run_platform',)},
    # 协调者预置：原 main 三行逐字，三个全局名改为同名注入参数（由脚本薄入口调用时传入）。
    "main": {"injected_keyword_params": ("parse_args", "xhs_supervisor_runtime_reporter_from_context", "_run_main")},
}


# T12 按规格授权的两处改动，在冻结旧定义上做同样的删除后再逐字比较，其余差异仍由 AST 断言拦截：
# 规格 D2（469/485 行）删除 6 个父发无消费者的 env；规格 C1 以包内资源替代 fork 源码树存在检查。
T12_REMOVED_ENV = {
    "TRIPPOSTCOLLECT_DISCOVERY_RUN_ID", "TRIPPOSTCOLLECT_DISCOVERY_PLATFORM", "TRIPPOSTCOLLECT_DISCOVERY_KEYWORD",
    "TRIPPOSTCOLLECT_DISCOVERY_RESUME_PAGE", "TRIPPOSTCOLLECT_DISCOVERY_CHECKPOINT_WRITE_DISABLED",
    "TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_WAIT_SECONDS",
}
# T14-C 授权差异：唯一消费者 fork 旧 store 随 fork 删除，父侧不再发出（ledger.AUTHORIZED_ENV_REMOVALS 同步登记）。
T14C_REMOVED_ENV = {"TRIPPOSTCOLLECT_XHS_KEEP_AUTHOR_DETAIL"}


def t12_authorized(node):
    node = deepcopy(node)
    for child in ast.walk(node):
        if isinstance(child, ast.Dict):
            kept = [(key, value) for key, value in zip(child.keys, child.values)
                    if not (isinstance(key, ast.Constant) and key.value in T12_REMOVED_ENV | T14C_REMOVED_ENV)]
            child.keys, child.values = [key for key, _ in kept], [value for _, value in kept]
    if isinstance(node, ast.FunctionDef) and node.name == "ensure_prerequisites":
        node.body = [statement for statement in node.body
                     if not (isinstance(statement, ast.If) and "MEDIACRAWLER_DIR" in ast.unparse(statement.test))]
    return node


# #75 授权差异：删除 timeout_per_platform 与按平台的下限，无进展看门狗阈值改为代码常量。
T75_OLD_TIMEOUT_SOURCE = (
    "    timeout = args.timeout_per_platform\n"
    "    if platform_key == \"xhs\" and (args.login_type == \"qrcode\" or args.headed):\n"
    "        timeout = max(timeout, 420)\n"
    "    if platform_key == \"zhihu\":\n"
    "        timeout = max(timeout, 300)\n"
)
T75_REPLACEMENTS = (
    (T75_OLD_TIMEOUT_SOURCE, ""),
    ("        ROOT,\n        timeout,\n        log_dir,", "        ROOT,\n        NO_PROGRESS_WATCHDOG_SECONDS,\n        log_dir,"),
    ("inactivity_timeout_seconds=float(timeout),", "inactivity_timeout_seconds=NO_PROGRESS_WATCHDOG_SECONDS,"),
)


def t75_authorized_source(text):
    """在冻结旧源码上做 #75 的同样替换；每处必须恰好出现一次。"""
    for old, new in T75_REPLACEMENTS:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


def t14_authorized(node):
    """T14 授权差异：run_platform 在预算守卫前对非小红书平台做 profile 迁移失败关闭检查。"""
    node = deepcopy(node)
    for child in ast.walk(node):
        body = getattr(child, "body", None)
        if isinstance(body, list):
            child.body = [statement for statement in body
                          if not (isinstance(statement, ast.If)
                                  and "require_platform_session_migrated" in ast.unparse(statement))] or body
    return node


def definition(source, qualname):
    node = ast.parse(source)
    for name in qualname.split("."):
        found = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name]
        assert len(found) == 1, f"新定义不存在或不唯一：{qualname}"
        node = found[0]
    return node


def normalized_pair(old, new, rules):
    """严格限定三类接缝；未登记的差异保留给 AST 断言。"""
    old, new = deepcopy(old), deepcopy(new)
    assert set(rules) <= {"leading_ports_bindings", "trailing_keyword_ports", "injected_keyword_params",
                          "ports_call_sites", "annotation_names"}
    names = rules.get("leading_ports_bindings", ())
    offset = int(bool(new.body and isinstance(new.body[0], ast.Expr)
                      and isinstance(new.body[0].value, ast.Constant)
                      and isinstance(new.body[0].value.value, str)))
    for name in names:
        expected = ast.parse(f"{name} = ports.{name}").body[0]
        assert ast.dump(new.body[offset]) == ast.dump(expected)
        del new.body[offset]
    injected = tuple(rules.get("injected_keyword_params", ()))
    if injected:
        assert tuple(a.arg for a in new.args.kwonlyargs[:len(injected)]) == injected
        assert all(default is None for default in new.args.kw_defaults[:len(injected)])
        del new.args.kwonlyargs[:len(injected)]
        del new.args.kw_defaults[:len(injected)]
    if rules.get("trailing_keyword_ports"):
        assert new.args.kwonlyargs[-1].arg == "ports"
        assert new.args.kw_defaults[-1] is None
        new.args.kwonlyargs.pop()
        new.args.kw_defaults.pop()

    callees = set(rules.get("ports_call_sites", ()))
    if callees:
        stripped = 0
        for node in ast.walk(new):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in callees:
                tail = node.keywords[-1] if node.keywords else None
                assert tail is not None and tail.arg == "ports" and isinstance(tail.value, ast.Name) \
                    and tail.value.id == "ports", f"{node.func.id} 调用缺少末尾 ports=ports"
                node.keywords.pop()
                stripped += 1
        assert stripped, "登记了 ports_call_sites 却没有对应调用"

    class AnnotationNames(ast.NodeTransformer):
        def visit_Name(self, node):
            return ast.Name(id=rules["annotation_names"].get(node.id, node.id), ctx=node.ctx)

    if rules.get("annotation_names"):
        rewrite = AnnotationNames()
        for node in ast.walk(old):
            for field in ("annotation", "returns"):
                annotation = getattr(node, field, None)
                if annotation is not None:
                    setattr(node, field, rewrite.visit(annotation))
    return ast.dump(old), ast.dump(new)


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row["qualname"])
def test_a_migrated_ast(row):
    name = row["qualname"]
    target = ROOT / "src/trippostcollect" / row["target"]
    if row["disposition"] == "薄":
        target = ROOT / "src/trippostcollect/application/collection.py"
    assert target.exists(), f"新定义不存在：{target.relative_to(ROOT)}::{name}"
    old = t12_authorized(definition(t75_authorized_source((FIXTURES / "mediacrawler_crawl.py.txt").read_text()), name))
    new = t14_authorized(definition(target.read_text(), name))
    assert normalized_pair(old, new, AST_RULES[name])[0] == normalized_pair(old, new, AST_RULES[name])[1]
    if row["disposition"] == "薄":
        tree = ast.parse((ROOT / row["file"]).read_text())
        entry = definition((ROOT / row["file"]).read_text(), name)
        assert len(entry.body) == 1 and isinstance(entry.body[0], ast.Return)
        call = entry.body[0].value
        # 薄入口只把脚本全局名按同名关键字传入，调用时取值，现有对脚本名的 patch 继续生效。
        assert isinstance(call, ast.Call) and not call.args
        injected = tuple(AST_RULES[name].get("injected_keyword_params", ()))
        assert tuple(k.arg for k in call.keywords) == injected
        assert all(isinstance(k.value, ast.Name) and k.value.id == k.arg for k in call.keywords)
        imports = {a.asname or a.name: (n.module, a.name) for n in tree.body
                   if isinstance(n, ast.ImportFrom) for a in n.names}
        assert isinstance(call.func, ast.Name)
        assert imports[call.func.id] == ("trippostcollect.application.collection", "main")


def test_a_other_top_level_definitions_unchanged():
    names = {r["qualname"].split(".")[0] for r in ROWS}
    # T09 按台账迁出的定义（child reporter 等）不再留在脚本，排除台账 card=="T09" 的行。
    ledger = json.loads((ROOT / "docs/adapter-ledger/symbols.json").read_text())["rows"]
    names |= {r["qualname"].split(".")[0] for r in ledger
              if r["card"] == "T09" and r["file"] == "scripts/mediacrawler_crawl.py"}
    old = ast.parse((FIXTURES / "mediacrawler_crawl.py.txt").read_text())
    new = (ROOT / "scripts/mediacrawler_crawl.py").read_text()
    assert len(ROWS) == 46 and set(AST_RULES) == {r["qualname"] for r in ROWS}
    for node in old.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name not in names:
            assert ast.dump(t12_authorized(node)) == ast.dump(definition(new, node.name)), node.name


def test_ast_normalization_interface():
    old = definition("def sample(value: Old):\n    first(value)\n    second(value)\n    return 1\n", "sample")
    new = definition("def sample(value: New, *, ports):\n    first = ports.first\n    first(value)\n    second(value)\n    return 1\n", "sample")
    assert normalized_pair(old, new, {})[0] != normalized_pair(old, new, {})[1]
    rules = {"leading_ports_bindings": ("first",), "trailing_keyword_ports": True,
             "annotation_names": {"Old": "New"}}
    assert normalized_pair(old, new, rules)[0] == normalized_pair(old, new, rules)[1]
    call_old = definition("def sample(value):\n    return inner(value, flag=1)\n", "sample")
    call_new = definition("def sample(value, *, ports):\n    return inner(value, flag=1, ports=ports)\n", "sample")
    call_rules = {"trailing_keyword_ports": True, "ports_call_sites": ("inner",)}
    assert normalized_pair(call_old, call_new, call_rules)[0] == normalized_pair(call_old, call_new, call_rules)[1]
    for bad in ("return inner(value, ports=ports, flag=1)", "return inner(value, flag=1, ports=other)",
                "return inner(value, flag=2, ports=ports)"):
        changed = definition("def sample(value, *, ports):\n    " + bad + "\n", "sample")
        try:
            pair = normalized_pair(call_old, changed, call_rules)
        except AssertionError:
            continue
        assert pair[0] != pair[1], bad
    for body in ("first(value)\n    second(value)\n    return 2",
                 "second(value)\n    first(value)\n    return 1",
                 "first(value)\n    second(value)\n    raise RuntimeError",
                 "if value:\n        first(value)\n    second(value)\n    return 1"):
        changed = definition("def sample(value: New, *, ports):\n    first = ports.first\n    " + body, "sample")
        assert normalized_pair(old, changed, rules)[0] != normalized_pair(old, changed, rules)[1]


@pytest.fixture
def baseline(monkeypatch):
    source = (FIXTURES / "mediacrawler_crawl.py.txt").read_bytes()
    provenance = json.loads((FIXTURES / "provenance.json").read_text())
    assert sha256(source).hexdigest() == provenance["fixtures_sha256"]["mediacrawler_crawl.py.txt"]
    module = ModuleType("t11_frozen_executor")
    module.__file__ = str(FIXTURES / "mediacrawler_crawl.py.txt")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    # T14-C 有意偏离：MEDIACRAWLER_DIR 与过渡模块 execution_state 已删除，执行前单向替换旧导入。
    text, namespace = fork_removal_deviation.frozen_source(
        source.decode("utf-8"), mediacrawler_dir_imports=1, execution_state_imports=1,
    )
    # #75 有意偏离：任务级时限删除，旧执行器同样改用统一的无进展看门狗常量。
    text = t75_authorized_source(text)
    namespace["NO_PROGRESS_WATCHDOG_SECONDS"] = process.NO_PROGRESS_WATCHDOG_SECONDS
    vars(module).update(namespace)
    exec(compile(text, module.__file__, "exec"), vars(module))
    return module


def strict_patch(monkeypatch, target, name, value):
    """不存在的属性必须失败，禁止默默增加一个没有被调用的 mock。"""
    assert hasattr(target, name), f"patch 目标不存在：{target}.{name}"
    monkeypatch.setattr(target, name, value, raising=True)


# 执行器端口字段：新实现经 ports 取得，装配在脚本入口调用时读取脚本全局名。
PORT_FIELDS = {"ensure_prerequisites", "run_bilibili_article_search"}
TARGETS = {r["qualname"]: r["target"] for r in ROWS if not r["target"].startswith("scripts/")}


def owner(module, function, name=None):
    """找到函数真正读取全局变量的模块；新侧 T11 函数按台账 target 定位，不经脚本薄包装。"""
    migrated = None
    if module is executor and function in TARGETS and name not in PORT_FIELDS:
        module_name = "trippostcollect." + TARGETS[function].removesuffix(".py").replace("/", ".")
        if importlib.util.find_spec(module_name) is not None:
            migrated = importlib.import_module(module_name)
    # 迁移前目标尚无定义时回落脚本，仅供旧对旧自检；A 组另行要求全部目标存在。
    if migrated is not None and hasattr(migrated, function):
        target = migrated
        implementation = inspect.unwrap(getattr(target, function))
    else:
        implementation = inspect.unwrap(getattr(module, function))
        target = sys.modules[implementation.__module__]
    assert implementation.__globals__ is vars(target)
    return target


def global_reads(code):
    """递归收集代码对象（含嵌套函数、生成器）读取的全局/属性名。"""
    names = set(code.co_names)
    for constant in code.co_consts:
        if inspect.iscode(constant):
            names |= global_reads(constant)
    return names


def binding(monkeypatch, module, function, name, value):
    target = owner(module, function, name)
    reader = inspect.unwrap(getattr(module if target is module else target, function))
    # 死 patch 防线：被替换的名字必须由实际执行的函数读取，否则 mock 不生效。
    assert name in global_reads(reader.__code__), f"patch 不会生效：{target.__name__}.{function} 不读取 {name}"
    strict_patch(monkeypatch, target, name, value)


def forbid(*args, **kwargs):
    pytest.fail("离线驱动漏拦：禁止启动真实进程、浏览器或网络")


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    strict_patch(monkeypatch, subprocess, "Popen", forbid)
    strict_patch(monkeypatch, process, "run_command", forbid)
    strict_patch(monkeypatch, socket.socket, "connect", forbid)
    strict_patch(monkeypatch, socket.socket, "connect_ex", forbid)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 1, 1, 2, 3, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def record_for(platform, number):
    post_id = str(100 + number)
    url = f"https://images.example.test/{platform}/{post_id}.png"
    record = {
        "title": "青岛游记", "content": "青岛海边完整正文", "content_text": "青岛海边完整正文",
        "desc": "青岛海边完整正文", "user_id": "author-1", "nickname": "作者",
        "published_at": "2026-09-20T12:00:00+08:00", "followers_count": 42,
        "followers_observed": True, "liked_count": 3, "comment_count": 2,
        "comments_count": 2, "shared_count": 1, "share_count": 1,
        "collected_count": 4, "view_count": 6, "voteup_count": 3,
        "source_keyword": "青岛旅游", "content_detail_status": "detail_observed",
        "content_detail_source": {"weibo": "mobile_detail", "douyin": "aweme_detail",
                                  "zhihu": "answer_detail", "xhs": "note_detail",
                                  "bilibili": "article_view_api"}[platform],
        "author_followers_source": {"weibo": "search_author", "zhihu": "search_author",
                                    "bilibili": "relation_stat", "xhs": "creator_profile",
                                    "douyin": "creator_profile"}[platform],
    }
    if platform == "bilibili":
        record.update(content_id=post_id, content_type="article", image_urls=[url],
                      content_images_detail_status="detail_observed")
    elif platform == "weibo":
        record.update(note_id=post_id, image_list=[url], image_list_source="mblog.pics")
    elif platform == "douyin":
        record.update(aweme_id=post_id, aweme_type=68, note_download_url=url,
                      image_assets=[{"url": url, "uri": post_id}])
    elif platform == "zhihu":
        record.update(content_id=post_id, content_type="answer", question_id="80", image_list=[url])
    else:
        record.update(note_id=post_id, type="normal", image_list=[{"url_default": url}])
    return record


def behavior_for(platform):
    return {
        "status": "completed", "profile": "xhs_guarded" if platform == "xhs" else "social_high_risk",
        "url": "https://example.test/search?keyword=青岛旅游",
        "events": [{"event": "pause"}, {"event": "mouse_moves"},
                   {"event": "human_scroll_complete", "effective_passes": 1}],
        "page_readiness": {"ready": True},
        "runtime_fingerprint": {"webdriver": None, "languages": ["zh-CN"], "platform": "MacIntel", "visibility_state": "visible",
                                "user_agent": "离线驱动", "viewport": {"width": 1280, "height": 900}},
        "request_pacing_events": [{"stage": s} for s in ("search_results", "note_detail", "creator_profile")],
        "continuity_events": [{"stage": "search_results", "status": "completed"}],
    }


def write_worker_artifacts(platform, save_path, behavior_path, state, scenario):
    """落盘真实 JSONL、manifest 和 PNG；仅替代 worker，不替代正式门禁。"""
    folder = save_path / platform
    folder.mkdir(parents=True)
    records, entries = [], []
    for number in (1, 2):
        record = record_for(platform, number)
        if scenario == "missing_required":
            record.pop("user_id")
        if scenario == "followers_unobserved":
            record["followers_observed"] = False
        records.append(record)
        for candidate in content_image_candidates(platform, record):
            image = io.BytesIO()
            Image.new("RGB", (6, 4), color=(number, 20, 30)).save(image, format="PNG")
            staged = write_staging_image([image.getvalue()], staging_root=folder,
                                         relative_stem=f"images/{number}/{candidate.source_index}")
            entries.append(ImageManifestEntry(
                schema_version=1, platform_key=platform, platform_post_id=candidate.platform_post_id,
                image_role=candidate.image_role, source_index=candidate.source_index,
                source_key=candidate.source_key, source_asset_key=candidate.source_asset_key,
                source_url=candidate.source_url, fetch_status="downloaded", attempts=1, http_status=200,
                staging_path=staged.path.relative_to(folder).as_posix(), size_bytes=staged.size_bytes,
                mime_type=staged.mime_type, width=staged.width, height=staged.height,
                sha256=staged.sha256, error_code=None,
            ))
            if scenario == "missing_image":
                staged.path.unlink()
    (folder / "search_contents_fixed.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    write_manifest_atomic(folder / "image_manifest.jsonl", entries)
    behavior = behavior_for(platform)
    if scenario == "invalid_behavior":
        behavior["events"] = []
    behavior_path.parent.mkdir(parents=True, exist_ok=True)
    behavior_path.write_text(json.dumps(behavior, ensure_ascii=False))
    details = {
        "platform": platform, "batch_no": 1, "candidate_count": 2, "valid_new_count": 2,
        "source_page": 2, "resume_page": 3, "batch_complete": True, "source_has_more": False,
        "candidate_identities": ["101", "102"], "discovery_phase": "frontier",
        "raw_batch_count": 2, "pages_fetched": 2, "stop_reason": "source_exhausted",
        "stop_detail": "has_more_false",
    }
    if scenario.startswith("count_"):
        details["candidate_count"] = int(scenario.split("_")[1])
    if scenario == "many_pages":
        details["pages_fetched"] = details["source_page"] = 100000
    if scenario == "stagnant":
        details["stagnant_batches"] = 100000
    if scenario == "empty_page":
        details["raw_batch_count"] = 0
    if scenario == "unverified_empty_first_page":
        details.update(source_page=1, source_offset=0, source_cursor=None, raw_batch_count=0, stop_detail="empty_page")
    no_stop = scenario in {"missing_stop", "many_pages", "stagnant", "empty_page"} or scenario.startswith("count_")
    if scenario == "pagination_block":
        details.update(stop_reason="runtime_failed", stop_detail="blocked_or_forbidden")
    state.append_event("adaptive_batch_completed", details)
    if not no_stop:
        state.append_event("adaptive_search_stopped", details)
    if scenario == "terminal_block":
        state.append_event("xhs_runtime_terminal", {
            "phase": "search", "failure_type": "platform_security_limit",
            "stop_reason": "runtime_failed", "stop_detail": "安全限制 300011", "retryable": False,
        })
    return behavior


def normalize(value, root):
    """只归一临时绝对路径与运行时钟；主键、内容、图片字节和顺序不变。"""
    if isinstance(value, bytes):
        return value.replace(str(root).encode(), b"<run>")
    if isinstance(value, str):
        value = value.replace(str(root), "<run>")
        return re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?", "<time>", value)
    if isinstance(value, (list, tuple)):
        return [normalize(v, root) for v in value]
    if isinstance(value, dict):
        return {k: normalize(v, root) for k, v in value.items()}
    return value


def database_snapshot(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                  if r[0] in {"web_posts", "web_post_images", "ctf_captures", "ctf_capture_images"}
                  or "discovery" in r[0]]
        snapshot = {}
        for table in tables:
            columns = list(conn.execute(f'PRAGMA table_info("{table}")'))
            primary = [r["name"] for r in sorted(columns, key=lambda r: r["pk"]) if r["pk"]]
            order = ", ".join(f'"{name}"' for name in primary)
            assert order, table
            snapshot[table] = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}')]
    return snapshot


def drive(module, root, patch, capsys, platform="weibo", scenario="success"):
    """旧新两侧共用边界、故障和 SQL 观测；不替换收集、晋升、事务或门禁。"""
    root.mkdir()
    media = root / "temp/media"
    db = root / "posts.sqlite"
    profile = root / "empty-profile"
    profile.mkdir()
    trace, held_locks = [], []
    state = FrozenExecutionState.create(root / "state.json", run_id="t11", job_key="青岛旅游",
                                        site_key=platform, job_kind="mediacrawler_search", plan={}, frozen_inputs=[])
    patch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state.path))
    argv = ["mediacrawler_crawl.py", "--platforms", platform, "--keyword", "青岛旅游",
            "--db", str(db), "--output-dir", str(root / "batches"), "--media-root", str(media),
            "--download-images", "--xhs-profile-dir", str(profile)]
    if platform == "xhs":
        argv += ["--xhs-account-id", "offline", "--xhs-discovery-target-key", "青岛旅游",
                 "--xhs-discovery-query-fingerprint", "query", "--behavior-profile", "xhs_guarded",
                 "--login-type", "qrcode"]
    else:
        argv += ["--discovery-job-id", "1", "--discovery-query-fingerprint", "query", "--discovery-run-id", "t11"]
    strict_patch(patch, sys, "argv", argv)
    args = module.parse_args()
    for name in ("resume_summary", "zhihu_detail_urls_file", "xhs_detail_urls_file",
                 "xhs_repair_target_ids_file", "repair_targets_file"):
        assert getattr(args, name) is None
    with sqlite3.connect(db) as conn:
        bootstrap_connection(conn, sync_jobs=False)
        conn.execute("INSERT INTO crawl_jobs (id, job_key, site_key, target_url, job_kind, next_run_at) VALUES (1, ?, ?, '', 'mediacrawler_search', '2026-10-01')", ("青岛旅游", platform))
    # 只替换函数体实际读取的全局名；默认参数在定义时已绑定，替换模块全局不会生效，不计为读取。
    for function in ("_run_main", "materialize_formal_record_images", "import_valid_records"):
        for name, replacement in (("PROJECT_ROOT", root), ("datetime", FixedDatetime)):
            if name in global_reads(inspect.unwrap(getattr(owner(module, function), function)).__code__):
                binding(patch, module, function, name, replacement)
    for function in ("load_pagination_evidence", "summarize_output"):
        binding(patch, module, function, "time", SimpleNamespace(monotonic=lambda: 100.0, time=lambda: 1790816523.0, sleep=forbid))
    binding(patch, module, "_run_main", "ensure_prerequisites", lambda: None)
    resolve = getattr(owner(module, "_run_main"), "resolve_media_root")
    binding(patch, module, "_run_main", "resolve_media_root",
            lambda value: resolve(value, project_root=root, default_media_root=media))
    binding(patch, module, "_run_main", "utc_stamp", lambda: "fixed")
    binding(patch, module, "_run_platform_without_policy", "ROOT", root)
    binding(patch, module, "_run_platform_without_policy", "discover_cdp_browser_path", lambda: None)
    binding(patch, module, "_run_platform_without_policy", "export_profile_cookies",
            lambda *a: {"cookie_header": "d_c0=test;z_c0=test", "source": "offline"})
    binding(patch, module, "_run_platform_without_policy", "cookie_snapshot_path", lambda *a: root / "snapshot.json")
    binding(patch, module, "run_platform", "clear_site_policy_state", lambda *a: None)
    binding(patch, module, "run_platform", "record_site_cooldown", forbid)
    strict_patch(patch, random, "uniform", lambda low, high: (low + high) / 2)

    @contextmanager
    def policy(*a, **kw):
        yield {"allowed": True, "disabled": platform == "xhs"}

    binding(patch, module, "run_platform", "site_request_guard", policy)
    lock = getattr(owner(module, "_run_main"), "formal_media_persistence_lock")

    @contextmanager
    def observed_lock(*, enabled, progress_callback=None):
        acquired = False
        try:
            with lock(enabled=enabled, lock_path=root / "media.lock", progress_callback=progress_callback):
                acquired = enabled
                if enabled:
                    trace.append("media.acquire")
                yield
        finally:
            if acquired:
                trace.append("media.release")

    binding(patch, module, "_run_main", "formal_media_persistence_lock", observed_lock)
    db_owner = owner(module, "import_valid_records")
    connect = db_owner.connect_db

    def observed_connect(*a, **kw):
        conn = connect(*a, **kw)
        trace.append(("sqlite.busy_timeout", conn.execute("PRAGMA busy_timeout").fetchone()[0]))
        conn.set_trace_callback(lambda sql: trace.append(sql) if sql in {"BEGIN", "BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"} else None)
        return conn

    # 内容事务与发现提交各自读取 connect_db；两处都挂同一观测，保证 ContentCommit/DiscoveryCommit 顺序可比。
    for function in ("import_valid_records", "persist_discovery_checkpoint"):
        binding(patch, module, function, "connect_db", observed_connect)
    upsert = db_owner.upsert_web_post
    calls = 0
    failure = module.XhsRuntimeSupervisionError("监督失败") if scenario == "supervision" else RuntimeError("提交探测失败")

    def observed_upsert(*a, **kw):
        nonlocal calls
        calls += 1
        trace.append(f"content.upsert.{calls}")
        result = upsert(*a, **kw)
        if calls == 2 and scenario in {"upsert_failure", "supervision"}:
            raise failure
        return result

    strict_patch(patch, db_owner, "upsert_web_post", observed_upsert)
    commit = db_owner.commit_formal_import

    def observed_commit(conn):
        trace.append("content.commit.enter")
        if scenario != "commit_before":
            commit(conn)
        trace.append(f"content.in_transaction.{conn.in_transaction}")
        if scenario in {"commit_before", "commit_after"}:
            raise failure
        trace.append("content.commit.exit")

    strict_patch(patch, db_owner, "commit_formal_import", observed_commit)
    imported = getattr(owner(module, "import_valid_records_with_media_rollback"), "import_valid_records")

    def observed_import(*a, **kw):
        try:
            return imported(*a, **kw)
        except BaseException as exc:
            trace.append(("content.exception", type(exc).__name__, exc is failure))
            raise

    binding(patch, module, "import_valid_records_with_media_rollback", "import_valid_records", observed_import)
    if scenario == "sqlite_busy":
        schema = db_owner.ensure_web_schema

        def busy_schema(conn):
            result = schema(conn)
            holder = sqlite3.connect(db)
            holder.execute("BEGIN IMMEDIATE")
            held_locks.append(holder)
            trace.append("other.begin_immediate")
            return result

        strict_patch(patch, db_owner, "ensure_web_schema", busy_schema)
        strict_patch(patch, db_owner, "FORMAL_SQLITE_BUSY_TIMEOUT_MS", 10)
    promotion_owner = owner(module, "materialize_formal_record_images")
    promote = promotion_owner.promote_validated_image
    promotions = 0

    def observed_promote(*a, **kw):
        nonlocal promotions
        promotions += 1
        trace.append(f"media.promote.{promotions}")
        if promotions == 2 and scenario == "promotion_failure":
            raise OSError("晋升失败")
        return promote(*a, **kw)

    strict_patch(patch, promotion_owner, "promote_validated_image", observed_promote)
    persist = getattr(owner(module, "_run_main"), "persist_discovery_checkpoint")

    def observed_persist(*a, **kw):
        trace.append("discovery.enter")
        if scenario == "discovery_failure":
            raise sqlite3.OperationalError("发现提交失败")
        result = persist(*a, **kw)
        trace.append("discovery.exit")
        return result

    binding(patch, module, "_run_main", "persist_discovery_checkpoint", observed_persist)

    def fake_command(cmd, cwd, timeout, log_dir, **kw):
        assert Path(cwd) == root
        assert cmd[1:4] == ["-P", "-m", "trippostcollect.platforms.entry"]
        save_path = Path(cmd[cmd.index("--save_data_path") + 1])
        assert save_path.is_relative_to(root)
        assert kw["extra_env"]["TRIPPOSTCOLLECT_DB_PATH"] == str(db)
        trace.append("worker.run")
        write_worker_artifacts(platform, save_path, log_dir / "behavior_evidence.json", state, scenario)
        return {"returncode": 0, "timed_out": False, "stdout_tail": "", "stderr_tail": ""}

    binding(patch, module, "_run_platform_without_policy", "run_command", fake_command)
    if platform == "bilibili":
        # B站没有 worker；保持执行器转发与 article search 本体，只替换会话/请求边界。
        async def fake_behavior(args, evidence_path):
            trace.append("bilibili.behavior")
            evidence = behavior_for("bilibili")
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False))
            return {"cookie_header": "SESSDATA=offline"}, evidence

        def fake_page(keyword, page, **kwargs):
            trace.append(("bilibili.page", page))
            return [{"id": str(100 + n), "title": "青岛游记", "desc": "青岛搜索摘要",
                     "pubdate": 1789876800, "like": 3, "reply": 2, "view": 6,
                     "author": "作者", "mid": "author-1"} for n in (1, 2)] if page == 1 else []

        def fake_detail(post_id, cookie_header):
            trace.append(("bilibili.detail", post_id))
            return {"title": "青岛游记", "content": f'<p>青岛海边完整正文</p><img src="https://i0.hdslb.com/bfs/article/{post_id}.png">'}, 1, 0.0

        def fake_image(url, post_id, cookie_header):
            trace.append(("bilibili.image", post_id))
            buffer = io.BytesIO()
            Image.new("RGB", (6, 4), color=(int(post_id) - 100, 20, 30)).save(buffer, format="PNG")
            return SimpleNamespace(content=buffer.getvalue(), http_status=200, media_type="image/png", final_url=url)

        binding(patch, module, "run_bilibili_article_search", "run_bilibili_behavior_session", fake_behavior)
        binding(patch, module, "download_bilibili_record_images", "fetch_bilibili_image_bytes", fake_image)
        for name, replacement in {
            "fetch_bilibili_wbi_keys": lambda *a: ("a", "b"),
            "fetch_bilibili_article_page": fake_page,
            "fetch_bilibili_article_detail_with_retry": fake_detail,
            "fetch_bilibili_follower_count": lambda *a: 42,
            "datetime": FixedDatetime,
            "time": SimpleNamespace(monotonic=lambda: 100.0, sleep=lambda delay: trace.append(("sleep", delay))),
        }.items():
            strict_patch(patch, bilibili_core, name, replacement)
    capsys.readouterr()
    try:
        result = module._run_main(args, None)
    except (RuntimeError, sqlite3.Error) as exc:
        result = {"exception": type(exc).__name__, "message": str(exc), "original": exc is failure}
    finally:
        for holder in held_locks:
            holder.rollback()
            holder.close()
    stdout, stderr = capsys.readouterr()
    summary_path = root / "batches/fixed/summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else None
    envelope = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    files = {p.relative_to(media).as_posix(): p.read_bytes() for p in media.rglob("*") if p.is_file()}
    dirs = sorted(p.relative_to(media).as_posix() for p in media.rglob("*") if p.is_dir())
    markdown = summary_path.with_suffix(".md")
    snapshot = database_snapshot(db)
    for row in snapshot["web_posts"]:
        assert row["published_at"] == "2026-09-20T12:00:00+08:00"
    for row in snapshot["web_post_images"]:
        assert sha256((root / row["local_path"]).read_bytes()).hexdigest() == row["sha256"]
    return normalize({"result": result, "trace": trace, "database": snapshot,
                      "media": files, "media_dirs": dirs, "summary": summary,
                      "markdown": normalize(markdown.read_text(), root).encode() if markdown.exists() else None,
                      "envelope": envelope, "state": json.loads(state.path.read_text()),
                      "stderr": stderr}, root)


def paired(baseline, tmp_path, monkeypatch, capsys, platform, scenario):
    values = []
    for label, module in (("old", baseline), ("new", executor)):
        with monkeypatch.context() as patch:
            values.append(drive(module, tmp_path / label, patch, capsys, platform, scenario))
    assert values[0] == values[1]
    return values


@pytest.mark.parametrize("platform", ["bilibili", "weibo", "douyin", "zhihu", "xhs"])
def test_b_full_chain(baseline, tmp_path, monkeypatch, capsys, platform):
    for value in paired(baseline, tmp_path, monkeypatch, capsys, platform, "success"):
        assert value["result"] == 0, {
            "validation": value["summary"]["formal_validation"]["invalid_reason_counts"],
            "stderr": value["summary"]["records"][-1]["run"].get("stderr_tail"),
            "images": value["summary"]["image_materialization"]["failures"],
        }
        assert value["summary"]["import_completion_met"] is True
        assert len(value["database"]["web_posts"]) == len(value["database"]["web_post_images"]) == len(value["media"]) == 2
        assert value["database"]["ctf_captures"] == []
        if platform != "xhs":
            assert len(value["database"]["crawl_discovery_checkpoints"]) == 1
            assert len(value["database"]["crawl_discovery_seen_candidates"]) == 2
        trace = value["trace"]
        assert ("worker.run" in trace) is (platform != "bilibili")
        assert ("bilibili.behavior" in trace) is (platform == "bilibili")
        assert trace.index("media.acquire") < trace.index("media.promote.1") < trace.index("BEGIN IMMEDIATE")
        assert trace.index("BEGIN IMMEDIATE") < trace.index("content.commit.enter") < trace.index("content.commit.exit")
        assert trace.index("content.commit.exit") < trace.index("media.release") < trace.index("discovery.enter")


@pytest.mark.parametrize("scenario", ["upsert_failure", "commit_before", "commit_after", "promotion_failure",
                                      "sqlite_busy", "supervision", "discovery_failure"])
def test_c_transaction_faults(baseline, tmp_path, monkeypatch, capsys, scenario):
    for value in paired(baseline, tmp_path, monkeypatch, capsys, "xhs" if scenario == "supervision" else "weibo", scenario):
        committed = scenario in {"commit_after", "discovery_failure"}
        assert len(value["database"]["web_posts"]) == (2 if committed else 0)
        assert len(value["database"]["web_post_images"]) == (2 if committed else 0)
        assert len(value["media"]) == (2 if committed or scenario == "supervision" else 0)
        assert value["database"]["crawl_discovery_checkpoints"] == [] or scenario == "promotion_failure"
        trace = value["trace"]
        if scenario in {"upsert_failure", "commit_before", "sqlite_busy"}:
            assert ["content.exception", "FormalImportBeforeCommitError", False] in trace
            assert value["summary"]["import_result"]["reason"] == "sqlite_import_failed"
            assert value["summary"]["image_materialization"]["rolled_back_images"] == 2
            assert "discovery.enter" not in trace
            assert value["result"] == 2
        if scenario in {"upsert_failure", "commit_before", "supervision"}:
            assert "ROLLBACK" in trace
        if scenario in {"commit_after", "supervision"}:
            assert value["result"]["original"] is True
            assert value["result"]["exception"] == ("RuntimeError" if committed else "XhsRuntimeSupervisionError")
            assert "discovery.enter" not in trace
        if scenario == "commit_after":
            assert "content.in_transaction.False" in trace and "ROLLBACK" not in trace
        if scenario == "commit_before":
            assert "content.in_transaction.True" in trace
        if scenario == "sqlite_busy":
            assert ["sqlite.busy_timeout", 10] in trace
            assert "other.begin_immediate" in trace and "BEGIN IMMEDIATE" in trace
            assert "content.upsert.1" not in trace
            assert "database is locked" in value["summary"]["import_result"]["error"]
        if scenario == "promotion_failure":
            assert value["summary"]["image_materialization"]["rolled_back_images"] == 1
            assert "content.commit.enter" not in trace
        if scenario == "discovery_failure":
            assert value["result"] == 2
            assert value["summary"]["failure_reason"] == "discovery_checkpoint_write_failed"
            assert trace.index("content.commit.exit") < trace.index("discovery.enter")


@pytest.mark.parametrize("scenario", ["missing_stop", "missing_required", "followers_unobserved",
                                      "invalid_behavior", "missing_image", "terminal_block", "pagination_block"])
def test_c_gates_and_blockers(baseline, tmp_path, monkeypatch, capsys, scenario):
    for value in paired(baseline, tmp_path, monkeypatch, capsys, "xhs" if scenario == "terminal_block" else "weibo", scenario):
        summary = value["summary"]
        assert value["database"]["web_posts"] == [] and value["media"] == {}
        validation = summary["formal_validation"]
        if scenario in {"missing_required", "followers_unobserved"}:
            reason = "missing_author_id" if scenario == "missing_required" else "followers_not_observed"
            assert validation["invalid_reason_counts"][reason] == 2
        else:
            assert value["result"] == 2 and summary["import_completion_met"] is False
        if scenario in {"terminal_block", "pagination_block"}:
            assert validation["stop_reason"] == "runtime_failed"
            assert summary["runtime_blocker"]["reason"] == ("安全限制 300011" if scenario == "terminal_block" else "blocked_or_forbidden")
            assert validation["stop_detail"] == summary["runtime_blocker"]["reason"]


@pytest.mark.parametrize("scenario", ["success", "missing_stop", "count_0", "count_1", "count_1000000",
                                      "many_pages", "stagnant", "empty_page", "unverified_empty_first_page"])
def test_d_completion_requires_stop_evidence(baseline, tmp_path, monkeypatch, capsys, scenario):
    platform = "douyin" if scenario == "unverified_empty_first_page" else "weibo"
    for value in paired(baseline, tmp_path, monkeypatch, capsys, platform, scenario):
        summary = value["summary"]
        completed = scenario == "success"
        events = value["state"]["events"]
        has_stop = any(e["type"] == "adaptive_search_stopped" and e["details"].get("stop_reason") == "source_exhausted" for e in events)
        assert has_stop is (scenario in {"success", "unverified_empty_first_page"})
        assert summary["formal_validation"]["source_exhausted_met"] is completed
        assert summary["formal_validation"]["completion_met"] is completed
        assert summary["import_completion_met"] is completed
        assert value["result"] == (0 if completed else 2)
        assert value["envelope"][-1]["status"] == ("completed" if completed else "failed")


def test_guard_and_strict_patch(monkeypatch):
    with pytest.raises(AssertionError, match="patch 目标不存在"):
        strict_patch(monkeypatch, process, "不存在的入口", forbid)
    for operation in (lambda: subprocess.Popen(["never"]), lambda: process.run_command([])):
        with pytest.raises(pytest.fail.Exception, match="离线驱动漏拦"):
            operation()
