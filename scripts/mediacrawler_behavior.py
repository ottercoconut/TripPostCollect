#!/usr/bin/env python3
"""Required human-behavior stage for project-managed MediaCrawler runs."""

from __future__ import annotations

from functools import partial

from trippostcollect.artifacts.evidence import write_evidence
from trippostcollect.runtime import behavior as _behavior
from trippostcollect.runtime.helpers import utc_now as utc_now
from trippostcollect.runtime.behavior import (
    BLOCKED_VISIBLE_RE as BLOCKED_VISIBLE_RE,
    CAPTCHA_VISIBLE_RE as CAPTCHA_VISIBLE_RE,
    HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS as HUMAN_BEHAVIOR_TIMEOUT_BUDGET_SECONDS,
    LOGIN_VISIBLE_RE as LOGIN_VISIBLE_RE,
    RATE_LIMIT_VISIBLE_RE as RATE_LIMIT_VISIBLE_RE,
    REQUEST_RANDOM as REQUEST_RANDOM,
    REQUIRED_BEHAVIOR_EVENTS as REQUIRED_BEHAVIOR_EVENTS,
    XHS_CAPTCHA_URL_RE as XHS_CAPTCHA_URL_RE,
    XHS_LOGIN_URL_RE as XHS_LOGIN_URL_RE,
    XHS_PLATFORM_SECURITY_LIMIT_RE as XHS_PLATFORM_SECURITY_LIMIT_RE,
    XHS_PLATFORM_SECURITY_LIMIT_URL_RE as XHS_PLATFORM_SECURITY_LIMIT_URL_RE,
    XHS_VISIBLE_CHECK_INTERVAL_SECONDS as XHS_VISIBLE_CHECK_INTERVAL_SECONDS,
    behavior_evidence_valid as behavior_evidence_valid,
    dwell_on_list_with_checks as dwell_on_list_with_checks,
    effective_scroll_recorded as effective_scroll_recorded,
    runtime_fingerprint as runtime_fingerprint,
    runtime_fingerprint_valid as runtime_fingerprint_valid,
    visible_challenge as visible_challenge,
    visible_page_state as visible_page_state,
)

from trippostcollect.runtime.human_flow import (
    dwell_on_list as dwell_on_list,
)


# T09：小红书行为实现迁入 trippostcollect.platforms.xhs.behavior；旧名重导出，证据写出沿用本入口的出口。
from trippostcollect.platforms.xhs import behavior as _xhs_behavior  # noqa: E402
from trippostcollect.platforms.xhs.behavior import (  # noqa: E402
    XHS_SEARCH_READY_TIMEOUT_SECONDS as XHS_SEARCH_READY_TIMEOUT_SECONDS,
    XHS_CONTINUITY_VERIFY_WAIT_SECONDS as XHS_CONTINUITY_VERIFY_WAIT_SECONDS,
    XHS_CONTINUITY_VERIFY_POLL_SECONDS as XHS_CONTINUITY_VERIFY_POLL_SECONDS,
    XHS_POST_INTERACTION_MODES as XHS_POST_INTERACTION_MODES,
    XHS_OPERATOR_CHALLENGES as XHS_OPERATOR_CHALLENGES,
    XHS_COMMENT_SELECTORS as XHS_COMMENT_SELECTORS,
    XHS_LIKE_SELECTORS as XHS_LIKE_SELECTORS,
    wait_for_xhs_search_ready as wait_for_xhs_search_ready,
    _first_visible_locator as _first_visible_locator,
    _like_control_state as _like_control_state,
    _looks_liked as _looks_liked,
    _looks_unliked as _looks_unliked,
    _run_xhs_comment_scroll as _run_xhs_comment_scroll,
    _run_xhs_like_once as _run_xhs_like_once,
)

record_xhs_platform_security_limit = partial(_xhs_behavior.record_xhs_platform_security_limit, write_evidence=write_evidence)
wait_for_xhs_continuity_verification = partial(_xhs_behavior.wait_for_xhs_continuity_verification, write_evidence=write_evidence)
run_xhs_api_captcha_verification = partial(_xhs_behavior.run_xhs_api_captcha_verification, write_evidence=write_evidence)
persist_xhs_continuity_failure = partial(_xhs_behavior.persist_xhs_continuity_failure, write_evidence=write_evidence)
run_xhs_continuity_behavior = partial(_xhs_behavior.run_xhs_continuity_behavior, write_evidence=write_evidence)
run_xhs_post_interaction = partial(_xhs_behavior.run_xhs_post_interaction, write_evidence=write_evidence)


async def run_page_behavior(*args, **kwargs):
    """为旧入口注入小红书等待与证据出口。"""
    kwargs.setdefault("xhs_search_ready", wait_for_xhs_search_ready)
    kwargs.setdefault("write_evidence", write_evidence)
    return await _behavior.run_page_behavior(*args, **kwargs)


async def run_guarded_request_pause(*args, **kwargs):
    """为旧入口注入证据出口。"""
    kwargs.setdefault("write_evidence", write_evidence)
    return await _behavior.run_guarded_request_pause(*args, **kwargs)
