"""T13：选站 worker 在装配、钩子、装载与实例化各阶段只导入本站根平台包（F10）。

唯一登记例外（决策 D9，结构风险留给 T14 之后整理）：微博依赖装配中的行为桥在
行为调用时懒导入 `trippostcollect.platforms.xhs.behavior`（`platforms/entry.py:weibo_dependencies`）。
它不在下列任一阶段发生；静态清单断言该例外恰好一处，新增任何跨站导入都会失败。
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
T02_COMMANDS = json.loads((ROOT / "tests/golden/t02_worker_commands.json").read_text(encoding="utf-8"))
PLATFORM_PACKAGES = {"wb": "weibo", "dy": "douyin", "zhihu": "zhihu", "xhs": "xhs"}
REGISTERED_CROSS_SITE_IMPORTS = {("weibo_dependencies", "trippostcollect.platforms.xhs.behavior")}

PROBE = r'''
import json, sys
argv, code = json.loads(sys.argv[1]), sys.argv[2]

def platform_packages():
    sites = {"bilibili", "weibo", "douyin", "zhihu", "xhs"}
    return sorted({name.split(".")[2] for name in sys.modules
                   if name.startswith("trippostcollect.platforms.") and name.count(".") >= 2
                   and name.split(".")[2] in sites})

from trippostcollect.platforms.entry import configure, install_hooks, load_crawler
stages = {}
configure(argv); stages["configure"] = platform_packages()
install_hooks(); stages["install_hooks"] = platform_packages()
crawler_class = load_crawler(code); stages["load_crawler"] = platform_packages()
crawler_class(); stages["instantiate"] = platform_packages()
print(json.dumps(stages))
'''


def _environment(extra_env: dict[str, str], tmp: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("TRIPPOSTCOLLECT_") and key not in {"PYTHONPATH", "TRIPPOST_PROJECT_ROOT"}}
    environment.update({key: value.replace("<TMP>", str(tmp)).replace("<ROOT>", str(ROOT))
                        for key, value in extra_env.items()})
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


@pytest.mark.parametrize("scenario", sorted(T02_COMMANDS))
def test_worker_loads_only_selected_root_platform(scenario: str, tmp_path: Path) -> None:
    golden = T02_COMMANDS[scenario]
    argv = [part.replace("<TMP>", str(tmp_path)) for part in golden["cmd"][4:]]
    code = argv[argv.index("--platform") + 1]
    result = subprocess.run(
        [sys.executable, "-P", "-c", PROBE, json.dumps(argv), code],
        cwd=tmp_path, env=_environment(golden["extra_env"], tmp_path),
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    stages = json.loads(result.stdout.strip().splitlines()[-1])
    selected = PLATFORM_PACKAGES[code]
    for stage, packages in stages.items():
        assert set(packages) <= {selected}, (stage, packages)
    assert stages["instantiate"] == [selected]


def test_cross_site_imports_in_worker_assembly_are_exactly_registered() -> None:
    tree = ast.parse((ROOT / "src/trippostcollect/platforms/entry.py").read_text(encoding="utf-8"))
    own = {"weibo_dependencies": "weibo", "douyin_dependencies": "douyin",
           "_zhihu_dependencies": "zhihu", "xhs_dependencies": "xhs"}
    found = set()
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef) or function.name not in own:
            continue
        for node in ast.walk(function):
            modules = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                       else [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            for module in modules:
                if module.startswith("trippostcollect.platforms.") and module.split(".")[2] != own[function.name]:
                    found.add((function.name, module))
    assert found == REGISTERED_CROSS_SITE_IMPORTS
