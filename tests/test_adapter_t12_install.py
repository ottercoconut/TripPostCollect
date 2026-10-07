"""T12 验收（installation lane）：wheel/sdist 资源完整、仓库外全新 venv 安装、入口与选站不依赖 cwd 与 fork。

构建只在临时源码副本中进行（不含 tools/、data/、outputs/），用锁定 dev 环境中的 setuptools 以进程内
PEP 517 调用离线完成（等价 `--no-build-isolation`，不建隔离环境、不下载构建依赖）；
全新 venv 不带 pip，直接解包 wheel，三方依赖经只读 .pth 复用锁定环境的 site-packages（不处理其中的
editable .pth），因此 `trippostcollect` 只能来自刚构建的 wheel。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import tarfile
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.installation

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
HELP_GOLDEN = json.loads((GOLDEN / "t12_cli_help.json").read_text(encoding="utf-8"))
T02_COMMANDS = json.loads((GOLDEN / "t02_worker_commands.json").read_text(encoding="utf-8"))
SQL_FILES = ("crawl_scheduler.sql", "ctf_captures.sql", "source_platforms.sql", "web_posts.sql", "xhs_control.sql")
# 包内资源 → 唯一编辑真源（规格 G 节“T01/T12 确定的资源与安装策略”）。
RESOURCE_SOURCES = {
    "js/douyin.js": "src/trippostcollect/resources/js/douyin.js",
    "js/zhihu.js": "src/trippostcollect/resources/js/zhihu.js",
    "js/stealth.min.js": "src/trippostcollect/resources/js/stealth.min.js",
    "licenses/MediaCrawler-LICENSE": "src/trippostcollect/resources/licenses/MediaCrawler-LICENSE",
    **{f"sql/{name}": f"db/{name}" for name in SQL_FILES},
    "contracts/formal-crawl-contract.md": "docs/formal-crawl-contract.md",
}
SDIST_REQUIRED = (
    "pyproject.toml", "build_support.py", "MANIFEST.in",
    *(f"db/{name}" for name in SQL_FILES), "docs/formal-crawl-contract.md",
    "src/trippostcollect/resources/js/douyin.js", "src/trippostcollect/resources/licenses/MediaCrawler-LICENSE",
)
COPY_EXCLUDED = {".git", ".venv", "venv", "tools", "data", "outputs", "temp", "build", "dist",
                 "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules"}
WORKER_SCENARIOS = {"wb": "weibo_search", "dy": "douyin_search_discovery", "zhihu": "zhihu_search", "xhs": "xhs_search_qrcode"}

BUILD_CODE = (
    "import sys\n"
    "from setuptools import build_meta\n"
    "kind, out = sys.argv[1], sys.argv[2]\n"
    "print((build_meta.build_sdist if kind == 'sdist' else build_meta.build_wheel)(out))\n"
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_environment() -> dict[str, str]:
    """构建子进程不继承指向仓库源码的路径，避免后端从仓库而非副本读取。"""
    environment = dict(os.environ)
    keep = [item for item in environment.get("PYTHONPATH", "").split(os.pathsep)
            if item and not Path(item).resolve().is_relative_to(ROOT)]
    environment["PYTHONPATH"] = os.pathsep.join(keep)
    environment.pop("TRIPPOST_PROJECT_ROOT", None)
    return environment


def run_backend(kind: str, source: Path, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([sys.executable, "-c", BUILD_CODE, kind, str(out)], cwd=source,
                            env=build_environment(), capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr[-4000:]
    return out / result.stdout.strip().splitlines()[-1]


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory) -> dict[str, Path]:
    if importlib.util.find_spec("setuptools") is None:
        pytest.fail("构建后端 setuptools 不在锁定环境中：沙箱内无网络，不能临时下载构建依赖")
    work = tmp_path_factory.mktemp("t12-build")
    source = work / "source"

    def ignore(directory, names):
        return [name for name in names if name in COPY_EXCLUDED or name.endswith(".egg-info")]

    shutil.copytree(ROOT, source, ignore=ignore, symlinks=False)
    sdist = run_backend("sdist", source, work / "dist")
    wheel = run_backend("wheel", source, work / "dist")
    unpacked = work / "from-sdist"
    with tarfile.open(sdist) as archive:
        archive.extractall(unpacked, filter="data")
    (sdist_root,) = list(unpacked.iterdir())
    wheel_from_sdist = run_backend("wheel", sdist_root, work / "dist-from-sdist")
    return {"work": work, "sdist": sdist, "wheel": wheel, "wheel_from_sdist": wheel_from_sdist}


def wheel_resources(wheel: Path) -> dict[str, bytes]:
    prefix = "trippostcollect/resources/"
    with zipfile.ZipFile(wheel) as archive:
        return {name.removeprefix(prefix): archive.read(name) for name in archive.namelist()
                if name.startswith(prefix) and not name.endswith((".py", "/"))}


def test_sdist_contains_single_source_truths_and_build_module(artifacts) -> None:
    with tarfile.open(artifacts["sdist"]) as archive:
        members = {member.name.split("/", 1)[1]: member for member in archive.getmembers() if "/" in member.name}
        for name in SDIST_REQUIRED:
            assert name in members, name
            if (ROOT / name).is_file() and name != "pyproject.toml":
                assert archive.extractfile(members[name]).read() == (ROOT / name).read_bytes(), name
        assert not any(name.startswith("tools/") for name in members)
        # 生成副本不进源码树，sdist 中也不得出现 src 内的 sql/contracts 副本。
        assert not any(name.startswith(("src/trippostcollect/resources/sql", "src/trippostcollect/resources/contracts"))
                       for name in members)


@pytest.mark.parametrize("which", ["wheel", "wheel_from_sdist"])
def test_wheel_resources_match_sources_byte_for_byte(artifacts, which) -> None:
    resources = wheel_resources(artifacts[which])
    assert set(resources) == set(RESOURCE_SOURCES)
    for name, source in RESOURCE_SOURCES.items():
        assert sha(resources[name]) == sha((ROOT / source).read_bytes()), name
    with zipfile.ZipFile(artifacts[which]) as archive:
        names = archive.namelist()
    assert not any(name.startswith(("tests/", "tools/", "scripts/", "db/", "docs/")) for name in names)


# ---------- 仓库外全新 venv ----------

@pytest.fixture(scope="module")
def installed(artifacts) -> dict[str, Path]:
    work = artifacts["work"] / "install"
    venv = work / "venv"
    subprocess.run([sys._base_executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=300)
    python = venv / "bin" / "python"
    purelib = Path(subprocess.check_output(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], text=True,
    ).strip())
    with zipfile.ZipFile(artifacts["wheel"]) as archive:
        archive.extractall(purelib)
    locked = {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]}
    (purelib / "zz_t12_locked_dependencies.pth").write_text("\n".join(sorted(locked)) + "\n")
    # 工作根只放入口脚本：没有 tools/MediaCrawler、db/、docs/、data/。
    workroot = work / "workroot"
    shutil.copytree(ROOT / "scripts", workroot / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__", "dev", "ci"))
    cwd = work / "cwd"
    cwd.mkdir()
    return {"python": python, "purelib": purelib, "workroot": workroot, "cwd": cwd}


def installed_environment(installed, **extra) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", str(installed["cwd"])),
        "TMPDIR": os.environ.get("TMPDIR", str(installed["cwd"])), "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
        "TRIPPOST_PROJECT_ROOT": str(installed["workroot"]),
    }
    environment.update(extra)
    return environment


def run_installed(installed, code: str, *args: str, environment=None, timeout=300):
    return subprocess.run([str(installed["python"]), "-I", "-c", code, *args], cwd=installed["cwd"],
                          env=environment or installed_environment(installed),
                          capture_output=True, text=True, timeout=timeout)


def test_installed_package_imports_and_reads_resources_outside_repository(installed) -> None:
    code = (
        "import importlib, json, pkgutil, sys, hashlib\n"
        "import trippostcollect\n"
        "from trippostcollect.core import resources, paths\n"
        "errors = []\n"
        "for item in pkgutil.walk_packages(trippostcollect.__path__, 'trippostcollect.'):\n"
        "    try:\n        importlib.import_module(item.name)\n"
        "    except Exception as error:\n        errors.append([item.name, type(error).__name__])\n"
        "resources.verify_package_resources()\n"
        "digests = {name: hashlib.sha256(resources.read_bytes(name)).hexdigest() for name in json.loads(sys.argv[1])}\n"
        "fork = sorted({n.split('.')[0] for n, m in list(sys.modules.items())\n"
        "               if 'MediaCrawler' in (getattr(m, '__file__', None) or '')})\n"
        "print(json.dumps({'file': trippostcollect.__file__, 'errors': errors, 'digests': digests,\n"
        "                  'root': str(paths.PROJECT_ROOT), 'fork': fork}))\n"
    )
    result = run_installed(installed, code, json.dumps(sorted(RESOURCE_SOURCES)))
    assert result.returncode == 0, result.stderr[-4000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert Path(report["file"]).is_relative_to(installed["purelib"])
    assert report["errors"] == []
    assert report["fork"] == []
    assert report["root"] == str(installed["workroot"])
    assert report["digests"] == {name: sha((ROOT / source).read_bytes()) for name, source in RESOURCE_SOURCES.items()}


def schema_rows(database: Path) -> list[tuple]:
    with sqlite3.connect(database) as connection:
        return sorted(connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall())


BOOTSTRAP_CODE = (
    "import sys\n"
    "from trippostcollect.db.bootstrap import bootstrap_database\n"
    "bootstrap_database(sys.argv[1], sync_jobs=False)\n"
)


def test_installed_package_bootstraps_schema_from_package_resources(installed, tmp_path) -> None:
    # 参照：源码 checkout 用仓库 db/*.sql 建出的结构。
    reference = tmp_path / "reference.sqlite"
    environment = {key: value for key, value in os.environ.items() if key not in {"TRIPPOST_PROJECT_ROOT"}}
    environment["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run([sys.executable, "-P", "-c", BOOTSTRAP_CODE, str(reference)], cwd=tmp_path,
                            env=environment, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-4000:]
    # 安装态：工作根没有 db/，结构只能来自包内资源。
    assert not (installed["workroot"] / "db").exists()
    installed_db = tmp_path / "installed.sqlite"
    result = run_installed(installed, BOOTSTRAP_CODE, str(installed_db))
    assert result.returncode == 0, result.stderr[-4000:]
    assert schema_rows(installed_db) == schema_rows(reference)


def test_installed_formal_entries_help_is_identical_to_main(installed) -> None:
    # 显式固定影响 argparse 排版的终端尺寸与区域设置（installed_environment 不继承宿主 env）。
    environment = installed_environment(installed, COLUMNS=str(HELP_GOLDEN["_meta"]["columns"]), LINES="24")
    differences = []
    for name, expected in HELP_GOLDEN.items():
        if name == "_meta":
            continue
        if name == "trippostcollect.platforms.entry":
            command = [str(installed["python"]), "-I", "-m", name, "--help"]
        else:
            # -E -s：忽略 PYTHON* 与用户目录，仅保留脚本目录供同目录脚本互相导入。
            command = [str(installed["python"]), "-E", "-s", str(installed["workroot"] / "scripts" / f"{name}.py"), "--help"]
        result = subprocess.run(command, cwd=installed["cwd"], env=environment, capture_output=True, text=True, timeout=120)
        actual = result.stdout.replace(str(installed["workroot"]), "<ROOT>")
        if result.returncode or result.stderr or actual != expected:
            differences.append([name, result.returncode, result.stderr[-300:]])
    assert differences == []


SELECT_CODE = r'''
import json, sys
argv, code = json.loads(sys.argv[1]), sys.argv[2]
from trippostcollect.platforms.entry import configure, install_hooks, load_crawler
configure(argv)
install_hooks()
crawler_class = load_crawler(code)
crawler_class()
fork = sorted({n.split(".")[0] for n, m in list(sys.modules.items())
               if "MediaCrawler" in (getattr(m, "__file__", None) or "")})
print(json.dumps({"class": crawler_class.__name__, "fork": fork,
                  "paths": [p for p in sys.path if "MediaCrawler" in p]}))
'''


@pytest.mark.parametrize("code", sorted(WORKER_SCENARIOS))
def test_installed_worker_selects_each_platform_without_fork_tree(installed, tmp_path, code) -> None:
    golden = T02_COMMANDS[WORKER_SCENARIOS[code]]
    argv = [part.replace("<TMP>", str(tmp_path)) for part in golden["cmd"][4:]]
    environment = installed_environment(installed, **{
        key: value.replace("<TMP>", str(tmp_path)).replace("<ROOT>", str(installed["workroot"]))
        for key, value in golden["extra_env"].items()
    })
    assert not (installed["workroot"] / "tools").exists()
    result = run_installed(installed, SELECT_CODE, json.dumps(argv), code, environment=environment)
    assert result.returncode == 0, result.stderr[-4000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["fork"] == [] and report["paths"] == []


def test_installed_package_requires_explicit_work_root(installed) -> None:
    # 规格 G：仓库外入口明确要求工作根（现有 TRIPPOST_PROJECT_ROOT），不把 site-packages 上三级当运行根。
    environment = installed_environment(installed)
    environment.pop("TRIPPOST_PROJECT_ROOT")
    result = run_installed(installed, "import trippostcollect.core.paths as p; print(p.PROJECT_ROOT)",
                           environment=environment)
    assert result.returncode != 0
    assert "TRIPPOST_PROJECT_ROOT" in result.stderr
