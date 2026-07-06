---
name: mediacrawler-structured-crawl
description: Use only for running, debugging, or reviewing TripPostCollect MediaCrawler structured crawls for xhs, weibo, douyin, or zhihu, including low-frequency headed operation and login/profile stability.
---

# MediaCrawler Structured Crawl

## Trigger

- User asks to crawl or verify xhs, weibo, douyin, or zhihu structured results.
- Changes touch `scripts/mediacrawler_crawl.py`, `scripts/mediacrawler_login_warmup.py`, or a `mediacrawler_search` job in `config/crawl_targets.json`.
- A MediaCrawler run summary, JSONL import issue, login snapshot issue, or platform field coverage issue needs review.

## Inputs

- `docs/crawl-architecture.md`
- `docs/anti-automation-behavior.md` for headed/headless, profile, low-frequency, cooldown, or anti-detection decisions
- `docs/data-persistence.md`
- `docs/platform-field-coverage.md`
- `config/crawl_targets.json`
- `scripts/mediacrawler_crawl.py`
- `scripts/mediacrawler_login_warmup.py` for login/profile issues

## Workflow

1. Identify whether the platform is configured for `mediacrawler_search`; if scheduled, inspect its job in `config/crawl_targets.json`.
2. For zhihu login failures, refresh login state with the documented warmup command before re-running crawl; confirm the summary reports a valid cookie snapshot.
3. For xhs, weibo, douyin, and zhihu, check `docs/anti-automation-behavior.md` before changing frequency, login/profile handling, headless mode, image download, or retry behavior.
4. Run the smallest safe crawl that answers the request. Use `--no-import` for capture-only checks and a temp DB for import validation.
5. Inspect the run `summary.json` and `summary.md`: status, `non_video_content_records`, `published_at_records`, skipped video count, image count, parse errors, and samples.
6. If importing, inspect `import_result` and query the target SQLite DB for row counts and representative fields.
7. Update `docs/platform-field-coverage.md` when a field moves between unavailable, evidence-only, conditional, and structured.

## Validation

- Python changes: `.venv/bin/python -m py_compile scripts/mediacrawler_crawl.py scripts/mediacrawler_login_warmup.py`
- JSON config changes: `python3 -m json.tool config/crawl_targets.json >/dev/null`
- Successful run evidence: a `summary.json` under `outputs/mediacrawler_runs/` with `failed_count` classified and samples present when content is captured.
- Import evidence: SQLite query against `web_posts` and `web_post_images` for the target platform.

## Common Mistakes

- Reading full JSONL or logs instead of using summary counts, samples, and targeted queries.
- Passing raw cookies on the command line; the wrapper uses environment variables and local snapshots.
- Treating missing zhihu `d_c0/z_c0` as a crawler failure instead of refreshing login state.
- Turning on broad media download flags instead of using the project wrapper behavior.
- Updating field coverage without a run artifact or DB query.
- Retrying high-risk platforms rapidly after login, captcha, or verification failures instead of using cooldown and warmup.

## References

- `docs/crawl-architecture.md`
- `docs/anti-automation-behavior.md`
- `docs/data-persistence.md`
- `docs/platform-field-coverage.md`
- `scripts/mediacrawler_crawl.py`
- `scripts/mediacrawler_login_warmup.py`

## Scripts

- Existing: `scripts/mediacrawler_crawl.py`
- Existing: `scripts/mediacrawler_login_warmup.py`
