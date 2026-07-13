from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

failure_classifier = import_module("failure_classifier")


def test_formal_target_failure_beats_incidental_rate_text() -> None:
    stdout = json.dumps(
        {
            "import_new_target_met": False,
            "failure_reason": "import_new_target_not_met",
            "formal_validation": {
                "new_target_met": False,
                "stop_reason": "stagnated",
                "invalid_reason_counts": {"raw_text_containing_429": 1},
            },
        }
    )

    result = failure_classifier.classify_attempt(exit_code=2, stdout=stdout)

    assert result["status"] == "retry_wait"
    assert result["failure_type"] == "import_new_target_not_met"
    assert result["reason"] == "formal_import_new_target_not_reached:stagnated"
