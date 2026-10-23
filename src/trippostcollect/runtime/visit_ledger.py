"""访问账本：只追加的 content_visits JSONL。

一个 worker 一个账本，记录动作、页面响应与页面状态读取（访问）、停顿、候选决定和诊断信号。
所有行共用一个单调序号 `seq`；判断先后一律比较 `seq`，不看文件行序或写入时刻。

账本不写响应体、token、查询参数值、签名头、正文、昵称、头像或完整 URL，只写 ID、路径模板、
键名、计数和哈希；写入与读取两侧都按同一组规则校验。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterator, Mapping
import uuid

from trippostcollect.runtime.helpers import utc_now


LEDGER_SCHEMA_VERSION = 1
LEDGER_MODES = frozenset({"discovery", "repair"})
KIND_PREFIXES = {"action": "a", "visit": "v", "pause": "p", "decision": "d", "signal": "s"}
ACTION_KINDS = frozenset(
    {
        "type_search",
        "press_enter",
        "dismiss_ai_chat",
        "apply_filter",
        "scroll",
        "click_card",
        "flip_image",
        "click_author",
        "close_tab",
        "close_overlay",
        "navigate",
        "go_back",
        "reload",
    }
)
VISIT_SOURCES = frozenset({"page_response", "page_state"})
PAUSE_CONTINUITY = frozenset({"completed", "failed", "n/a"})
SIGNAL_OUTCOMES = frozenset({"ignored", "verification_surfaced"})
# 账本只存标识与计数；这些键名一旦出现即视为把内容或凭据写进了账本。
FORBIDDEN_KEYS = frozenset(
    {
        "url",
        "urls",
        "href",
        "src",
        "token",
        "xsec_token",
        "cookie",
        "cookies",
        "headers",
        "body",
        "json",
        "text",
        "content",
        "desc",
        "title",
        "nickname",
        "avatar",
        "keyword",
        "query",
    }
)
MAX_STRING_LENGTH = 512
_LEDGER_ID_RE = re.compile(r"[0-9a-f]{32}")
_LINE_ID_RE = re.compile(r"(?P<ledger>[0-9a-f]{32}):(?P<prefix>[avpds])(?P<seq>\d{6,})")


class LedgerFormatError(ValueError):
    """账本行缺失、损坏或与 schema 不符。"""


class LedgerPrivacyError(ValueError):
    """待写入的值含 URL、查询值或内容字段。"""


def content_visits_path(behavior_evidence_path: str | Path) -> Path:
    """账本与行为证据同目录，推导方式与人工等待诊断相同，不经环境变量。"""
    path = Path(behavior_evidence_path).expanduser()
    return path.with_name(f"{path.stem}.content_visits.jsonl")


def line_id(ledger_id: str, kind: str, seq: int) -> str:
    return f"{ledger_id}:{KIND_PREFIXES[kind]}{int(seq):06d}"


def parse_line_id(value: str) -> tuple[str, str, int]:
    """返回 (ledger_id, kind, seq)；格式不符时抛出 LedgerFormatError。"""
    match = _LINE_ID_RE.fullmatch(str(value or ""))
    if match is None:
        raise LedgerFormatError(f"invalid ledger line id: {value!r}")
    kind = next(name for name, prefix in KIND_PREFIXES.items() if prefix == match["prefix"])
    return match["ledger"], kind, int(match["seq"])


def line_seq(value: str) -> int:
    return parse_line_id(value)[2]


def assert_private(value: Any, *, path: str = "line") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LedgerPrivacyError(f"{path}: non-string key")
            if key.lower() in FORBIDDEN_KEYS:
                raise LedgerPrivacyError(f"{path}.{key}: forbidden ledger key")
            assert_private(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_private(item, path=f"{path}[{index}]")
    elif isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            raise LedgerPrivacyError(f"{path}: string too long for a ledger value")
        if "://" in value or "?" in value:
            raise LedgerPrivacyError(f"{path}: URL or query value")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise LedgerPrivacyError(f"{path}: unsupported ledger value type {type(value).__name__}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LedgerFormatError(message)


def _optional_str(value: Any) -> bool:
    return value is None or (isinstance(value, str) and bool(value))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_header(header: Any) -> dict[str, Any]:
    _require(isinstance(header, dict), "ledger header must be an object")
    _require(header.get("kind") == "ledger", "first ledger line must be kind=ledger")
    _require(header.get("schema_version") == LEDGER_SCHEMA_VERSION, "unsupported ledger schema_version")
    _require(bool(_LEDGER_ID_RE.fullmatch(str(header.get("ledger_id") or ""))), "invalid ledger_id")
    _require(isinstance(header.get("platform"), str) and bool(header["platform"]), "ledger platform missing")
    _require(header.get("mode") in LEDGER_MODES, "invalid ledger mode")
    _require(isinstance(header.get("started_at"), str), "ledger started_at missing")
    assert_private(header)
    return header


def validate_line(line: Any, ledger_id: str) -> dict[str, Any]:
    _require(isinstance(line, dict), "ledger line must be an object")
    kind = line.get("kind")
    _require(kind in KIND_PREFIXES, f"unknown ledger line kind: {kind!r}")
    seq = line.get("seq")
    _require(_is_int(seq) and seq >= 1, "ledger line seq must be a positive integer")
    _require(line.get("id") == line_id(ledger_id, kind, seq), "ledger line id does not match kind and seq")
    _require(isinstance(line.get("at"), str), "ledger line at missing")
    _require(_optional_str(line.get("page_role")), "invalid page_role")
    if kind == "action":
        _require(line.get("action") in ACTION_KINDS, f"unknown action: {line.get('action')!r}")
        _require(isinstance(line.get("target"), dict), "action target must be an object")
        _require(_optional_str(line.get("from_visit")), "invalid action from_visit")
    elif kind == "visit":
        _require(isinstance(line.get("stage"), str) and bool(line["stage"]), "visit stage missing")
        _require(line.get("source") in VISIT_SOURCES, f"invalid visit source: {line.get('source')!r}")
        _require(isinstance(line.get("rule"), str) and bool(line["rule"]), "visit rule missing")
        _require(line.get("status") is None or _is_int(line.get("status")), "invalid visit status")
        _require(_optional_str(line.get("subject_id")), "invalid visit subject_id")
        _require(_optional_str(line.get("trigger")), "invalid visit trigger")
        _require(isinstance(line.get("recorded_at"), str), "visit recorded_at missing")
    elif kind == "pause":
        _require(isinstance(line.get("stage"), str) and bool(line["stage"]), "pause stage missing")
        start_seq = line.get("start_seq")
        _require(_is_int(start_seq) and 1 <= start_seq < seq, "pause start_seq must precede seq")
        _require(isinstance(line.get("seconds"), (int, float)) and not isinstance(line.get("seconds"), bool),
                 "pause seconds missing")
        _require(line.get("continuity") in PAUSE_CONTINUITY, "invalid pause continuity")
    elif kind == "decision":
        _require(isinstance(line.get("subject_id"), str) and bool(line["subject_id"]), "decision subject missing")
        _require(isinstance(line.get("decision"), str) and bool(line["decision"]), "decision missing")
        _require(_optional_str(line.get("from_visit")), "invalid decision from_visit")
    else:
        _require(_is_int(line.get("status")), "signal status missing")
        _require(isinstance(line.get("endpoint"), str), "signal endpoint missing")
        _require(isinstance(line.get("verify_headers_present"), bool), "signal verify flag missing")
        _require(line.get("outcome") in SIGNAL_OUTCOMES, "invalid signal outcome")
    assert_private(line)
    return line


def _encode(line: Mapping[str, Any]) -> bytes:
    return (json.dumps(line, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


class VisitLedger:
    """只追加写入；行整行经 O_APPEND 写出，每行写完即落到内核。"""

    def __init__(
        self,
        path: str | Path,
        *,
        platform: str,
        mode: str,
        ledger_id: str | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.path = Path(path).expanduser()
        self.platform = platform
        self.mode = mode
        self.ledger_id = ledger_id or uuid.uuid4().hex
        self._clock = clock
        self._seq = 0
        self._written: set[int] = set()
        self._fd: int | None = None

    @classmethod
    def create(
        cls,
        path: str | Path,
        *,
        platform: str,
        mode: str,
        ledger_id: str | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> VisitLedger:
        ledger = cls(path, platform=platform, mode=mode, ledger_id=ledger_id, clock=clock)
        ledger._open()
        return ledger

    def _open(self) -> None:
        header = validate_header(
            {
                "kind": "ledger",
                "schema_version": LEDGER_SCHEMA_VERSION,
                "ledger_id": self.ledger_id,
                "platform": self.platform,
                "mode": self.mode,
                "started_at": self._clock(),
            }
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 一个 worker 一个账本：同名文件已存在说明路径被复用，拒绝续写。
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
        self._write(_encode(header))

    @property
    def last_seq(self) -> int:
        return self._seq

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def append(
        self,
        kind: str,
        *,
        seq: int,
        page_role: str | None,
        at: str | None = None,
        **fields: Any,
    ) -> str:
        if self._fd is None:
            raise RuntimeError("visit ledger is not open")
        if not _is_int(seq) or not 1 <= seq <= self._seq:
            raise LedgerFormatError("ledger seq must be allocated by next_seq before it is written")
        if seq in self._written:
            raise LedgerFormatError(f"ledger seq {seq} is already written")
        identifier = line_id(self.ledger_id, kind, seq)
        line = {
            "kind": kind,
            "id": identifier,
            "seq": seq,
            "at": at or self._clock(),
            "page_role": page_role,
            **fields,
        }
        validate_line(line, self.ledger_id)
        self._write(_encode(line))
        self._written.add(seq)
        return identifier

    def _write(self, payload: bytes) -> None:
        assert self._fd is not None
        view = memoryview(payload)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def _parse_payload(payload: bytes, source: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not payload:
        raise LedgerFormatError(f"ledger is empty: {source.name}")
    if not payload.endswith(b"\n"):
        raise LedgerFormatError(f"ledger ends with a partial line: {source.name}")
    header: dict[str, Any] | None = None
    lines: list[dict[str, Any]] = []
    ids: set[str] = set()
    seqs: set[int] = set()
    for number, raw in enumerate(payload.split(b"\n")[:-1], start=1):
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LedgerFormatError(f"{source.name}:{number}: invalid JSON line") from exc
        if header is None:
            header = validate_header(value)
            continue
        try:
            line = validate_line(value, header["ledger_id"])
        except LedgerPrivacyError as exc:
            raise LedgerFormatError(f"{source.name}:{number}: {exc}") from exc
        except LedgerFormatError as exc:
            raise LedgerFormatError(f"{source.name}:{number}: {exc}") from exc
        if line["id"] in ids or line["seq"] in seqs:
            raise LedgerFormatError(f"{source.name}:{number}: duplicate ledger seq")
        ids.add(line["id"])
        seqs.add(line["seq"])
        lines.append(line)
    assert header is not None
    return header, lines


def iter_ledger_lines(path: str | Path) -> Iterator[dict[str, Any]]:
    """依次产出首行与全部账本行；任一行不合规即抛出 LedgerFormatError。"""
    source = Path(path).expanduser()
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise LedgerFormatError(f"cannot read ledger {source.name}: {type(exc).__name__}") from exc
    header, lines = _parse_payload(payload, source)
    yield header
    yield from lines


@dataclass(frozen=True)
class LoadedLedger:
    path: Path
    header: dict[str, Any]
    lines: tuple[dict[str, Any], ...]

    @property
    def ledger_id(self) -> str:
        return str(self.header["ledger_id"])

    @property
    def mode(self) -> str:
        return str(self.header["mode"])

    @property
    def platform(self) -> str:
        return str(self.header["platform"])


def load_ledger(path: str | Path) -> LoadedLedger:
    source = Path(path).expanduser()
    header, *lines = list(iter_ledger_lines(source))
    return LoadedLedger(path=source, header=header, lines=tuple(lines))


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def snapshot_ledger(path: str | Path, sequence: int) -> Path:
    """把当前账本逐行校验后固化为 `<账本>.batch-<seq>.snapshot`；快照名一经引用不得改变。"""
    source = Path(path).expanduser()
    payload = source.read_bytes()
    _parse_payload(payload, source)
    snapshot = source.with_name(f"{source.name}.batch-{int(sequence):06d}.snapshot")
    with snapshot.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(snapshot.parent)
    return snapshot
