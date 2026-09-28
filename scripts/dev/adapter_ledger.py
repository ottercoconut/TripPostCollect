"""生成平台适配迁移台账；只做静态分析、只读基线查询和临时副本测试收集。"""

from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from adapter_ledger_rules import (
    ENTRYPOINTS,
    FORK_EXCLUDED_DIRS,
    FORK_EXCLUDED_PLATFORMS,
    FORK_PREFIX,
    RESOURCE_FILES,
    SPEC_MERGE_BASE,
    SYMBOL_ROOT_FILES,
    SYMBOL_RULES,
    TEST_FILE_RULES,
)

ROOT = Path(__file__).resolve().parents[2]
LEDGER_DIR = Path("docs/adapter-ledger")
SYMBOL_MARKDOWN = Path("docs/platform-adapter-symbol-ledger.md")
DEFINITION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
ENV_NAME = re.compile(r"\b(?:TRIPPOSTCOLLECT_[A-Z0-9_]+|TRIPPOST_PROJECT_ROOT)\b")


def json_text(value):
    """统一产物编码、缩进、键顺序及末尾换行。"""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def check_file(path, expected):
    """按 UTF-8 字节比较；缺失的产物同样视为过期。"""
    path = Path(path)
    return path.is_file() and path.read_bytes() == expected.encode("utf-8")


def publish(path, content, check=False):
    if check:
        matches = check_file(path, content)
        if not matches:
            print(f"产物缺失或过期：{path}")
        return matches
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    print(f"已生成：{path.relative_to(ROOT)}")
    return True


def fork_python_files(root):
    """先裁剪无关目录再枚举，避免遍历虚拟环境与运行产物。"""
    fork = root / FORK_PREFIX
    for directory, dirs, names in os.walk(fork, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in FORK_EXCLUDED_DIRS
            and not any(platform in name for platform in FORK_EXCLUDED_PLATFORMS)
            and not (Path(directory) / name).is_symlink()
        )
        for name in sorted(names):
            path = Path(directory) / name
            relative = path.relative_to(fork).as_posix()
            if (
                path.suffix == ".py"
                and not path.is_symlink()
                and not any(platform in relative for platform in FORK_EXCLUDED_PLATFORMS)
            ):
                yield path.relative_to(root).as_posix()


def symbol_files(root):
    files = list(SYMBOL_ROOT_FILES)
    for relative in sorted(fork_python_files(root)):
        local = relative.removeprefix(FORK_PREFIX)
        if local.startswith(("config/", "constant/")) or local == "var.py":
            continue
        if local.endswith("__init__.py") and not local.startswith("store/"):
            continue
        files.append(relative)
    return files


