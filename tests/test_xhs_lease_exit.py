"""Memory-only __exit__ regression tests; these do not prove OS integration.

No acquisition, database, browser, subprocess, network or real signal is used.
Resource ownership/release assertions remain in the existing integration tests.
"""

import signal
import sys

import pytest

from trippostcollect.xhs.leases import (
    LeaseGuard,
    XhsLeaseProcessesAlive,
    XhsLeaseSignal,
)
from trippostcollect.xhs.terminal import XhsTerminalSignal


SENSITIVE = "Cookie=secret-cookie; token=secret-token; avatar=https://private/avatar"
PRIMARY_FACTORIES = (
    pytest.param(lambda: ValueError(SENSITIVE), id="value-error"),
    pytest.param(lambda: KeyboardInterrupt(SENSITIVE), id="keyboard-interrupt"),
    pytest.param(lambda: XhsLeaseSignal(signal.SIGINT), id="lease-sigint"),
    pytest.param(lambda: XhsLeaseSignal(signal.SIGTERM), id="lease-sigterm"),
    pytest.param(lambda: XhsTerminalSignal(signal.SIGINT), id="terminal-sigint"),
    pytest.param(lambda: XhsTerminalSignal(signal.SIGTERM), id="terminal-sigterm"),
)
CLEANUP_FACTORIES = (
    pytest.param(lambda: True, id="true"),
    pytest.param(lambda: False, id="false"),
    pytest.param(lambda: RuntimeError(SENSITIVE), id="runtime-error"),
    pytest.param(lambda: KeyboardInterrupt(SENSITIVE), id="keyboard-interrupt"),
    pytest.param(lambda: XhsLeaseSignal(signal.SIGINT), id="lease-sigint"),
    pytest.param(lambda: XhsLeaseSignal(signal.SIGTERM), id="lease-sigterm"),
    pytest.param(lambda: XhsTerminalSignal(signal.SIGINT), id="terminal-sigint"),
    pytest.param(lambda: XhsTerminalSignal(signal.SIGTERM), id="terminal-sigterm"),
    pytest.param(lambda: BaseException(SENSITIVE), id="base-exception"),
)


class MemoryOnlyGuard:
    __exit__ = LeaseGuard.__exit__

    def __init__(self, result):
        self.result = result
        self.calls = 0
        self.body_traceback = None

    def __enter__(self):
        return self

    def close(self):
        self.calls += 1
        self.body_traceback = sys.exception().__traceback__ if sys.exception() else None
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.parametrize("primary_factory", PRIMARY_FACTORIES)
@pytest.mark.parametrize("cleanup_factory", CLEANUP_FACTORIES)
def test_primary_object_traceback_and_chain_survive(primary_factory, cleanup_factory):
    primary = primary_factory()
    cause = LookupError("existing cause")
    context = ArithmeticError("existing context")
    primary.__cause__ = cause
    primary.__context__ = context
    primary.add_note("existing note")
    signum = getattr(primary, "signum", None)
    cleanup = cleanup_factory()
    guard = MemoryOnlyGuard(cleanup)

    with pytest.raises(BaseException) as caught:
        with guard:
            raise primary

    assert caught.value is primary
    assert caught.value.__traceback__ is guard.body_traceback
    assert primary.__cause__ is cause
    assert primary.__context__ is context
    assert primary.__suppress_context__ is True
    assert getattr(primary, "signum", None) == signum
    assert guard.calls == 1
    if isinstance(cleanup, BaseException):
        assert cleanup.__context__ is primary
        assert len(primary.__notes__) == 2
        note = primary.__notes__[-1]
        assert "LeaseGuard.__exit__: close raised " in note
        assert "lease release unconfirmed" in note
        assert SENSITIVE not in note
        assert all(marker not in note for marker in ("secret-cookie", "secret-token", "https://private/avatar"))
    else:
        assert primary.__notes__ == ["existing note"]


@pytest.mark.parametrize("cleanup_factory", CLEANUP_FACTORIES)
def test_without_primary_preserves_close_contract(cleanup_factory):
    cleanup = cleanup_factory()
    guard = MemoryOnlyGuard(cleanup)
    if cleanup is True:
        with guard:
            pass
    else:
        with pytest.raises(BaseException) as caught:
            with guard:
                pass
        if cleanup is False:
            assert type(caught.value) is XhsLeaseProcessesAlive
        else:
            assert caught.value is cleanup
        assert caught.value.__context__ is None
    assert guard.calls == 1


class OverriddenNoteError(ValueError):
    def add_note(self, note):
        raise AssertionError("overridden add_note must not run")


class BrokenNoteAccessError(ValueError):
    def __getattribute__(self, name):
        if name == "__notes__":
            raise XhsTerminalSignal(signal.SIGTERM)
        return super().__getattribute__(name)


@pytest.mark.parametrize("mode", ("overridden", "malformed", "attribute-interrupt"))
def test_note_failures_cannot_replace_primary(mode):
    if mode == "overridden":
        primary = OverriddenNoteError(SENSITIVE)
    elif mode == "malformed":
        primary = ValueError(SENSITIVE)
        primary.__notes__ = 42
    else:
        primary = BrokenNoteAccessError(SENSITIVE)
    cause, context = ValueError("cause"), ValueError("context")
    primary.__cause__, primary.__context__ = cause, context
    guard = MemoryOnlyGuard(RuntimeError(SENSITIVE))
    with pytest.raises(BaseException) as caught:
        with guard:
            raise primary
    assert caught.value is primary
    assert primary.__traceback__ is guard.body_traceback
    assert primary.__cause__ is cause
    assert primary.__context__ is context
    assert guard.calls == 1
    if mode == "overridden":
        assert "close raised RuntimeError" in primary.__notes__[0]
    elif mode == "malformed":
        assert primary.__notes__ == 42


def test_wrapped_probe_failure_keeps_primary_chain_without_leaking_details():
    primary = ValueError(SENSITIVE)

    class WrappedProbeGuard(MemoryOnlyGuard):
        def close(self):
            self.calls += 1
            self.body_traceback = sys.exception().__traceback__
            try:
                raise PermissionError(SENSITIVE)
            except PermissionError as probe_error:
                raise RuntimeError(SENSITIVE) from probe_error

    guard = WrappedProbeGuard(None)
    with pytest.raises(ValueError) as caught:
        with guard:
            raise primary
    assert caught.value is primary
    assert primary.__traceback__ is guard.body_traceback
    assert primary.__cause__ is None
    assert primary.__context__ is None
    assert primary.__suppress_context__ is False
    assert primary.__notes__ == [
        "LeaseGuard.__exit__: close raised RuntimeError; "
        "primary exception preserved; lease release unconfirmed."
    ]
    assert guard.calls == 1


def test_custom_cleanup_name_and_string_are_not_diagnostic_input():
    def forbidden_string(self):
        pytest.fail("cleanup exception must not be formatted")

    cleanup_type = type(SENSITIVE, (RuntimeError,), {"__str__": forbidden_string})
    primary = ValueError(SENSITIVE)
    guard = MemoryOnlyGuard(cleanup_type(SENSITIVE))
    with pytest.raises(ValueError) as caught:
        with guard:
            raise primary
    assert caught.value is primary
    assert "close raised RuntimeError" in primary.__notes__[0]
    assert SENSITIVE not in primary.__notes__[0]
    assert guard.calls == 1
