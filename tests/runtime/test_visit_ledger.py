"""访问账本：首行、行 id、整行追加、快照与坏行检测、隐私规则。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from trippostcollect.runtime.visit_ledger import (
    LedgerFormatError,
    LedgerPrivacyError,
    VisitLedger,
    content_visits_path,
    iter_ledger_lines,
    line_seq,
    load_ledger,
    parse_line_id,
    snapshot_ledger,
)


LEDGER_ID = "0123456789abcdef0123456789abcdef"


def clock() -> str:
    return "2026-10-11T00:00:00+00:00"


def new_ledger(tmp_path: Path, **kwargs) -> VisitLedger:
    return VisitLedger.create(
        tmp_path / "logs" / "behavior_evidence.content_visits.jsonl",
        platform="xhs",
        mode=kwargs.pop("mode", "discovery"),
        ledger_id=LEDGER_ID,
        clock=clock,
        **kwargs,
    )


def raw_lines(ledger: VisitLedger) -> list[dict]:
    return [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()]


def test_content_visits_path_sits_beside_behavior_evidence(tmp_path):
    evidence = tmp_path / "logs" / "xhs" / "behavior_evidence.json"
    assert content_visits_path(evidence) == evidence.with_name("behavior_evidence.content_visits.jsonl")


def test_header_is_first_line_and_ids_encode_kind_and_seq(tmp_path):
    ledger = new_ledger(tmp_path)
    action_id = ledger.append(
        "action", seq=ledger.next_seq(), page_role="primary", action="click_card",
        target={"note_id": "n1", "data_index": 3}, from_visit=None,
    )
    pause_seq = ledger.next_seq()
    pause_id = ledger.append(
        "pause", seq=ledger.next_seq(), page_role="primary", stage="note_detail",
        start_seq=pause_seq, seconds=4.5, continuity="n/a",
    )
    lines = raw_lines(ledger)
    assert lines[0] == {
        "kind": "ledger", "schema_version": 1, "ledger_id": LEDGER_ID, "platform": "xhs",
        "mode": "discovery", "started_at": clock(),
    }
    assert action_id == f"{LEDGER_ID}:a000001"
    assert pause_id == f"{LEDGER_ID}:p000003"
    assert parse_line_id(pause_id) == (LEDGER_ID, "pause", 3)
    assert line_seq(action_id) == 1
    assert [line["seq"] for line in lines[1:]] == [1, 3]
    assert ledger.path.read_bytes().endswith(b"\n")


def test_random_ledger_id_is_uuid_hex(tmp_path):
    ledger = VisitLedger.create(tmp_path / "a.jsonl", platform="xhs", mode="repair")
    assert len(ledger.ledger_id) == 32 and int(ledger.ledger_id, 16) >= 0
    assert load_ledger(ledger.path).mode == "repair"


def test_ledger_path_is_never_reused(tmp_path):
    ledger = new_ledger(tmp_path)
    ledger.close()
    with pytest.raises(FileExistsError):
        new_ledger(tmp_path)


def test_invalid_mode_is_rejected(tmp_path):
    with pytest.raises(LedgerFormatError):
        VisitLedger.create(tmp_path / "a.jsonl", platform="xhs", mode="refresh")


def test_visit_lines_may_carry_earlier_arrival_seq_once(tmp_path):
    ledger = new_ledger(tmp_path)
    arrival = ledger.next_seq()
    later = ledger.next_seq()
    ledger.append("decision", seq=later, page_role=None, subject_id="n1", decision="video_skipped_from_search",
                  from_visit=None)
    ledger.append(
        "visit", seq=arrival, page_role="primary", stage="search_results", source="page_response",
        rule="xhs.search_notes", status=200, subject_id=None, trigger=None, recorded_at=clock(),
    )
    with pytest.raises(LedgerFormatError):
        ledger.append(
            "visit", seq=arrival, page_role="primary", stage="search_results", source="page_response",
            rule="xhs.search_notes", status=200, subject_id=None, trigger=None, recorded_at=clock(),
        )
    with pytest.raises(LedgerFormatError):
        ledger.append("decision", seq=99, page_role=None, subject_id="n1", decision="x", from_visit=None)
    loaded = load_ledger(ledger.path)
    assert [line["seq"] for line in loaded.lines] == [later, arrival]
    assert loaded.ledger_id == LEDGER_ID and loaded.platform == "xhs"


@pytest.mark.parametrize(
    "fields",
    [
        {"target": {"href": "/explore/n1"}},
        {"target": {"xsec_token": "abc"}},
        {"target": {"note_id": "n1", "avatar": "a"}},
        {"target": {"nickname": "someone"}},
        {"target": {"desc": "正文"}},
        {"target": {"keyword": "青岛"}},
        {"target": {"route": "https://www.xiaohongshu.com/explore/n1"}},
        {"target": {"route": "/explore/n1?xsec_token=abc"}},
        {"target": {"note_id": "x" * 600}},
        {"target": {"blob": b"bytes"}},
    ],
)
def test_privacy_rules_reject_urls_tokens_and_content(tmp_path, fields):
    ledger = new_ledger(tmp_path)
    before = ledger.path.read_bytes()
    with pytest.raises(LedgerPrivacyError):
        ledger.append("action", seq=ledger.next_seq(), page_role="primary", action="scroll",
                      from_visit=None, **fields)
    assert ledger.path.read_bytes() == before


def test_unknown_action_kind_is_rejected(tmp_path):
    ledger = new_ledger(tmp_path)
    with pytest.raises(LedgerFormatError):
        ledger.append("action", seq=ledger.next_seq(), page_role="primary", action="fetch", target={},
                      from_visit=None)


def write(path: Path, lines: list, *, trailing_newline: bool = True) -> Path:
    text = "\n".join(line if isinstance(line, str) else json.dumps(line, ensure_ascii=False) for line in lines)
    path.write_text(text + ("\n" if trailing_newline else ""), encoding="utf-8")
    return path


HEADER = {"kind": "ledger", "schema_version": 1, "ledger_id": LEDGER_ID, "platform": "xhs",
          "mode": "discovery", "started_at": "t"}
ACTION = {"kind": "action", "id": f"{LEDGER_ID}:a000001", "seq": 1, "at": "t", "page_role": "primary",
          "action": "scroll", "target": {}, "from_visit": None}


@pytest.mark.parametrize(
    "lines,trailing",
    [
        ([], True),
        ([ACTION], True),
        ([HEADER, "{not json"], True),
        ([HEADER, ACTION], False),
        ([HEADER, [1, 2]], True),
        ([HEADER, {**ACTION, "id": f"{LEDGER_ID}:v000001"}], True),
        ([HEADER, {**ACTION, "seq": 0, "id": f"{LEDGER_ID}:a000000"}], True),
        ([HEADER, ACTION, ACTION], True),
        ([HEADER, {**ACTION, "kind": "fetch"}], True),
        ([HEADER, {**ACTION, "target": {"url": "/x"}}], True),
        ([{**HEADER, "schema_version": 2}, ACTION], True),
        ([HEADER, {**ACTION, "kind": "pause", "id": f"{LEDGER_ID}:p000001", "stage": "x", "start_seq": 1,
                   "seconds": 1, "continuity": "n/a"}], True),
    ],
    ids=[
        "empty", "missing_header", "bad_json", "partial_last_line", "non_object", "id_kind_mismatch",
        "seq_zero", "duplicate_seq", "unknown_kind", "privacy_key", "schema_version",
        "pause_start_not_before_seq",
    ],
)
def test_bad_lines_are_detected(tmp_path, lines, trailing):
    path = write(tmp_path / "ledger.jsonl", lines, trailing_newline=trailing)
    with pytest.raises(LedgerFormatError):
        list(iter_ledger_lines(path))


def test_missing_file_is_a_format_error(tmp_path):
    with pytest.raises(LedgerFormatError):
        load_ledger(tmp_path / "absent.jsonl")


def test_snapshot_copies_validated_bytes_once(tmp_path):
    ledger = new_ledger(tmp_path)
    ledger.append("action", seq=ledger.next_seq(), page_role="primary", action="press_enter", target={},
                  from_visit=None)
    snapshot = snapshot_ledger(ledger.path, 7)
    assert snapshot.name == "behavior_evidence.content_visits.jsonl.batch-000007.snapshot"
    assert snapshot.read_bytes() == ledger.path.read_bytes()
    assert sha256(snapshot.read_bytes()).hexdigest() == sha256(ledger.path.read_bytes()).hexdigest()
    # 快照名一经引用不得改变：同序号再次固化失败，原快照不变。
    ledger.append("action", seq=ledger.next_seq(), page_role="primary", action="scroll", target={},
                  from_visit=None)
    with pytest.raises(FileExistsError):
        snapshot_ledger(ledger.path, 7)
    assert len(load_ledger(snapshot).lines) == 1


def test_snapshot_refuses_corrupt_ledger(tmp_path):
    path = write(tmp_path / "ledger.jsonl", [HEADER, "{broken"])
    with pytest.raises(LedgerFormatError):
        snapshot_ledger(path, 1)
    assert not path.with_name("ledger.jsonl.batch-000001.snapshot").exists()
