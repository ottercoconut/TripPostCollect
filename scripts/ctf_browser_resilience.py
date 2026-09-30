#!/usr/bin/env python3
"""页面证据辅助接口，保留原名导出。"""

from trippostcollect.application.page_evidence import (
    is_douyin_target as is_douyin_target,
    clean_douyin_profile_cookies as clean_douyin_profile_cookies,
    clear_douyin_context_cookies as clear_douyin_context_cookies,
)
from trippostcollect.runtime.page_readiness import (
    page_readiness_state as page_readiness_state,
    wait_for_content_ready as wait_for_content_ready,
    wait_for_content_enrichment as wait_for_content_enrichment,
    navigate_with_commit_and_readiness as navigate_with_commit_and_readiness,
)
