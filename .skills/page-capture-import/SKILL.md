---
name: page-capture-import
description: Use only for TripPostCollect page-level evidence capture, anti-automation behavior, browser resilience, and import flows for non-MediaCrawler targets such as bilibili, ctrip, qunar, qyer, or douban_group.
---

# Page Capture Import

## Trigger

- User asks to crawl, verify, import, or debug page-level evidence captures.
- Changes touch `scripts/ctf_resource_crawl.py`, `scripts/import_ctf_captures.py`, `scripts/web_sites.py`, or `ctf_resource_crawl` jobs.
- A `capture_meta.json`, screenshot, HTML, visible text, or image-resource issue needs analysis.

## Inputs

- `docs/crawl-architecture.md`
- `docs/anti-automation-behavior.md`
- `docs/data-persistence.md`
- `config/crawl_targets.json`
- `scripts/ctf_resource_crawl.py`
- `scripts/import_ctf_captures.py`
- `scripts/web_sites.py`

## Workflow

1. Confirm the site is configured for `ctf_resource_crawl` and identify its site policy in `scripts/web_sites.py`.
2. Read `docs/anti-automation-behavior.md` before changing browser context, profile directories, throttling, behavior profile, Scrapling preflight, cooldown, or headed/headless behavior.
3. Run a focused capture with small image and scroll limits unless the user requests broader evidence.
4. Inspect `capture_meta.json`, `summary.json`, `visible_text.txt` excerpts, image counts, `policy_events`, `behavior_events`, browser engine, and failure classification.
5. Import explicit `capture_meta.json` files when needed with `scripts/import_ctf_captures.py`; use a temp DB when validating code changes.
6. Verify both layers: evidence rows in `ctf_captures` and user-facing rows in `web_posts`.
7. When publication time extraction changes, verify `published_at` came from page metadata or visible date text, not capture time.

## Validation

- Python changes: `.venv/bin/python -m py_compile scripts/ctf_resource_crawl.py scripts/import_ctf_captures.py`
- Capture evidence: `summary.json` plus target `capture_meta.json` under `outputs/ctf_resource_crawls/`.
- Import evidence: SQL query showing rows in `ctf_captures` and corresponding `web_posts.source_capture_id`.
- Field changes: update `docs/platform-field-coverage.md` only with artifact-backed evidence.

## Common Mistakes

- Treating page-level evidence as complete structured fields without importer support.
- Importing skipped captures as user-facing posts.
- Reading complete rendered HTML when visible text, metadata, and summaries are enough.
- Depending on `temp/` artifacts as if they were stable project state.
- Using `--no-throttle` or headless mode for formal evidence without a task-specific reason.

## References

- `docs/crawl-architecture.md`
- `docs/anti-automation-behavior.md`
- `docs/data-persistence.md`
- `scripts/ctf_resource_crawl.py`
- `scripts/import_ctf_captures.py`
- `scripts/web_sites.py`

## Scripts

- Existing: `scripts/ctf_resource_crawl.py`
- Existing: `scripts/import_ctf_captures.py`
