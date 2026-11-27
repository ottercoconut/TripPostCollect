"""知乎测试默认去掉详情打开间隔下限，避免假页面连开时真的等待；间隔本身由专门的测试覆盖。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_detail_open_spacing(monkeypatch: pytest.MonkeyPatch) -> None:
    import trippostcollect.platforms.zhihu.detail_page as detail_page

    monkeypatch.setattr(detail_page, "DETAIL_OPEN_MIN_INTERVAL_SECONDS", (0.0, 0.0))
