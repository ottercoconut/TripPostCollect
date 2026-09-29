"""采集应用与运行监督之间的异常契约。"""

from __future__ import annotations




class XhsRuntimeSupervisionError(RuntimeError):
    """Raised when authenticated XHS runtime supervision can no longer continue."""
