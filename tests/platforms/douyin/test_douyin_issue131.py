"""#131：search_nil_type 只放行空值与 service_empty，其他取值不得当作来源耗尽。"""

from __future__ import annotations

import json

import pytest

from trippostcollect.platforms.douyin.models import SearchResponseError
from trippostcollect.platforms.douyin.parser import (
    decode_douyin_json_body,
    validate_douyin_search_response,
)


def _payload(search_nil_type: str | None, *, data=None, has_more: int = 0) -> dict:
    payload = {"status_code": 0, "data": [] if data is None else data, "has_more": has_more}
    if search_nil_type is not None:
        payload["search_nil_info"] = {"search_nil_type": search_nil_type}
    return payload


def _framed(*documents: dict) -> bytes:
    body = b""
    for document in documents:
        chunk = json.dumps(document, separators=(",", ":")).encode()
        body += f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n"
    return body + b"0\r\n\r\n"


def test_unknown_first_page_is_runtime_failure_not_exhaustion() -> None:
    # 10-31 03:39Z 实测：首页 1 条非作品卡片、has_more=0、search_nil_type=unknown。
    payload = _payload("unknown", data=[{"type": 999, "card_info": {}}])
    with pytest.raises(SearchResponseError) as raised:
        validate_douyin_search_response(payload)
    assert raised.value.reason == "search_nil_unknown"


@pytest.mark.parametrize("value", ["unknown", "risk_control", "normal_empty"])
def test_any_unlisted_search_nil_type_is_rejected(value: str) -> None:
    with pytest.raises(SearchResponseError) as raised:
        validate_douyin_search_response(_payload(value))
    assert raised.value.reason == f"search_nil_{value}"


@pytest.mark.parametrize("value", [None, ""])
def test_empty_search_nil_type_is_normal(value: str | None) -> None:
    payload = _payload(value, data=[{"aweme_info": {"aweme_id": "1"}}], has_more=1)
    payload["extra"] = {"logid": "stable-search-id"}
    assert validate_douyin_search_response(payload) is payload


def test_service_empty_at_chain_end_is_normal() -> None:
    # 08-27、08-31 实测：深层链尾 data=[]、has_more=0、search_nil_type=service_empty。
    payload = _payload("service_empty")
    assert validate_douyin_search_response(payload) is payload


def test_verify_check_still_reports_search_verify_check() -> None:
    with pytest.raises(SearchResponseError) as raised:
        validate_douyin_search_response(_payload("verify_check"))
    assert raised.value.reason == "search_verify_check"


def test_stream_merge_keeps_unknown_search_nil_info() -> None:
    framed = _framed(
        {"status_code": 0, "data": [{"type": 999}], "has_more": 0, "search_nil_info": {"search_nil_type": "unknown"}},
        {"status_code": 0, "data": [], "has_more": 0},
    )
    merged = decode_douyin_json_body(framed)
    assert merged["search_nil_info"] == {"search_nil_type": "unknown"}
    with pytest.raises(SearchResponseError) as raised:
        validate_douyin_search_response(merged)
    assert raised.value.reason == "search_nil_unknown"


def test_stream_merge_prefers_verify_check_over_other_values() -> None:
    framed = _framed(
        {"status_code": 0, "data": [], "has_more": 0, "search_nil_info": {"search_nil_type": "unknown"}},
        {"status_code": 0, "data": [], "has_more": 0, "search_nil_info": {"search_nil_type": "verify_check"}},
    )
    with pytest.raises(SearchResponseError) as raised:
        validate_douyin_search_response(decode_douyin_json_body(framed))
    assert raised.value.reason == "search_verify_check"
