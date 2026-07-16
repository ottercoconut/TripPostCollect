from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MEDIACRAWLER_TOOLS = ROOT / "tools" / "MediaCrawler" / "tools"
if str(MEDIACRAWLER_TOOLS) not in sys.path:
    sys.path.insert(0, str(MEDIACRAWLER_TOOLS))

trippostcollect_adaptive = import_module("trippostcollect_adaptive")


def test_stagnation_counts_batches_without_new_valid_records(monkeypatch) -> None:
    monkeypatch.setattr(trippostcollect_adaptive, "append_execution_event", lambda *args, **kwargs: None)
    accumulator = trippostcollect_adaptive.AdaptiveAccumulator(
        platform="xhs",
        hard_limit=20,
        target_new=5,
        max_stagnant_batches=2,
    )

    accumulator.begin_batch()
    accumulator.consider("candidate-1", valid=False)
    assert accumulator.finish_batch(source_page=1) is False
    assert accumulator.stagnant_batches == 1

    accumulator.begin_batch()
    accumulator.consider("candidate-2", valid=False)
    assert accumulator.finish_batch(source_page=2) is True
    assert accumulator.stop_reason == "stagnated"


def test_new_valid_record_resets_stagnation(monkeypatch) -> None:
    monkeypatch.setattr(trippostcollect_adaptive, "append_execution_event", lambda *args, **kwargs: None)
    accumulator = trippostcollect_adaptive.AdaptiveAccumulator(
        platform="xhs",
        hard_limit=20,
        target_new=5,
        max_stagnant_batches=3,
    )

    accumulator.begin_batch()
    accumulator.consider("candidate-1", valid=False)
    accumulator.finish_batch(source_page=1)
    accumulator.begin_batch()
    accumulator.consider("candidate-2", valid=True)
    accumulator.finish_batch(source_page=2)

    assert accumulator.stagnant_batches == 0
