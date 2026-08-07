from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from trippostcollect.artifacts.local_image_backfill import apply_backfill_plan, build_backfill_plan
from trippostcollect.db.bootstrap import bootstrap_connection


def make_xhs_artifact(
    root: Path,
    *,
    note_id: str,
    urls: list[str],
    files: list[bytes],
) -> Path:
    data_root = root / "run" / "child" / "xhs" / "data" / "xhs"
    image_dir = data_root / "images" / note_id
    image_dir.mkdir(parents=True)
    for index, content in enumerate(files):
        (image_dir / f"{index}.jpg").write_bytes(content)
    jsonl_dir = data_root / "jsonl"
    jsonl_dir.mkdir()
    (jsonl_dir / "search_contents.jsonl").write_text(
        json.dumps({"note_id": note_id, "image_list": ",".join(urls)}) + "\n",
        encoding="utf-8",
    )
    return image_dir


def insert_post(conn: sqlite3.Connection, *, note_id: str, urls: list[str]) -> tuple[int, list[int]]:
    raw = json.dumps({"note_id": note_id, "image_list": ",".join(urls)})
    conn.execute(
        """
        INSERT INTO web_posts (
          platform_key, platform_post_id, source_type, source_url, canonical_url, content_text,
          post_images_count, raw_sample_json, artifact_dir, captured_at
        ) VALUES ('xhs', ?, 'mediacrawler_search', ?, ?, 'text', ?, ?, 'artifact', '2026-08-07T00:00:00+00:00')
        """,
        (
            note_id,
            f"https://www.xiaohongshu.com/explore/{note_id}",
            f"https://www.xiaohongshu.com/explore/{note_id}",
            len(urls),
            raw,
        ),
    )
    post_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    image_rows = [
        ("https://sns-avatar-qc.xhscdn.com/avatar/example", "author_avatar"),
        *[(url, "content") for url in urls],
        ("https://sns-avatar-qc.xhscdn.com/avatar/example?imageView2/2/w/540/", "content"),
    ]
    image_ids = []
    for index, (url, role) in enumerate(image_rows):
        conn.execute(
            """
            INSERT INTO web_post_images (
              web_post_id, image_index, image_url, image_role, raw_image_json
            ) VALUES (?, ?, ?, ?, '{}')
            """,
            (post_id, index, url, role),
        )
        image_ids.append(int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]))
    return post_id, image_ids


def test_backfill_maps_only_verified_raw_content_images(tmp_path: Path) -> None:
    note_id = "6871d8dd000000002400b21d"
    urls = [
        "http://sns-webpic-qc.xhscdn.com/a/notes_pre_post/image-a!nd_dft_wlteh_webp_3",
        "http://sns-webpic-qc.xhscdn.com/b/notes_pre_post/image-b!nd_dft_wlteh_webp_3",
    ]
    contents = [b"\xff\xd8\xfffirst", b"\xff\xd8\xffsecond"]
    make_xhs_artifact(tmp_path, note_id=note_id, urls=urls, files=contents)

    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, sync_jobs=False)
        _post_id, image_ids = insert_post(conn, note_id=note_id, urls=urls)
        conn.commit()
        plan = build_backfill_plan(conn, tmp_path, project_root=tmp_path)

        assert [item.image_row_id for item in plan.mappings] == image_ids[1:3]
        assert [item.source_image_index for item in plan.mappings] == [0, 1]
        assert plan.reason_counts == {}

        result = apply_backfill_plan(conn, plan)
        repeated = apply_backfill_plan(conn, plan)
        rows = conn.execute(
            "SELECT id, local_path, sha256, raw_image_json FROM web_post_images ORDER BY id"
        ).fetchall()

    assert result == {"updated": 2, "unchanged": 0, "conflicts": 0}
    assert repeated == {"updated": 0, "unchanged": 2, "conflicts": 0}
    assert rows[0][1] is None
    assert rows[1][1].endswith(f"{note_id}/0.jpg")
    assert rows[2][1].endswith(f"{note_id}/1.jpg")
    assert rows[3][1] is None
    assert rows[1][2] == hashlib.sha256(contents[0]).hexdigest()
    assert json.loads(rows[1][3])["local_file"]["source_image_index"] == 0


def test_backfill_rejects_partial_download_set(tmp_path: Path) -> None:
    note_id = "partial"
    urls = [
        "https://sns-webpic-qc.xhscdn.com/a/notes_pre_post/image-a",
        "https://sns-webpic-qc.xhscdn.com/b/notes_pre_post/image-b",
    ]
    make_xhs_artifact(tmp_path, note_id=note_id, urls=urls, files=[b"\xff\xd8\xffonly-one"])

    with sqlite3.connect(":memory:") as conn:
        bootstrap_connection(conn, sync_jobs=False)
        insert_post(conn, note_id=note_id, urls=urls)
        plan = build_backfill_plan(conn, tmp_path, project_root=tmp_path)

    assert plan.mappings == ()
    assert plan.reason_counts == {"source_urls_or_count_mismatch": 1}
