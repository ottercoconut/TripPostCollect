"""根包安装验收（installation lane）：正式脚本入口在锁定环境中可导入。

T12 按测试台账 target_file 由 tests/test_run_ids.py 迁入，用例名与断言不变。
"""

from __future__ import annotations

import re
import sys
from importlib import import_module
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

STAMP_PATTERN = re.compile(r"^\d{8}T\d{12}[+-]\d{4}$")
STAMP_FUNCTIONS = (
    ("crawl_runner", "utc_stamp"),
    ("ctf_resource_crawl", "utc_stamp"),
    ("login_warmup", "utc_stamp"),
    ("mediacrawler_crawl", "utc_stamp"),
    ("mediacrawler_login_warmup", "utc_stamp"),
    ("xhs_runner", "utc_stamp"),
    ("info_collection_benchmark", "run_id"),
)


@pytest.mark.installation
def test_all_script_run_ids_include_microseconds() -> None:
    for module_name, function_name in STAMP_FUNCTIONS:
        stamp = getattr(import_module(module_name), function_name)()
        assert STAMP_PATTERN.fullmatch(stamp), (module_name, stamp)

    for path in SCRIPTS.glob("*.py"):
        assert "%Y%m%dT%H%M%S%z" not in path.read_text(encoding="utf-8"), path
