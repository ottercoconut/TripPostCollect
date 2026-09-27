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
        environment = dict(os.environ, TPC_SIGNAL_DRIVER=node)
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
