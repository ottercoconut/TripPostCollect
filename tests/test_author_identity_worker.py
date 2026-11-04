"""身份 child 的摘要、看门狗和一次 SIGTERM 收束。不启动平台入口。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from trippostcollect.application import author_identity_worker as worker
from trippostcollect.application.policy import CrawlPolicyBlocked
from trippostcollect.records.author_observe import OBSERVATIONS_NAME
from trippostcollect.runtime.process import run_command


def _args(tmp_path: Path) -> object:
    return worker.parse_args(
        [
            "--platform",
            "weibo",
            "--keyword",
            "青岛旅游",
            "--db",
            str(tmp_path / "db.sqlite"),
            "--save-data-path",
            str(tmp_path / "data"),
            "--headless",
            "true",
        ]
    )


@contextmanager
def _allow(*_args: object, **_kwargs: object):
    yield {"allowed": True, "wait_seconds": 0}


def test_policy_blocked_summary_has_wait_seconds_and_no_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    @contextmanager
    def blocked(*_args: object, **_kwargs: object):
        if False:
            yield {}
        raise CrawlPolicyBlocked({"reason": "cooldown_active", "wait_seconds": 90, "site": "weibo"})

    monkeypatch.setattr(worker, "site_request_guard", blocked)
    code = worker.run_supervised_batch(
        _args(tmp_path),
        [sys.executable, "-c", "raise SystemExit(0)"],
    )
    captured = capsys.readouterr()
    summary_path = tmp_path / "logs" / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    envelope = json.loads(captured.out.strip().splitlines()[-1])
    assert code == 2
    assert "Traceback" not in captured.err
    assert envelope["summary"] == str(summary_path)
    assert summary["formal_validation"]["stop_reason"] == "policy_blocked"
    assert summary["failure_classification"]["wait_seconds"] == 90
    assert summary["behavior_validation"]["behavior_ok"] is False
    assert "platform_returncode" in summary


def test_observation_updates_count_as_watchdog_progress(tmp_path: Path) -> None:
    evidence = tmp_path / "logs" / "behavior_evidence.json"
    evidence.parent.mkdir(parents=True)
    observation = evidence.with_name(OBSERVATIONS_NAME)
    paths = worker.batch_progress_paths(tmp_path / "data", evidence)
    assert paths[-1] == observation
    script = tmp_path / "touch.py"
    script.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "path, seconds = Path(sys.argv[1]), float(sys.argv[2])\n"
        "end = time.time() + seconds\n"
        "n = 0\n"
        "while time.time() < end:\n"
        "    n += 1\n"
        "    path.write_text(str(n))\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    command = [sys.executable, str(script), str(observation), "1.4"]
    alive = run_command(
        command,
        tmp_path,
        0.5,
        tmp_path / "alive-logs",
        progress_paths=paths,
        cleanup_grace_seconds=0.3,
    )
    stalled = run_command(
        [sys.executable, str(script), str(observation), "1.4"],
        tmp_path,
        0.5,
        tmp_path / "stalled-logs",
        progress_paths=[tmp_path / "data", evidence],
        cleanup_grace_seconds=0.3,
    )
    assert alive["returncode"] == 0
    assert stalled["returncode"] == 124
    assert stalled["timeout_reason"] == "no_progress_timeout"


def test_worker_forwards_one_sigterm_then_sigkill_and_writes_summary(tmp_path: Path) -> None:
    sleeper = tmp_path / "sleeper.py"
    counter = tmp_path / "sigterms"
    sleeper.write_text(
        "import signal, sys, time\n"
        "from pathlib import Path\n"
        "path = Path(sys.argv[1])\n"
        "path.write_text('0')\n"
        "def handler(signum, frame):\n"
        "    path.write_text(str(int(path.read_text() or '0') + 1))\n"
        "signal.signal(signal.SIGTERM, handler)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import sys\n"
        "from contextlib import contextmanager\n"
        "from trippostcollect.application.author_identity_worker import (\n"
        "    parse_args, run_supervised_batch,\n"
        ")\n"
        "from trippostcollect.runtime.process import run_main_with_operator_interrupt\n"
        "import trippostcollect.application.author_identity_worker as worker\n"
        "@contextmanager\n"
        "def allow(*_args, **_kwargs):\n"
        "    yield {'allowed': True, 'wait_seconds': 0}\n"
        "worker.site_request_guard = allow\n"
        "grace = float(sys.argv[-3])\n"
        "sleeper_path = sys.argv[-2]\n"
        "counter_path = sys.argv[-1]\n"
        "args = parse_args(sys.argv[1:-3])\n"
        "raise SystemExit(run_main_with_operator_interrupt(lambda: run_supervised_batch(\n"
        "    args,\n"
        "    [sys.executable, sleeper_path, counter_path],\n"
        "    cleanup_grace_seconds=grace,\n"
        ")))\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            str(driver),
            "--platform",
            "weibo",
            "--keyword",
            "青岛旅游",
            "--db",
            str(tmp_path / "db.sqlite"),
            "--save-data-path",
            str(tmp_path / "data"),
            "--headless",
            "true",
            "0.4",
            str(sleeper),
            str(counter),
        ],
        start_new_session=True,
    )
    deadline = time.time() + 8
    while time.time() < deadline and not counter.exists():
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    assert counter.exists()
    os.kill(proc.pid, signal.SIGTERM)
    assert proc.wait(timeout=10) == 128 + signal.SIGTERM
    assert counter.read_text(encoding="utf-8") == "1"
    summary = json.loads((tmp_path / "logs" / "summary.json").read_text(encoding="utf-8"))
    assert summary["formal_validation"]["stop_reason"] == "operator_interrupt"


def test_platform_transport_blocks_and_empty_shell_records_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from trippostcollect.platforms.douyin.core import DouYinCrawler
    from trippostcollect.platforms.douyin.models import DataFetchError as DouyinDataFetchError
    from trippostcollect.platforms.weibo.core import WeiboCrawler
    from trippostcollect.platforms.weibo.models import DataFetchError as WeiboDataFetchError
    from trippostcollect.platforms.weibo.models import PlatformRuntimeError as WeiboRuntimeError
    from trippostcollect.platforms.zhihu.core import ZhihuCrawler
    from trippostcollect.platforms.zhihu.models import DataFetchError as ZhihuDataFetchError
    from trippostcollect.records.author_observe import AuthorIdentityBlocked

    evidence = tmp_path / "behavior_evidence.json"
    targets = evidence.with_name("author_identity_targets.json")
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(evidence))

    def install(posts: list[dict]) -> None:
        targets.write_text(
            json.dumps([{"author_key": "a" * 16, "posts": posts}]),
            encoding="utf-8",
        )

    class WeiboClient:
        def __init__(self, action: str) -> None:
            self.action = action

        async def get_note_info_by_id(self, _note_id: str) -> dict:
            if self.action == "transport":
                raise httpx.ConnectTimeout("down")
            if self.action == "empty":
                raise WeiboDataFetchError("missing")
            if self.action == "runtime":
                raise WeiboRuntimeError("rate_limited")
            raise RuntimeError("odd")

    weibo = object.__new__(WeiboCrawler)
    weibo.config = SimpleNamespace(CRAWLER_MAX_SLEEP_SEC=0)
    weibo.wb_client = WeiboClient("transport")
    install([{"platform_post_id": "1"}])
    with pytest.raises(AuthorIdentityBlocked, match="network_unreachable"):
        asyncio.run(weibo.collect_author_identities())

    weibo.wb_client = WeiboClient("empty")
    asyncio.run(weibo.collect_author_identities())
    observed = json.loads(evidence.with_name(OBSERVATIONS_NAME).read_text(encoding="utf-8"))
    assert observed["observations"][0]["not_observed_reason"] == "detail_missing"
    assert observed["observations"][0]["status"] == "not_observed"

    weibo.wb_client = WeiboClient("odd")
    asyncio.run(weibo.collect_author_identities())
    observed = json.loads(evidence.with_name(OBSERVATIONS_NAME).read_text(encoding="utf-8"))
    assert observed["observations"][0]["not_observed_reason"] == "RuntimeError"

    class DouyinClient:
        async def get_video_by_id(self, _aweme_id: str) -> dict:
            raise httpx.ConnectError("down")

    douyin = object.__new__(DouYinCrawler)
    douyin.settings = SimpleNamespace(CRAWLER_MAX_SLEEP_SEC=0)
    douyin.dy_client = DouyinClient()
    install([{"platform_post_id": "7525082444551310602", "detail_target": "7525082444551310602"}])
    with pytest.raises(AuthorIdentityBlocked, match="network_unreachable"):
        asyncio.run(douyin.collect_author_identities())

    class DouyinEmpty:
        async def get_video_by_id(self, _aweme_id: str) -> dict:
            raise DouyinDataFetchError("missing")

    douyin.dy_client = DouyinEmpty()
    asyncio.run(douyin.collect_author_identities())
    observed = json.loads(evidence.with_name(OBSERVATIONS_NAME).read_text(encoding="utf-8"))
    assert observed["observations"][0]["not_observed_reason"] == "detail_missing"

    class ZhihuClient:
        def __init__(self, action: str) -> None:
            self.action = action

        async def get_answer_info(self, _question: str, _answer: str) -> object:
            if self.action == "transport":
                raise httpx.ReadTimeout("down")
            if self.action == "empty":
                raise ZhihuDataFetchError("missing")
            return None

    zhihu = object.__new__(ZhihuCrawler)
    zhihu.settings = SimpleNamespace(CRAWLER_MAX_SLEEP_SEC=0)
    zhihu.zhihu_client = ZhihuClient("transport")
    install(
        [
            {
                "platform_post_id": "55",
                "detail_target": "https://www.zhihu.com/question/9/answer/55",
            }
        ]
    )
    with pytest.raises(AuthorIdentityBlocked, match="network_unreachable"):
        asyncio.run(zhihu.collect_author_identities())
    zhihu.zhihu_client = ZhihuClient("none")
    asyncio.run(zhihu.collect_author_identities())
    observed = json.loads(evidence.with_name(OBSERVATIONS_NAME).read_text(encoding="utf-8"))
    assert observed["observations"][0]["not_observed_reason"] == "detail_missing"
