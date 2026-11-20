"""抖音离线边界：依赖方向、搜索截获、worker 装配与签名输入。

#222 起整轮搜索驱动不再收集。见 tests/support/douyin_issue222_deviation.py。
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from trippostcollect.platforms.douyin import parser


ROOT = Path(__file__).resolve().parents[3]
KEYWORD = "青岛崂山旅游攻略"


def aweme(post_id):
    return {
        "aweme_id": str(post_id), "aweme_type": 68,
        "desc": "青岛崂山旅游攻略正文", "create_time": 1700000000,
        "author": {"uid": "author", "sec_uid": "sec-author", "nickname": "旅行作者",
                   "avatar_thumb": {"url_list": ["https://avatar.test/never.png"]}},
        "statistics": {"digg_count": 1, "collect_count": 2, "comment_count": 3, "share_count": 4},
        "images": [{"uri": f"uri-{post_id}", "url_list": [
            f"https://image.test/{post_id}.jpg?signature=old",
            f"https://image.test/{post_id}.jpg?signature=fresh",
        ]}],
        "video": {"play_addr": {"url_list": ["", "https://media.test/never.mp4"]}},
        "music": {"play_url": {"uri": "https://media.test/never.mp3"}},
    }


#222 起整轮 crawler.start() 驱动不再收集。见 tests/support/douyin_issue222_deviation.py。


def test_dependency_direction():
    allowed_runtime = {
        "trippostcollect.runtime.helpers": {"extract_url_params_to_dict", "normalize_image_url"},
        "trippostcollect.runtime.cookies": {"convert_cookies", "convert_str_cookie_to_dict"},
        "trippostcollect.runtime.image_retry": {
            "ImageDownloadFetchError", "classified_http_image_error", "is_runtime_blocking_image_error",
        },
        "trippostcollect.runtime.page_capture": {
            "CaptureRule", "PageCaptureChannel", "CapturedResponse",
        },
        "trippostcollect.runtime.visit_ledger": {"VisitLedger", "content_visits_path"},
        "trippostcollect.runtime.human_pointer": {"ElementNotInView", "pointer_for"},
    }
    forbidden = {"config", "tools", "media_platform", "store", "base", "var", "model", "proxy", "cache", "scripts"}
    for path in (ROOT / "src/trippostcollect/platforms/douyin").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            for module in modules:
                assert module.split(".")[0] not in forbidden, (path, module)
                assert not module.startswith(("trippostcollect.artifacts", "trippostcollect.db", "trippostcollect.scheduler"))
                if module.startswith("trippostcollect.application"):
                    assert module == "trippostcollect.application.contracts"
                if module.startswith("trippostcollect.platforms"):
                    assert module.startswith("trippostcollect.platforms.douyin")
                if module.startswith("trippostcollect.runtime"):
                    assert isinstance(node, ast.ImportFrom) and module in allowed_runtime
                    assert {alias.name for alias in node.names} <= allowed_runtime[module]


def test_issue131_search_nil_whitelist():
    """#131：search_nil_type 只放行空值与 service_empty，其他取值按取值命名失败原因。"""
    assert parser.DOUYIN_NORMAL_SEARCH_NIL_TYPES == frozenset({"service_empty"})
    for value, reason in (("unknown", "search_nil_unknown"), ("verify_check", "search_verify_check")):
        with pytest.raises(parser.SearchResponseError) as raised:
            parser.validate_douyin_search_response(
                {"status_code": 0, "data": [], "has_more": 0, "search_nil_info": {"search_nil_type": value}})
        assert raised.value.reason == reason
    service_empty = {"status_code": 0, "data": [], "has_more": 0, "search_nil_info": {"search_nil_type": "service_empty"}}
    assert parser.validate_douyin_search_response(service_empty) is service_empty

def test_worker_assembly_from_arbitrary_cwd(tmp_path):
    argv = [
        "--platform", "dy", "--lt", "cookie", "--type", "search", "--keywords", KEYWORD,
        "--get_comment", "false", "--get_sub_comment", "false", "--get_media", "true",
        "--headless", "false", "--save_data_option", "jsonl", "--save_data_path", str(tmp_path / "batch"),
        "--start", "1", "--max_concurrency_num", "1", "--enable_ip_proxy", "false",
    ]
    source = (
        "import sys,json\n"
        "from trippostcollect.platforms.entry import configure,install_hooks,load_crawler\n"
        f"configure({argv!r})\n"
        "assert 'trippostcollect.platforms.douyin.signer' not in sys.modules\n"
        "install_hooks()\n"
        "cls=load_crawler('dy'); crawler=cls()\n"
        "assert cls.__module__ == 'trippostcollect.platforms.douyin.core'\n"
        "assert not any(n.startswith(('media_platform.douyin','store.douyin')) for n in sys.modules)\n"
        "assert crawler.ports.browser_detail_fallback is False\n"
        "print(json.dumps([load_crawler(code).__name__ for code in ('wb','zhihu','xhs')]))\n"
    )
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    result = subprocess.run([sys.executable, "-P", "-c", source], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    assert json.loads(result.stdout.splitlines()[-1]) == ["WeiboCrawler", "ZhihuCrawler", "XiaoHongShuCrawler"]


REPAIR_CASES = [(False, True), (True, True), (True, False)]
NAVIGATION_FAILED = ("browser detail response did not contain aweme_detail; attempts=["
                     "'note:reason=navigation_or_response:Error:payload_keys=[]', "
                     "'video:reason=navigation_or_response:Error:payload_keys=[]']")
REPAIR_EXPECTED = {
    (False, True): ("RuntimeError", NAVIGATION_FAILED, "NoneType", "None", [{"timeout": 30000}] * 2),
    (True, True): ("RuntimeError", NAVIGATION_FAILED, "ValueError", "原详情请求失败", [{"timeout": 30000}] * 2),
    (True, False): ("RuntimeError", "Douyin browser detail fallback requires a Playwright page",
                    "ValueError", "原详情请求失败", []),
}


