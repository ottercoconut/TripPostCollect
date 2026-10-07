"""PEP 517 前端语义下的树内构建后端：只按 backend-path 导入后端，并能离线构建 wheel 与 editable。

pip/build 共用的 pyproject_hooks 要求：声明 backend-path 时，build-backend 模块必须从这些目录加载。
本用例在仓库外副本中以隔离解释器复现这一导入规则，不依赖 cwd 或 PYTHONPATH，构建全程离线。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.installation

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ("build_wheel", "build_sdist", "build_editable", "get_requires_for_build_wheel",
         "get_requires_for_build_sdist", "get_requires_for_build_editable",
         "prepare_metadata_for_build_wheel", "prepare_metadata_for_build_editable")
EXPECTED_RESOURCES = {
    "js/douyin.js", "js/zhihu.js", "js/stealth.min.js", "licenses/MediaCrawler-LICENSE",
    "sql/crawl_scheduler.sql", "sql/ctf_captures.sql", "sql/source_platforms.sql", "sql/web_posts.sql",
    "sql/xhs_control.sql", "contracts/formal-crawl-contract.md",
}
COPY_EXCLUDED = {".git", ".venv", "venv", "tools", "data", "outputs", "temp", "build", "dist",
                 "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules"}

# 按 pyproject_hooks 的做法：backend-path 目录放在 sys.path 最前，再导入 build-backend 指定的模块。
FRONTEND = r'''
import importlib, json, sys
from pathlib import Path
request = json.loads(sys.argv[1])
backend_path = [str(Path(item).resolve()) for item in request["backend_path"]]
sys.path[:0] = backend_path
module = importlib.import_module(request["backend"])
location = Path(module.__file__).resolve()
report = {
    "file": str(location),
    "inside_backend_path": any(location.is_relative_to(item) for item in backend_path),
    "hooks": sorted(name for name in request["hooks"] if callable(getattr(module, name, None))),
}
if request.get("build"):
    out = Path(request["out"])
    report["wheel"] = module.build_wheel(str(out / "wheel"))
    report["editable"] = module.build_editable(str(out / "editable"))
print(json.dumps(report))
'''


@pytest.fixture(scope="module")
def source(tmp_path_factory) -> Path:
    copy = tmp_path_factory.mktemp("t12-backend") / "source"

    def ignore(directory, names):
        return [name for name in names if name in COPY_EXCLUDED or name.endswith(".egg-info")]

    shutil.copytree(ROOT, copy, ignore=ignore)
    return copy


def frontend(source: Path, **request):
    config = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["build-system"]
    request.update(backend=config["build-backend"], hooks=list(HOOKS),
                   backend_path=[str(source / item) for item in config.get("backend-path", [])])
    # -I：不把脚本目录、cwd 或 PYTHONPATH 放入 sys.path，只有 backend-path 与锁定环境的 site-packages。
    result = subprocess.run([sys.executable, "-I", "-c", FRONTEND, json.dumps(request)], cwd=source,
                            capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr[-4000:]
    return config, json.loads(result.stdout.strip().splitlines()[-1])


def test_backend_loads_from_backend_path_and_exposes_pep517_hooks(source) -> None:
    config, report = frontend(source)
    assert config.get("backend-path"), "树内后端必须声明 backend-path"
    assert report["inside_backend_path"], report["file"]
    assert report["hooks"] == sorted(HOOKS)


def test_backend_builds_wheel_and_editable_offline(source, tmp_path) -> None:
    _, report = frontend(source, build=True, out=str(tmp_path))
    with zipfile.ZipFile(tmp_path / "wheel" / report["wheel"]) as archive:
        prefix = "trippostcollect/resources/"
        resources = {name.removeprefix(prefix) for name in archive.namelist()
                     if name.startswith(prefix) and not name.endswith((".py", "/"))}
    assert resources == EXPECTED_RESOURCES
    with zipfile.ZipFile(tmp_path / "editable" / report["editable"]) as archive:
        names = archive.namelist()
    assert any(name.endswith(".pth") for name in names), names
    # 可编辑构建不在源码树生成资源副本。
    assert not (source / "src/trippostcollect/resources/sql").exists()
    assert not (source / "src/trippostcollect/resources/contracts").exists()
