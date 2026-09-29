"""共享抓取策略的外部薄入口。"""

from trippostcollect.application.policy import *  # noqa: F403
from trippostcollect.application.policy import (
    _load_policy_state_unlocked as _load_policy_state_unlocked,
    _save_policy_state_unlocked as _save_policy_state_unlocked,
    _current_daily_key as _current_daily_key,
    _ceil_positive_seconds as _ceil_positive_seconds,
    _seconds_until_next_utc_day as _seconds_until_next_utc_day,
    _automatic_session_cooldown_until as _automatic_session_cooldown_until,
    _normalize_entry as _normalize_entry,
    _blocked_event as _blocked_event,
    _compute_pacing_wait as _compute_pacing_wait,
)
