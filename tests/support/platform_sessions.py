"""T14：测试进程内把 platform_sessions 根重定向到临时目录。"""

from __future__ import annotations

from pathlib import Path


def redirect_platform_session_roots(monkeypatch, root: Path) -> Path:
    """根只在 core.paths 内按名读取（其余模块导入的是函数），改模块属性即对进程内全部调用生效。"""
    from trippostcollect.core import paths

    sessions = root / "platform_sessions"
    monkeypatch.setattr(paths, "PLATFORM_SESSIONS_ROOT", sessions)
    return sessions


def child_redirect_source(root: Path) -> str:
    """子进程不继承进程内重定向；probe 源码在子进程内做同样的重定向，不新增环境变量。"""
    return (
        "from pathlib import Path as _T14Path\n"
        "from trippostcollect.core import paths as _t14_paths\n"
        f"_t14_paths.PLATFORM_SESSIONS_ROOT = _T14Path({str(root / 'platform_sessions')!r})\n"
    )
