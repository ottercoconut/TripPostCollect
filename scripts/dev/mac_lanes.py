"""在 macOS 上预检已提交的 HEAD：三个 lane、台账、冻结文件和 ruff。

不运行 os lane，也没有跳过或改跑它的参数。临时源码、独立环境和报告放在
/private/tmp，结束时删除，包括带不可变标志的副本。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
LANES = ("component", "socket", "installation")
LABELS = {
    "prepare": "准备",
    "component": "component",
    "socket": "socket",
    "installation": "installation",
    "symbols": "台账 symbols",
    "inputs": "台账 inputs",
    "drift": "台账 drift",
    "progress": "台账 progress",
    "frozen": "冻结文件",
    "ruff": "ruff",
}
_NOFILE_TARGETS = (65536, 10240, 4096, 2048)
_RUN_MATRIX = None


def _import_remove_source():
    dev = str(Path(__file__).resolve().parent)
    if dev not in sys.path:
        sys.path.insert(0, dev)
    from card_gate import remove_source as imported

    return imported


remove_source = _import_remove_source()


def load_run_matrix():
    """按路径装载本仓库的 run_matrix，供 fresh_source 与 lane 命令复用。"""
    global _RUN_MATRIX
    if _RUN_MATRIX is None:
        import importlib.util

        path = ROOT / "scripts" / "ci" / "run_matrix.py"
        spec = importlib.util.spec_from_file_location("mac_lanes_run_matrix", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RUN_MATRIX = module
    return _RUN_MATRIX


def raise_nofile_limit():
    """默认软限制 256 会在 lane 里耗尽描述符，出现上千个假错误。"""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    for desired in _NOFILE_TARGETS:
        target = desired if hard == resource.RLIM_INFINITY else min(desired, hard)
        if target <= soft:
            break
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (OSError, ValueError):
            continue
        return target
    if soft > 256:
        return soft
    raise RuntimeError(f"RLIMIT_NOFILE 软限制为 {soft}，硬限制 {hard}，无法提高到 256 以上")


def working_tree_status(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or "git status 失败")
    return result.stdout


def uv_executable() -> str:
    found = shutil.which("uv")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "uv"
    if fallback.is_file():
        return str(fallback)
    raise RuntimeError("找不到 uv")


def tail_text(text: str, limit: int = 40) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    return "\n".join(lines[-limit:])


def tail_file(path: Path, limit: int = 40, max_bytes: int = 65536) -> str:
    if not path.is_file():
        return ""
    data = path.read_bytes()[-max_bytes:]
    return tail_text(data.decode(errors="replace"), limit)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def make_item(name, ok, seconds, returncode, counts, excerpt):
    return {
        "name": name,
        "label": LABELS[name],
        "ok": bool(ok),
        "seconds": seconds,
        "returncode": returncode,
        "counts": counts,
        "excerpt": excerpt,
    }


def format_item(item) -> str:
    seconds = f"{item['seconds']:.1f}"
    counts = item.get("counts")
    if isinstance(counts, dict):
        parts = [
            f"选中 {counts.get('selected', 0)}",
            f"通过 {counts.get('passed', 0)}",
            f"失败 {counts.get('failed', 0)}",
            f"错误 {counts.get('errors', 0)}",
            f"跳过 {counts.get('skipped', 0)}",
        ]
        for key, label in (("xfailed", "xfail"), ("xpassed", "xpass"), ("collection_errors", "收集错误")):
            if counts.get(key):
                parts.append(f"{label} {counts[key]}")
        detail = "，".join(parts)
        state = "通过" if item["ok"] else "未通过"
        return f"{item['label']}：{state}，{detail}，耗时 {seconds} 秒"
    state = "通过" if item["ok"] else f"未通过（退出码 {item['returncode']}）"
    return f"{item['label']}：{state}，耗时 {seconds} 秒"


def static_commands(python: Path):
    ledger = [str(python), "scripts/dev/adapter_ledger.py"]
    return (
        ("symbols", [*ledger, "symbols", "--check"]),
        ("inputs", [*ledger, "inputs", "--check"]),
        ("drift", [*ledger, "drift"]),
        ("progress", [*ledger, "progress"]),
        ("frozen", [str(python), "scripts/verify_frozen_files.py"]),
        ("ruff", [str(python), "-m", "ruff", "check", "--no-cache"]),
    )


def run_static(name, command, cwd: Path):
    started = time.monotonic()
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, errors="replace", check=False)
    seconds = time.monotonic() - started
    excerpt = "" if result.returncode == 0 else tail_text(f"{result.stdout}\n{result.stderr}")
    return make_item(name, result.returncode == 0, seconds, result.returncode, None, excerpt)


def prepare_archived_env(root: Path, work: Path):
    """git archive HEAD 解出纯源码，再用锁定的 dev extra 建立独立环境。"""
    pristine = work / "pristine"
    pristine.mkdir()
    with subprocess.Popen(
        ["git", "-C", str(root), "archive", "HEAD"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as archive:
        extracted = subprocess.run(
            ["tar", "-x", "-C", str(pristine)], stdin=archive.stdout, capture_output=True, check=False,
        )
        if archive.stdout:
            archive.stdout.close()
        _, archive_error = archive.communicate()
        archive_code = archive.returncode
    if archive_code != 0:
        detail = archive_error.decode(errors="replace").strip()
        raise RuntimeError(detail or "git archive HEAD 失败")
    if extracted.returncode != 0:
        detail = extracted.stderr.decode(errors="replace").strip()
        raise RuntimeError(detail or "解出纯源码失败")
    venv = work / "root-venv"
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    log_path = work / "uv-sync.log"
    with log_path.open("w", encoding="utf-8") as log:
        try:
            sync = subprocess.run(
                [uv_executable(), "sync", "--locked", "--extra", "dev", "--python", "3.12"],
                cwd=pristine, env=env, stdout=log, stderr=subprocess.STDOUT, check=False, timeout=600,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("uv sync 超过 600 秒") from exc
    if sync.returncode != 0:
        raise RuntimeError("uv sync 失败\n" + tail_file(log_path))
    python = venv / "bin" / "python"
    if not python.is_file():
        raise RuntimeError("独立环境缺少 python")
    return pristine, python


def lane_argv(lane: str, runner: Path, source: Path, output: Path, python: Path):
    """复用 run_matrix.lane_command。解释器换成 /private/tmp 里的独立环境，Seatbelt 才能读到它。"""
    if lane not in LANES:
        raise RuntimeError(f"拒绝运行 lane：{lane}")
    command = load_run_matrix().lane_command(runner, source, lane, output)
    command[0] = str(python)
    return command


def diagnostics_text(counts) -> str:
    if not isinstance(counts, dict):
        return ""
    rows = counts.get("failure_diagnostics") or []
    if not rows:
        return ""
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows[:10])


def run_lane(lane: str, pristine: Path, runner: Path, python: Path, work: Path):
    if lane not in LANES:
        raise RuntimeError(f"拒绝运行 lane：{lane}")
    print(f"{LABELS[lane]}：开始", flush=True)
    started = time.monotonic()
    output = work / "reports" / lane
    try:
        matrix = load_run_matrix()
        source = matrix.fresh_source(pristine, work / f"lane-{lane}")
        if not runner.is_file():
            raise RuntimeError(f"临时源码缺少 runner：{runner}")
        command = lane_argv(lane, runner, source, output, python)
        result = subprocess.run(
            command, cwd=runner.parent.parent, env=matrix.lane_environment(lane),
            capture_output=True, text=True, errors="replace", check=False,
        )
        summary = read_json(output / "result.json") or {}
        counts = summary.get("counts")
        if not isinstance(counts, dict):
            counts = None
        ok = result.returncode == 0 and counts is not None
        excerpt = ""
        if not ok:
            excerpt = "\n".join(part for part in (
                tail_text(result.stdout or ""),
                tail_text(result.stderr or ""),
                tail_file(output / "pytest.log"),
                diagnostics_text(counts if counts is not None else read_json(output / "counts.json")),
            ) if part)
        return make_item(lane, ok, time.monotonic() - started, result.returncode, counts, excerpt)
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return make_item(lane, False, time.monotonic() - started, 1, None, str(exc))


def run_suite(root: Path, work: Path):
    items = []

    def record(item):
        items.append(item)
        print(format_item(item), flush=True)
        if not item["ok"] and item.get("excerpt"):
            print(item["excerpt"], file=sys.stderr, flush=True)

    for name, command in static_commands(Path(sys.executable)):
        record(run_static(name, command, root))
    print("准备：开始解出 HEAD 并建立独立环境", flush=True)
    started = time.monotonic()
    try:
        pristine, python = prepare_archived_env(root, work)
    except (OSError, RuntimeError) as exc:
        record(make_item("prepare", False, time.monotonic() - started, 1, None, str(exc)))
        return items
    record(make_item("prepare", True, time.monotonic() - started, 0, None, ""))
    runner = pristine / "tests" / "run_lanes.py"
    for lane in LANES:
        record(run_lane(lane, pristine, runner, python, work))
    return items


def temporary_work_dir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="tpc-mac-lanes.", dir="/private/tmp")).resolve()
    if Path("/private/tmp") not in path.parents:
        raise RuntimeError(f"临时目录不在 /private/tmp：{path}")
    return path


def cleanup(work: Path) -> None:
    resolved = work.resolve()
    if resolved == Path("/") or Path("/private/tmp") not in resolved.parents:
        raise RuntimeError(f"拒绝删除临时目录以外的路径：{resolved}")
    remove_source(resolved)


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if sys.platform != "darwin":
        print(f"mac_lanes 只支持 macOS，当前平台 {sys.platform} 拒绝运行。", file=sys.stderr)
        return 2
    if arguments:
        print("mac_lanes 不接受参数，没有跳过或改跑 os lane 的选项。", file=sys.stderr)
        return 2
    try:
        status = working_tree_status(ROOT)
    except RuntimeError as exc:
        print(f"无法确认工作区是否干净：{exc}", file=sys.stderr)
        return 1
    if status.strip():
        print("工作区有未提交改动，mac_lanes 只测已提交的 HEAD。", file=sys.stderr)
        print("\n".join(status.splitlines()[:30]), file=sys.stderr)
        return 1
    try:
        limit = raise_nofile_limit()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"无法提高文件描述符上限：{exc}", file=sys.stderr)
        return 1
    print(f"文件描述符上限：{limit}", flush=True)
    work = None
    code = 1
    started = time.monotonic()
    try:
        work = temporary_work_dir()
        results = run_suite(ROOT, work)
        code = 0 if results and all(item["ok"] for item in results) else 1
    except Exception as exc:
        print(f"本机预检中断：{exc}", file=sys.stderr)
        code = 1
    finally:
        if work is not None:
            try:
                cleanup(work)
            except OSError as exc:
                print(f"临时目录清理失败：{work}：{exc}", file=sys.stderr)
                code = 1
            if work.exists():
                print(f"临时目录仍在：{work}", file=sys.stderr)
                code = 1
    elapsed = time.monotonic() - started
    print(f"合计：{'通过' if code == 0 else '未通过'}，耗时 {elapsed:.1f} 秒", flush=True)
    print("os lane 没跑，需要 GitHub CI。", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
