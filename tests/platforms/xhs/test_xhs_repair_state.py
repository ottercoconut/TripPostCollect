"""小红书历史详情修复：运行级阻断按异常类型判定、显式分支与失败码。"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from trippostcollect.platforms.xhs import errors, repair


ROOT = Path(__file__).resolve().parents[3]


def test_repair_blocking_uses_exception_types_not_text() -> None:
    """自 T09 迁入：阻断按根异常类型判定；普通候选失败不阻断。"""
    crawler = SimpleNamespace(_request_failure_exception=lambda exc: exc)
    plain_ip = errors.IPBlockError("Network connection error, please check network settings or restart")
    plain_runtime = errors.PlatformRuntimeError(
        "XHS platform security limit, code 300011", code="platform_security_limit_300011",
    )
    assert repair._repair_exception_is_blocking(crawler, plain_ip) is True
    assert repair._repair_exception_is_blocking(crawler, plain_runtime) is True
    assert repair._repair_exception_is_blocking(crawler, RuntimeError("普通失败")) is False
    assert repair._repair_exception_is_blocking(crawler, RuntimeError("captcha visible")) is True
    assert repair._xhs_repair_blocker(crawler, plain_ip) == {"error_type": "IPBlockError", "error_code": "ip_blocked"}
    assert repair._xhs_repair_failure_scope(RuntimeError()) == "candidate"
    failure = repair._xhs_repair_failure(note_id="r9", batch=2, failure_scope="detail", error_code="note_not_found")
    assert failure["retryable"] is False and failure["identity"] == "xhs:id:r9"


def test_root_repair_imports_root_errors_explicitly() -> None:
    path = ROOT / "src/trippostcollect/platforms/xhs/repair.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        and node.module == "trippostcollect.platforms.xhs.errors"
        for alias in node.names
    }
    assert {"IPBlockError", "PlatformRuntimeError"} <= imported
    assert "sys.modules" not in path.read_text(encoding="utf-8")


def test_root_repair_is_an_explicit_branch_of_the_detail_mixin() -> None:
    from trippostcollect.platforms.xhs.core import XiaoHongShuCrawler

    assert XiaoHongShuCrawler.get_specified_notes.__module__ == "trippostcollect.platforms.xhs.detail"
    assert not hasattr(XiaoHongShuCrawler, "_trippostcollect_repair_resilience")
