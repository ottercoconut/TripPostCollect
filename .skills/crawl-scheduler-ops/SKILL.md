---
name: crawl-scheduler-ops
description: Use only for TripPostCollect crawl scheduler configuration, due-job selection, behavior profiles, throttle/headed settings, run reports, and scheduler failure classification.
---

# Crawl Scheduler Ops

## Trigger

- User asks which crawl jobs will run, why a job ran or did not run, or how to schedule a platform.
- Changes touch `config/crawl_targets.json`, `scripts/crawl_runner.py`, `scripts/failure_classifier.py`, or scheduler schema.
- A `run_summary.json`, `crawl_attempts`, or `crawl_run_reports` issue needs investigation.

## Inputs

- `docs/crawl-architecture.md`
- `docs/anti-automation-behavior.md` when schedule, headed/headless, throttle, cooldown, or behavior profile changes
- `config/crawl_targets.json`
- `scripts/crawl_runner.py`
- `scripts/failure_classifier.py`
- `scripts/db_bootstrap.py`
- `db/crawl_scheduler.sql`

## Workflow

1. Validate `config/crawl_targets.json` syntax before reasoning about jobs.
2. Inspect the target job: `job_key`, `site_key`, `job_kind`, enabled flag, priority, schedule, params, and behavior profile.
3. For anti-automation-sensitive changes, check `docs/anti-automation-behavior.md`; keep scheduled intervals compatible with site policy, avoid `--no-throttle`, and prefer headed runs for formal captures.
4. Sync config into a temp DB when validating config or schema changes.
5. Use dry-run output to verify command construction and selected due jobs before executing.
6. For executed runs, inspect `run_summary.json`, `crawl_attempts`, `crawl_run_reports`, artifact paths, and failure classification.
7. Route job-specific crawl failures to the MediaCrawler or page-capture skill rather than debugging all details in the scheduler.

## Validation

- JSON config: `python3 -m json.tool config/crawl_targets.json >/dev/null`
- Python changes: `.venv/bin/python -m py_compile scripts/crawl_runner.py scripts/failure_classifier.py scripts/db_bootstrap.py`
- Scheduler sync: `crawl_jobs` contains configured active jobs and retired job kinds are disabled or migrated.
- Dry-run: selected commands match the expected `job_kind` and params.

## Common Mistakes

- Editing the SQLite scheduler state directly instead of updating `config/crawl_targets.json` and syncing.
- Treating child crawler failures as scheduler bugs without checking classification and artifact paths.
- Running many due jobs before confirming dry-run output.
- Adding new job kinds without updating schema and bootstrap migration logic.
- Raising schedule frequency or disabling throttling without checking the anti-automation policy.

## References

- `docs/crawl-architecture.md`
- `docs/anti-automation-behavior.md`
- `config/crawl_targets.json`
- `scripts/crawl_runner.py`
- `scripts/failure_classifier.py`
- `db/crawl_scheduler.sql`

## Scripts

- Existing: `scripts/crawl_runner.py`
- Existing: `scripts/failure_classifier.py`
