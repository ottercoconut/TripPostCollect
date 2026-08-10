from __future__ import annotations

from urllib.parse import unquote

import pytest

from trippostcollect.core.scope import (
    is_qingdao_topic_keyword,
    require_qingdao_topic_keyword,
)
from trippostcollect.platforms.registry import SITES


@pytest.mark.parametrize("keyword", ["青岛旅游", "青岛西海岸攻略", "崂山攻略"])
def test_qingdao_topic_scope_accepts_project_keywords(keyword: str) -> None:
    assert is_qingdao_topic_keyword(keyword)
    assert require_qingdao_topic_keyword(f"  {keyword}  ") == keyword


@pytest.mark.parametrize("keyword", [None, "", "济南旅游", "烟台攻略"])
def test_qingdao_topic_scope_rejects_other_keywords(keyword: object) -> None:
    assert not is_qingdao_topic_keyword(keyword)
    with pytest.raises(ValueError, match="must start with one of: 青岛、崂山"):
        require_qingdao_topic_keyword(keyword)


def test_qingdao_topic_scope_rejects_marker_after_another_city() -> None:
    assert not is_qingdao_topic_keyword("济南到青岛旅游")


def test_all_platform_default_surfaces_are_qingdao_scoped() -> None:
    for site in SITES.values():
        assert "青岛" in unquote(site.default_url), site.key
