---
name: sqlite-import-validation
description: Use only when changing or validating TripPostCollect SQLite schema, bootstrap, import mapping, de-duplication, or field persistence.
---

# SQLite Import Validation

## Trigger

- Changes touch `db/*.sql`, `scripts/db_bootstrap.py`, `scripts/mediacrawler_crawl.py`, or `scripts/import_ctf_captures.py`.
- User asks about table structure, import correctness, de-duplication, `published_at`, author fields, images, or row counts.
- A crawl result must be verified in SQLite.

## Inputs

- `docs/data-persistence.md`
- `db/source_platforms.sql`
- `db/web_posts.sql`
- `db/ctf_captures.sql`
- `db/crawl_scheduler.sql`
- `scripts/db_bootstrap.py`
- Relevant importer script for the source being tested

## Workflow

1. Inspect the schema and importer boundary for the field or table being changed.
2. Use a temp SQLite database for code validation unless the user explicitly asks to update the default DB.
3. Run bootstrap through an existing entrypoint or `crawl_runner.py --sync-only` with the temp DB.
4. Import a small, explicit artifact or run a small crawl into the temp DB.
5. Query row counts, key identifiers, timestamps, author fields, image rows, and source/evidence links.
6. If a persistent field contract changes, update `docs/data-persistence.md` and, when platform capability changes, `docs/platform-field-coverage.md`.

## Validation

- Python changes: `.venv/bin/python -m py_compile scripts/db_bootstrap.py scripts/mediacrawler_crawl.py scripts/import_ctf_captures.py`
- Schema sanity: SQLite opens the temp DB and required tables exist.
- Import sanity: representative `SELECT` queries show expected rows and no unintended video or skipped rows.
- Config/schema sync: `crawl_jobs` contains only current configured job kinds.

## Common Mistakes

- Testing against the default DB when a temp DB would prove the change.
- Backfilling old records without an explicit user request.
- Adding platform-specific columns when the existing normalized fields or JSON payloads are sufficient.
- Updating `web_posts` without rebuilding associated `web_post_images` when importer logic expects replacement.

## References

- `docs/data-persistence.md`
- `db/*.sql`
- `scripts/db_bootstrap.py`
- `scripts/mediacrawler_crawl.py`
- `scripts/import_ctf_captures.py`

## Scripts

- Existing: `scripts/db_bootstrap.py`
- Existing: `scripts/mediacrawler_crawl.py`
- Existing: `scripts/import_ctf_captures.py`
