"""在单独的 pytest driver 中执行真实信号用例，保留原函数全部断言。"""

from functools import wraps
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


def isolated_signal_test(function):
    @wraps(function)
    def isolated(*args, **kwargs):
        node = os.environ["PYTEST_CURRENT_TEST"].rsplit(" (", 1)[0]
        if os.environ.get("TPC_SIGNAL_DRIVER") == node:
            return function(*args, **kwargs)
        temporary = kwargs["tmp_path"]
        report = temporary / "signal-driver.xml"
        output = temporary / "signal-driver.log"
        root = Path(__file__).resolve().parents[2]
        environment = dict(os.environ, TPC_SIGNAL_DRIVER=node)
        # 与 lane 一致：显式加载 pytest_asyncio，并让 support 可导入。
        # 自动入口会把同一插件再注册一次；裸 pytest 没有把 tests/ 放进 PYTHONPATH。
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(str(root / name) for name in ("src", "tests", "scripts"))
        environment["TPC_LANE_COUNTS"] = str(temporary / "driver-counts.json")
        environment["TPC_EXEC_GUARD_REPORT"] = str(temporary / "driver-guard.json")
        with output.open("w") as stream:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", node, "-q",
                 "-p", "pytest_asyncio.plugin", "-p", "support.execution_guard",
                 "-o", "xfail_strict=true",
                 "--basetemp", str(temporary / "driver"),
                 "--junitxml", str(report),
                 "-o", f"cache_dir={temporary / 'cache'}"],
                cwd=Path(__file__).resolve().parents[2], env=environment,
                stdout=stream, stderr=subprocess.STDOUT, timeout=120,
            )
        assert result.returncode == 0, output.read_text()[-6000:]
        suites = list(ET.parse(report).getroot().iter("testsuite"))
        assert sum(int(s.attrib["tests"]) for s in suites) == 1
        assert all(int(s.attrib.get(key, 0)) == 0
                   for s in suites for key in ("failures", "errors", "skipped"))

    return isolated
