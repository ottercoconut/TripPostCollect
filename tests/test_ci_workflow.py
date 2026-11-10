"""#149：锁定 macOS CI 的触发规则与超时，防止 draft、并发或超时设置被无意改回。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "macos-test-lanes.yml").read_text(encoding="utf-8")


def test_draft_pull_requests_do_not_run_and_ready_for_review_triggers() -> None:
    assert "types: [opened, synchronize, reopened, ready_for_review]" in WORKFLOW
    assert "if: github.event_name != 'pull_request' || !github.event.pull_request.draft" in WORKFLOW
    assert "workflow_dispatch:" in WORKFLOW


def test_new_push_cancels_previous_run_of_same_pull_request_only() -> None:
    assert "group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.run_id }}" in WORKFLOW
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in WORKFLOW


def test_lane_step_times_out_before_job_so_artifacts_still_upload() -> None:
    lines = WORKFLOW.splitlines()
    job_timeout = next(line for line in lines if line.startswith("    timeout-minutes:"))
    step_timeout = next(line for line in lines if line.startswith("        timeout-minutes:"))
    assert job_timeout.split(":")[1].strip() == "18"
    assert step_timeout.split(":")[1].strip() == "15"
    upload = WORKFLOW.index("仅上传计数与脱敏控制证据")
    assert "if: always()" in WORKFLOW[upload:upload + 200]
# #149 验收用临时改动，不合入。
