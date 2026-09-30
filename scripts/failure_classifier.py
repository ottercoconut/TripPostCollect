"""失败分类的外部薄入口。"""

from trippostcollect.application.failures import *  # noqa: F403
from trippostcollect.application.failures import (
    _XHS_SMS_CONTEXT as _XHS_SMS_CONTEXT,
    _XHS_SMS_VERIFICATION as _XHS_SMS_VERIFICATION,
    _XHS_SMS_PARAMETER_ERROR as _XHS_SMS_PARAMETER_ERROR,
    _XHS_DAILY as _XHS_DAILY,
    _XHS_LIMIT as _XHS_LIMIT,
    _xhs_stable_sms_terminal_reason as _xhs_stable_sms_terminal_reason,
    _xhs_sms_terminal_reason as _xhs_sms_terminal_reason,
    _platform_security_limit_reason as _platform_security_limit_reason,
    _meta_markers as _meta_markers,
    _text_blob as _text_blob,
    _without_false_security_markers as _without_false_security_markers,
    _stdout_without_json_payload as _stdout_without_json_payload,
    _terminal_stdout_payload as _terminal_stdout_payload,
    _strong_child_classification as _strong_child_classification,
    _structured_runtime_blocker as _structured_runtime_blocker,
)