def definitions(path):
    """与原型一致：只枚举顶层定义及类的直接方法，不枚举局部函数。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, DEFINITION_TYPES):
            yield node.name, node
            if isinstance(node, ast.ClassDef):
                for method in node.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        yield f"{node.name}.{method.name}", method


def build_symbols(root, files=None):
    root = Path(root)
    rows, unmapped, nodes = [], [], []
    rules = [(files.split("|"), re.compile(pattern), rest) for files, pattern, *rest in SYMBOL_RULES]
    for relative in symbol_files(root) if files is None else files:
        for qualname, node in definitions(root / relative):
            row = {"file": relative, "line": node.lineno, "qualname": qualname}
            for matches, pattern, values in rules:
                if relative in matches and pattern.fullmatch(qualname):
                    row.update(zip(("target", "disposition", "card", "tests", "note"), values))
                    rows.append(row)
                    nodes.append((row, node))
                    break
            else:
                unmapped.append(row)
    exited = {row["qualname"].split(".")[-1] for row in rows if row["disposition"] == "退"}
    kept = {row["qualname"].split(".")[-1] for row in rows if row["disposition"] != "退"}
    references = collections.defaultdict(set)
    for row, node in nodes:
        if row["disposition"] == "退":
            continue
        for child in ast.walk(node):
            name = (
                child.id if isinstance(child, ast.Name) else (child.attr if isinstance(child, ast.Attribute) else None)
            )
            if name in exited - kept:
                references[name].add(f"{row['file'].replace(FORK_PREFIX, 'M/')}:{row['qualname']}")
    stat = dict(collections.Counter(row["disposition"] for row in rows))
    stat["total"] = len(rows)
    return {
        "rows": rows,
        "unmapped": unmapped,
        "stat": stat,
        "exit_references": [
            {"symbol": name, "referenced_by": sorted(refs)} for name, refs in sorted(references.items())
        ],
    }


def build_inputs(root):
    root = Path(root)
    cli = {}
    for relative in ENTRYPOINTS:
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        arguments = []
        for node in sorted(
            ast.walk(tree), key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        ):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
            ):
                continue
            flags = [
                arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else ast.unparse(arg)
                for arg in node.args
            ]
            argument = {"flags": flags}
            for keyword in node.keywords:
                if keyword.arg in {"default", "type", "choices", "action", "required", "nargs"}:
                    argument[keyword.arg] = ast.unparse(keyword.value)
            arguments.append(argument)
        cli[relative] = arguments
    files = {
        path.relative_to(root).as_posix() for pattern in ("scripts/*.py", "src/**/*.py") for path in root.glob(pattern)
    }
    files.update(fork_python_files(root))
    env = collections.defaultdict(set)
    for relative in sorted(files):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for name in ENV_NAME.findall(node.value):
                    env[name].add(relative)
    return {"cli": cli, "env": {name: {"files": sorted(paths)} for name, paths in sorted(env.items())}}


def git_read(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"只读 Git 查询失败：{root}，{' '.join(args)}")
    return result.stdout.strip()


def build_baseline(root):
    root = Path(root)
    fork, upstream = root / FORK_PREFIX, root.parent / "MediaCrawler-upstream"
    if not upstream.is_dir():
        raise RuntimeError(f"上游只读副本不存在：{upstream}")
    upstream_head = git_read(upstream, "rev-parse", "HEAD")
    baseline = {
        "root_head": git_read(root, "rev-parse", "HEAD"),
        "fork_head": git_read(fork, "rev-parse", "HEAD"),
        "upstream_head": upstream_head,
        "resources": {name: hashlib.sha256((fork / name).read_bytes()).hexdigest() for name in RESOURCE_FILES},
    }
    object_check = subprocess.run(
        ["git", "-C", str(fork), "cat-file", "-e", f"{upstream_head}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if object_check.returncode:
        baseline["fork_upstream_merge_base"] = SPEC_MERGE_BASE
        baseline["note"] = "fork 对象库没有上游 HEAD 对象；分叉点回退为规格 A 节固定值，未拉取或写入 Git。"
    else:
        baseline["fork_upstream_merge_base"] = git_read(fork, "merge-base", "HEAD", upstream_head)
    return baseline


def load_matrix():
    """只加载 CI 的定义，复用副本白名单、fork 文件清单和导入路径规则。"""
    spec = importlib.util.spec_from_file_location("adapter_ledger_matrix", ROOT / "scripts/ci/run_matrix.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def temporary_source(path):
    source = Path(path).resolve()
    if source == ROOT.resolve():
        raise SystemExit("拒绝在生产 checkout 收集测试；请先使用 make-source 创建临时源码副本。")
    parents = {Path("/private/tmp").resolve(), Path(tempfile.gettempdir()).resolve()}
    if not any(source != parent and source.is_relative_to(parent) for parent in parents):
        raise SystemExit("源码副本必须位于 /private/tmp 或 tempfile.gettempdir() 下。")
    return source


COLLECTION_PLUGIN = '''"""仅记录收集结果，不执行测试或夹具。"""
import json
import os
from pathlib import Path


def pytest_collection_finish(session):
    nodes = [
        {"node_id": item.nodeid, "markers": sorted({marker.name for marker in item.iter_markers()})}
        for item in session.items
    ]
    Path(os.environ["ADAPTER_LEDGER_COLLECTION"]).write_text(
        json.dumps(nodes, ensure_ascii=False, indent=2, sort_keys=True) + "\\n", encoding="utf-8",
    )
'''


def collect_nodes(source, side, matrix, support):
    fork = source / FORK_PREFIX
    interpreter = ROOT / (".venv/bin/python" if side == "root" else f"{FORK_PREFIX}.venv/bin/python")
    cwd = source if side == "root" else fork
    output = support / f"{side}.json"
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    environment.update(
        {
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TRIPPOST_PROJECT_ROOT": str(source),
            "ADAPTER_LEDGER_COLLECTION": str(output),
            "PYTHONPATH": os.pathsep.join(
                str(path)
                for path in (
                    source / "src",
                    source / "scripts",
                    source / "tests",
                    support,
                )
            )
            if side == "root"
            else matrix.fork_pythonpath(source, support),
        }
    )
    command = [str(interpreter), "-m", "pytest"]
    if side == "root":
        command += ["tests"]
    else:
        command += [
            *matrix.FORK_OFFLINE_TESTS,
            "-c",
            str(fork / "pyproject.toml"),
            "--rootdir",
            str(fork),
            "--confcutdir",
            str(fork),
        ]
    command += [
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "pytest_asyncio.plugin",
        "-p",
        "adapter_ledger_collection",
    ]
    result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True, check=False, timeout=180)
    if result.returncode:
        # 收集失败只展示尾部，避免把全量节点或导入日志展开。
        detail = "\n".join((result.stdout + result.stderr).splitlines()[-40:])
        raise RuntimeError(f"{side} 测试收集失败（退出码 {result.returncode}）：\n{detail}")
    nodes = json.loads(output.read_text(encoding="utf-8"))
    if not nodes:
        raise RuntimeError(f"{side} 测试收集为空")
    return nodes


def build_tests(source):
    source = temporary_source(source)
    matrix = load_matrix()
    files = {path.relative_to(source).as_posix() for path in (source / "tests").rglob("test_*.py")}
    files.update(FORK_PREFIX + name for name in matrix.FORK_OFFLINE_TESTS)
    missing = sorted(files - TEST_FILE_RULES.keys())
    if missing:
        raise RuntimeError("以下测试文件没有迁移规则：\n" + "\n".join(missing))
    result = {"collected_at_root_head": git_read(ROOT, "rev-parse", "HEAD")}
    with tempfile.TemporaryDirectory(prefix="adapter-ledger-plugin-") as directory:
        support = Path(directory)
        (support / "adapter_ledger_collection.py").write_text(COLLECTION_PLUGIN, encoding="utf-8")
        for side in ("root", "fork"):
            nodes = collect_nodes(source, side, matrix, support)
            for node in nodes:
                source_file = (FORK_PREFIX if side == "fork" else "") + node["node_id"].split("::", 1)[0]
                if source_file not in TEST_FILE_RULES:
                    raise RuntimeError(f"测试文件没有迁移规则：{source_file}")
                lane = (
                    "fork"
                    if side == "fork"
                    else next(
                        (
                            lane
                            for marker, lane in (
                                ("macos_process", "os"),
                                ("local_socket", "socket"),
                                ("installation", "installation"),
                            )
                            if marker in node["markers"]
                        ),
                        "component",
                    )
                )
                rule = TEST_FILE_RULES[source_file]
                target = rule.get("target_by_lane", {}).get(lane, rule["target_file"])
                node.update(
                    source_file=source_file, target_file=target, lane=lane, protects=rule["protects"], card=rule["card"]
                )
            result[side] = {"nodes": sorted(nodes, key=lambda node: node["node_id"])}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, title="子命令")
    for name in ("symbols", "inputs", "baseline", "all"):
        subparser = commands.add_parser(name, help="生成或校验迁移台账")
        subparser.add_argument("--check", action="store_true", help="只比较磁盘产物，不写文件")
    collector = commands.add_parser("tests", help="在临时源码副本中收集测试节点")
    collector.add_argument("--source", required=True, type=Path, help="临时源码副本目录")
    copier = commands.add_parser("make-source", help="复制源码白名单，排除运行产物和虚拟环境")
    copier.add_argument("destination", type=Path, help="尚不存在的临时目录")
    args = parser.parse_args(argv)
    try:
        if args.command == "make-source":
            source = temporary_source(args.destination)
            load_matrix().fresh_source(ROOT, source)
            print(f"已创建临时源码副本：{source}")
            return 0
        if args.command == "tests":
            result = build_tests(args.source)
            publish(ROOT / LEDGER_DIR / "tests.json", json_text(result))
            for side in ("root", "fork"):
                counts = dict(collections.Counter(node["lane"] for node in result[side]["nodes"]))
                print(f"{side} 收集 {len(result[side]['nodes'])} 个节点，分组：{counts}")
            return 0
        success = True
        for command in ("baseline", "symbols", "inputs") if args.command == "all" else (args.command,):
            result = {"baseline": build_baseline, "symbols": build_symbols, "inputs": build_inputs}[command](ROOT)
            if command == "symbols" and result["unmapped"]:
                print("以下定义没有处置规则：")
                for row in result["unmapped"]:
                    print(f"{row['file']}:{row['line']} {row['qualname']}")
                return 1
            success = publish(ROOT / LEDGER_DIR / f"{command}.json", json_text(result), args.check) and success
            if command == "symbols":
                success = publish(ROOT / SYMBOL_MARKDOWN, render_symbols(result), args.check) and success
        return 0 if success else 1
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"台账生成失败：{error}")
        return 1


def render_symbols(result):
    """沿用 C8 原型的顺序和表格格式；JSON 保存全部引用，表格保持原五处样本。"""
    files = {}
    for row in result["rows"]:
        files.setdefault(row["file"], []).append(
            tuple(
                row[key]
                for key in (
                    "line",
                    "qualname",
                    "target",
                    "disposition",
                    "card",
                    "tests",
                    "note",
                )
            )
        )
    L = {"files": files, "stat": {key: value for key, value in result["stat"].items() if key != "total"}}
    viol = [f"{item['symbol']} <- {item['referenced_by'][:5]!r}" for item in result["exit_references"]]
    M = "tools/MediaCrawler/"

    def short(f):
        return f.replace(M, "M/")

    lines = []
    w = lines.append
    w("# 平台适配附录 C8：全量符号处置账 v0.7")
    w("")
    w("> 本附录是[详细迁移规格](platform-adapter-specification.md) C 节的补充，逐个列出迁移闭包内每个")
    w("> 顶层函数、类及类方法的目标位置和处置。目标路径均未实现；当前运行仍遵循[正式契约](formal-crawl-contract.md)。")
    w("")
    w("## 基线与生成方式")
    w("")
    w("| 项 | 值 |")
    w("|---|---|")
    w("| 根源码 | `fe3e28ac7cc9575968e3279dd0e1c60ad0b7b1c1` |")
    w("| fork | `tools/MediaCrawler` @ `2bcde689cc9a1b50cbcc7255597e9da1ff1a1b30` |")
    w(
        "| 上游对照 | `NanmiCoder/MediaCrawler` @ `380b426000aac3d612837ed72c99808347dc94c9`（2026-09-19）；与 fork 分叉点 `d6f7c5bb906b6dac40ddf343ef9e26438a3de092` |"
    )
    w(
        "| 生成方式 | 对下列文件做 AST 枚举（`def`/`async def`/`class` 及类内方法），逐项匹配处置规则；任何未匹配项使生成失败 |"
    )
    st = L["stat"]
    total = sum(st.values())
    w(
        f"| 规模 | {len(L['files'])} 个文件、{total} 个定义：迁 {st.get('迁', 0)}、拆 {st.get('拆', 0)}、并 {st.get('并', 0)}、薄 {st.get('薄', 0)}、退 {st.get('退', 0)} |"
    )
    w("")
    w("范围：执行器 C、私有桥 E、行为/人类流程、两个 warmup、四个被执行器导入的共享脚本（失败分类、节流策略、")
    w("浏览器运行时、执行状态）、页面证据就绪模块，以及 fork 中微博/抖音/知乎/小红书四站 `media_platform`、`store`、")
    w("`tools`、`model`、`cache`、`proxy`、`database`、`base`、`cmd_arg`、`main.py`、`recv_sms.py`。")
    w(
        "不含：`config/*`、`constant/*`、`var.py`（无函数定义，按 D1 的逐键规则处置）；B站/快手/贴吧的上游平台目录（整体退出，"
    )
    w(
        "见 C7）；fork 的 `api/`、`webui/`、`docs/`、`tests/`（测试按 F 节迁移）；根项目 `src/trippostcollect` 现有模块（原位保留，见 C0）。"
    )
    w("")
    w("## 处置含义")
    w("")
    w("| 处置 | 含义 | 删除旧定义的前提 |")
    w("|---|---|---|")
    w(
        "| 迁 | 原样移动到目标模块；调用顺序、次数、异常、wire 不变，只改导入路径 | 所有调用方切到新位置，旧位置无静态/动态引用 |"
    )
    w(
        "| 拆 | 同一定义里的纯逻辑、IO、全局 config/env 读取或 monkeypatch 分别归位 | 新显式出口的负例测试先通过，再删旧 hook |"
    )
    w("| 并 | 与另一处同名同义定义合并为唯一实现 | 逐项比对两份实现一致；不一致时保留差异为显式参数，不擅自取其一 |")
    w("| 薄 | 保留外部命令名，函数体只转发到包内入口 | 外部 CLI 与冻结命令兼容 |")
    w(
        "| 退 | 不进入目标包 | 下文“退出切片的调用点切断”中列出的所有活跃调用点先改为显式不可达或删除；T12 静态/动态引用检查为零，T14 才删除文件 |"
    )
    w("")
    w("目标路径均相对 `src/trippostcollect/`，`scripts/...` 除外。“卡”指详细规格 G 的任务卡，“测”指 F 节的测试责任。")
    w("")
    w("## 本附录对详细规格的新增约束")
    w("")
    w("以下各项由全量枚举发现，已同步写入详细规格 B/C/D；此处集中列出，便于核对。")
    w("")
    w("| 编号 | 发现 | 约束 |")
    w("|---|---|---|")
    w(
        "| X1 | `row_for_record`、`inject_materialized_images` 需要 `artifacts.image_materialization.MaterializedImage` 类型 | `records/formal.py` 只在 `TYPE_CHECKING` 下导入该类型，不形成运行期 records→artifacts 依赖；依赖矩阵登记为唯一例外 |"
    )
    w(
        "| X2 | 四站 store 投影直接调用 `image_manifest` 的 `*_source_asset_key`、`normalize_image_url` 与 `upsert_manifest_rows_atomic` | 稳定键函数归各站 parser，`normalize_image_url` 归 runtime/helpers；staging 写出经 `contracts.ImageStager` 端口，平台不直接 import artifacts |"
    )
    w(
        "| X3 | 四站 `*_store_media.py` 是同构的整帖 staging 实现 | T04 先做四份逐行差分，一致部分合并为 `artifacts/image_staging.py` 单实现，差异项（source_key、asset key 函数）作为参数 |"
    )
    w(
        "| X4 | `should_reseed_douyin_frontier` 由抖音 core 调用，决定是否开启新的游标纪元 | 归 `application/candidates.py`，经 `CandidateDecisions` 端口新增只读方法供抖音调用；判定条件逐字不变 |"
    )
    w(
        '| X5 | `AdaptiveAccumulator.from_environment` 对微博使用 `stagnation_basis="candidate_identity"` | 构造时显式传入；停滞计数仍不作停止条件，但事件字段 `stagnation_basis` 值不变 |'
    )
    w(
        "| X6 | `env_int`、各站 `_env_float` 在操作起点读 env | 统一由 `application/worker_inputs.py` 解析为零参 reader，读取时刻与解析失败回默认值的行为不变 |"
    )
    w(
        "| X7 | C 与 W 各有 `profile_dir_for`/`cookie_snapshot_path`/`required_cookie_names`，平台代号字典键名不同（`mediacrawler` vs `code`） | T00 已核：共有平台代号一致（dy/zhihu/wb/bili，W 不含 xhs）；T01 合并为 `core.paths` 与 `runtime/cookies.py` |"
    )
    w(
        "| X8 | `repair_bilibili_articles.py` 从 C 导入 6 个符号；`mediacrawler_login_warmup.py` 导入 `discover_cdp_browser_path` | T08/T10 删除 C 旧函数前，两脚本改为从包内新位置导入 |"
    )
    w(
        "| X9 | `XhsRuntimeSupervisionError` 被执行器事务代码按类型捕获并回滚 | 类型定义进 `application/contracts.py`，`db/content.py` 与 `xhs/supervision.py` 都从 contracts 导入，避免 db→xhs 依赖 |"
    )
    w(
        "| X10 | C 的 `is_retryable_image_error`/`is_runtime_blocking_image_error` 与 fork `image_download_retry` 同名同常量 | 合并为 `runtime/image_retry.py` 单一实现；合并前比对两组错误码集合 |"
    )
    w(
        "| X11 | `repair_runtime_stop_reason` 无生产调用，仅 2 个测试引用 | T00 已定：保留，随 T10 迁入 `application/repair.py`，2 个测试不改 |"
    )
    w(
        "| X12 | 知乎指定详情遇 zvideo 时调用 `get_video_info`/`extract_zvideo_content_from_html` | 照迁以保持现行行为；视频仍由根 `is_video_record` 后置过滤，不在迁移中改为提前跳过 |"
    )
    w(
        "| X13 | 上游 2026-09-18/19 修复了抖音 detail 接口的 `uifid/verifyFp/fp` 参数与 `x-tt-argus` 请求头；fork 在公共参数中已有前者，没有后者 | 首期机械迁移不引入；若正式运行出现 “Blocked by ArgusSecurityPlugin”，按独立行为变更处理，见 H/R06 |"
    )
    w("")
    w("## 退出切片的调用点切断")
    w("")
    w("被标为“退”的定义仍有来自保留代码的静态引用，全部位于正式运行关闭的分支（评论开关、代理开关、creator 模式、")
    w(
        "上游基类/工厂、词云/Excel）。迁移时必须在保留代码中删除这些分支或显式替换，**不能只删文件**。下表为 AST 检出的完整列表："
    )
    w("")
    w("| 退出符号 | 保留代码中的引用位置 | 切断方式 |")
    w("|---|---|---|")
    how = {
        "Abstract": "解除继承，改为本站类型或 D4 端口",
        "Factory": "改为直接调用本站 JSONL 出口/MEMORY 缓存",
        "proxy": "删除 `ENABLE_IP_PROXY` 分支；client 去掉 Mixin，保持 `proxy=None` 无操作",
        "comment": "删除 `ENABLE_GET_COMMENTS` 分支（正式恒为 false）",
        "creator": '删除 `CRAWLER_TYPE == "creator"` 分支',
        "wordcloud": "删除 `ENABLE_GET_WORDCLOUD`/Excel 分支",
        "argv": "删除 init_db/贴吧参数归一",
        "video": "删除视频分支（正式禁用视频）",
    }

    def cut(name):
        n = name
        if n.startswith("Abstract"):
            return how["Abstract"]
        if "Factory" in n or n in ("create_store", "create_cache", "create_crawler"):
            return how["Factory"]
        if n in (
            "IpInfoModel",
            "ProxyRefreshMixin",
            "_refresh_proxy_if_expired",
            "create_ip_pool",
            "format_proxy_info",
            "get_proxy",
            "init_proxy_pool",
        ):
            return how["proxy"]
        if "comment" in n.lower() or n in ("ZhihuComment", "_extract_comment", "extract_comments", "extract_offset"):
            return how["comment"]
        if "creator" in n.lower() or n in (
            "CreatorUrlInfo",
            "save_creator",
            "get_all_notes_by_creator",
            "get_notes_by_creator",
            "get_all_user_aweme_posts",
            "get_user_aweme_posts",
            "get_all_anwser_by_creator",
            "get_creators_and_notes",
            "get_creators_and_videos",
            "batch_update_weibo_notes",
            "batch_update_zhihu_contents",
            "batch_get_notes_full_text",
            "get_all_notes_by_creator_id",
            "get_creator_info_by_id",
            "parse_creator_info_from_url",
        ):
            return how["creator"]
        if n in (
            "AsyncWordCloudGenerator",
            "generate_word_frequency_and_cloud",
            "_flush_excel_if_needed",
            "_generate_wordcloud_if_needed",
        ):
            return how["wordcloud"]
        if n in ("_inject_init_db_default", "_normalize_tieba_creator_url", "_normalize_tieba_note_id", "init_db"):
            return how["argv"]
        if "video" in n.lower():
            return how["video"]
        return "T00 逐项确认"

    for v in viol:
        if v.startswith("violations"):
            continue
        name, refs = v.split(" <- ", 1)
        refs = refs.strip("[]").replace("'", "").replace("M/", "")
        w(f"| `{name}` | {refs} | {cut(name)} |")
    w("")
    w(
        "`batch_get_notes_full_text` 与 `batch_update_weibo_notes` 等只在 creator 链调用，按 creator 切断；`get_comments`、"
    )
    w("`get_note_all_comments` 等同名方法在多站出现时按各站分别切断。")
    w("")
    w("## 全量账")
    w("")
    w("每行：`行号 定义 → 目标 ｜处置｜卡｜测｜备注`。同一文件内按源码顺序排列。")
    w("")
    order = list(L["files"].keys())
    for f in order:
        rows = L["files"][f]
        c = collections.Counter(r[3] for r in rows)
        w(f"### `{short(f)}`（{len(rows)}；" + "、".join(f"{k}{v}" for k, v in c.items()) + "）")
        w("")
        w("| 行 | 定义 | 目标 | 处置 | 卡 | 测 | 备注 |")
        w("|---:|---|---|---|---|---|---|")
        for ln, q, tgt, disp, card, test, note in rows:
            tg = "—" if tgt == "—" else f"`{tgt}`"
            w(f"| {ln} | `{q}` | {tg} | {disp} | {card} | {test or '—'} | {note} |")
        w("")
    w("## 维护规则")
    w("")
    w(
        "- 使用 `python scripts/dev/adapter_ledger.py symbols` 生成本附录与 JSON；使用 `symbols --check` 核对，禁止手工增删行。源码基线变化后，T00 必须解释每处差异。"
    )
    w("- 新增、删除或改名的定义没有规则时，枚举失败即视为账目不完整，不得进入对应实施卡。")
    w("- 处置从“退”改为保留，或从保留改为“退”，属于行为范围变更，须同时修改详细规格对应 C 小节与 G 任务卡。")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
