from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from trippostcollect.artifacts.image_proxy import RemoteImagePreview
from trippostcollect.db.bootstrap import bootstrap_database


class ReadonlyAdminApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path("temp/admin_client_verify_artifacts")
        cls.db_path = Path("temp/admin_client_verify.sqlite")
        shutil.rmtree(cls.root, ignore_errors=True)
        if cls.db_path.exists():
            cls.db_path.unlink()
        cls.root.mkdir(parents=True, exist_ok=True)
        cls.image_path = cls.root / "image.png"
        cls.screenshot_path = cls.root / "screen.png"
        cls.visible_text_path = cls.root / "visible_text.txt"
        cls.rendered_html_path = cls.root / "rendered.html"
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
        )
        cls.image_path.write_bytes(png)
        cls.screenshot_path.write_bytes(png)
        cls.visible_text_path.write_text("visible text", encoding="utf-8")
        cls.rendered_html_path.write_text("<html><body>rendered</body></html>", encoding="utf-8")
        cls.symlink_path = cls.root / "escape.png"
        try:
            cls.symlink_path.symlink_to("/etc/hosts")
            cls.has_symlink = True
        except OSError:
            cls.has_symlink = False

        bootstrap_database(cls.db_path)
        cls._insert_samples()
        os.environ["TRIPPOST_ADMIN_DB"] = str(cls.db_path)
        os.environ["TRIPPOST_ADMIN_CONFIG"] = "config/crawl_targets.json"

        from apps.admin_api.app.deps import reset_settings_cache
        from apps.admin_api.app.main import app

        reset_settings_cache()
        cls.app = app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.db_path.exists():
            cls.db_path.unlink()
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def _insert_samples(cls) -> None:
        with sqlite3.connect(cls.db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            cls.capture_id = conn.execute(
                """
                INSERT INTO ctf_captures (
                    capture_kind, site_key, target_url, ok, content_ready,
                    body_text_length, root_text_length, image_count, loaded_image_count,
                    flag_count, total_image_requests, successful_image_responses,
                    http_failed_image_responses, request_failed_images, saved_images,
                    captured_at, artifact_dir, capture_meta_path, rendered_html_path,
                    visible_text_path, screenshot_path, raw_meta_json
                ) VALUES (
                    'resource', 'bilibili', 'https://example.test/opus/1', 1, 1,
                    120, 100, 1, 1,
                    0, 1, 1,
                    0, 0, 1,
                    '2026-07-09T08:00:00+08:00', ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    str(cls.root / "capture"),
                    str(cls.root / "capture_meta.json"),
                    str(cls.rendered_html_path),
                    str(cls.visible_text_path),
                    str(cls.screenshot_path),
                    json.dumps({"capture": True}, ensure_ascii=False),
                ),
            ).lastrowid
            cls.bad_capture_id = conn.execute(
                """
                INSERT INTO ctf_captures (
                    capture_kind, site_key, target_url, ok, content_ready,
                    body_text_length, root_text_length, image_count, loaded_image_count,
                    flag_count, total_image_requests, successful_image_responses,
                    http_failed_image_responses, request_failed_images, saved_images,
                    captured_at, artifact_dir, capture_meta_path, screenshot_path, raw_meta_json
                ) VALUES (
                    'resource', 'bilibili', 'https://example.test/bad', 1, 1,
                    120, 100, 1, 1,
                    0, 1, 1,
                    0, 0, 1,
                    '2026-07-09T08:00:00+08:00', ?, ?, '/etc/hosts', '{}'
                )
                """,
                (str(cls.root / "bad_capture"), str(cls.root / "bad_capture_meta.json")),
            ).lastrowid
            cls.post_id = conn.execute(
                """
                INSERT INTO web_posts (
                    platform_key, source_capture_id, platform_post_id, source_type,
                    source_url, canonical_url, title, author_display_name,
                    author_platform_id, author_followers_count, published_at,
                    captured_at, keyword, content_text, content_length,
                    post_likes_count, post_comments_count, post_images_count,
                    metrics_json, author_json, raw_sample_json, artifact_dir,
                    capture_method, status
                ) VALUES (
                    'bilibili', ?, 'opus-1', 'ctf_capture',
                    'https://example.test/opus/1', 'https://example.test/opus/1',
                    '青岛记录', '作者A', 'author-a', 42,
                    '2026-07-08T12:00:00+08:00', '2026-07-09T08:00:00+08:00',
                    '青岛旅游', '正文内容', 4,
                    10, 2, 4, ?, ?, ?, ?,
                    'import', 'captured'
                )
                """,
                (
                    cls.capture_id,
                    json.dumps({"likes": 10}, ensure_ascii=False),
                    json.dumps({"verified": False}, ensure_ascii=False),
                    json.dumps({"source": "test"}, ensure_ascii=False),
                    str(cls.root / "capture"),
                ),
            ).lastrowid
            cls.local_image_id = conn.execute(
                """
                INSERT INTO web_post_images(web_post_id, image_index, image_url, image_role, local_path, mime_type, raw_image_json)
                VALUES (?, 0, 'https://example.test/local.png', 'content', ?, 'image/png', '{}')
                """,
                (cls.post_id, str(cls.image_path)),
            ).lastrowid
            cls.remote_image_id = conn.execute(
                """
                INSERT INTO web_post_images(web_post_id, image_index, image_url, image_role, raw_image_json)
                VALUES (?, 1, 'https://example.test/remote.jpg', 'content', '{}')
                """,
                (cls.post_id,),
            ).lastrowid
            cls.unsafe_remote_image_id = conn.execute(
                """
                INSERT INTO web_post_images(web_post_id, image_index, image_url, image_role, raw_image_json)
                VALUES (?, 2, 'http://127.0.0.1/private.jpg', 'content', '{}')
                """,
                (cls.post_id,),
            ).lastrowid
            cls.outside_image_id = conn.execute(
                """
                INSERT INTO web_post_images(web_post_id, image_index, image_url, image_role, local_path, raw_image_json)
                VALUES (?, 3, 'https://example.test/outside.jpg', 'content', '/etc/hosts', '{}')
                """,
                (cls.post_id,),
            ).lastrowid
            cls.symlink_image_id = None
            if cls.has_symlink:
                cls.symlink_image_id = conn.execute(
                    """
                    INSERT INTO web_post_images(web_post_id, image_index, image_url, image_role, local_path, raw_image_json)
                    VALUES (?, 4, 'https://example.test/symlink.jpg', 'content', ?, '{}')
                    """,
                    (cls.post_id, str(cls.symlink_path)),
                ).lastrowid
            cls.capture_image_id = conn.execute(
                """
                INSERT INTO ctf_capture_images(ctf_capture_id, image_index, image_url, ok, content_type, saved_path, raw_image_json)
                VALUES (?, 0, 'https://example.test/capture.png', 1, 'image/png', ?, '{}')
                """,
                (cls.capture_id, str(cls.image_path)),
            ).lastrowid
            conn.commit()

    def test_records_list_detail_context_raw_and_realtime_reads(self) -> None:
        records = self.client.get("/api/records", params={"platform_key": "bilibili"})
        self.assertEqual(records.status_code, 200, records.text)
        self.assertEqual(records.json()["meta"]["total"], 1)
        self.assertEqual(records.json()["data"][0]["author_followers_count"], 42)

        detail = self.client.get(f"/api/records/{self.post_id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["data"]["author"]["followers_count"], 42)

        context = self.client.get(f"/api/records/{self.post_id}/context")
        self.assertEqual(context.status_code, 200, context.text)
        self.assertEqual(len(context.json()["data"]["images"]), 5 if self.has_symlink else 4)
        self.assertEqual(context.json()["data"]["capture"]["id"], self.capture_id)

        raw = self.client.get(f"/api/records/{self.post_id}/raw")
        self.assertEqual(raw.status_code, 200, raw.text)
        self.assertEqual(raw.json()["data"]["record"]["metrics_json"]["likes"], 10)
        self.assertTrue(raw.json()["data"]["capture"]["raw_meta_json"]["capture"])

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                """
                INSERT INTO web_posts (
                    platform_key, platform_post_id, source_type, source_url, title,
                    captured_at, keyword, content_text, content_length,
                    raw_sample_json, metrics_json, author_json, capture_method, status
                ) VALUES (
                    'weibo', 'wb-live', 'mediacrawler', 'https://example.test/wb-live', '新增记录',
                    '2026-07-09T08:01:00+08:00', '青岛旅游', '新正文', 3,
                    '{}', '{}', '{}', 'import', 'captured'
                )
                """
            )
            conn.commit()
        latest = self.client.get("/api/records")
        self.assertEqual(latest.json()["meta"]["total"], 2)

    def test_images_and_capture_artifacts_enforce_readonly_path_boundaries(self) -> None:
        self.assertEqual(self.client.get(f"/api/records/{self.post_id}/images").status_code, 200)
        self.assertEqual(self.client.get(f"/api/images/{self.local_image_id}/preview").status_code, 200)
        with mock.patch("apps.admin_api.app.routers.images.fetch_remote_image_preview") as fetch_preview:
            fetch_preview.return_value = RemoteImagePreview(
                content=b"remote-image",
                media_type="image/jpeg",
                final_url="https://example.test/remote.jpg",
            )
            remote = self.client.get(f"/api/images/{self.remote_image_id}/preview")
            self.assertEqual(remote.status_code, 200, remote.text)
            self.assertEqual(remote.headers["content-type"], "image/jpeg")
            self.assertEqual(remote.content, b"remote-image")
            fetch_preview.assert_called_once_with("https://example.test/remote.jpg", platform_key="bilibili")
        self.assertEqual(self.client.get(f"/api/images/{self.unsafe_remote_image_id}/preview").status_code, 403)
        self.assertEqual(self.client.get(f"/api/images/{self.outside_image_id}/preview").status_code, 403)
        if self.symlink_image_id is not None:
            self.assertEqual(self.client.get(f"/api/images/{self.symlink_image_id}/preview").status_code, 403)

        self.assertEqual(self.client.get("/api/captures").status_code, 200)
        self.assertEqual(self.client.get(f"/api/captures/{self.capture_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/captures/{self.capture_id}/images").status_code, 200)
        self.assertEqual(self.client.get(f"/api/captures/images/{self.capture_image_id}/preview").status_code, 200)
        for kind in ["visible_text", "rendered_html", "screenshot"]:
            response = self.client.get(f"/api/captures/{self.capture_id}/artifact", params={"kind": kind})
            self.assertEqual(response.status_code, 200, response.text)
        outside = self.client.get(f"/api/captures/{self.bad_capture_id}/artifact", params={"kind": "screenshot"})
        self.assertEqual(outside.status_code, 403)

    def test_overview_platforms_scheduler_and_schema(self) -> None:
        for path in [
            "/api/health",
            "/api/meta",
            "/api/maintenance/schema-status",
            "/api/overview/counts",
            "/api/overview/field-gaps",
            "/api/overview/recent-runs",
            "/api/platforms",
            "/api/scheduler/config",
            "/api/scheduler/jobs",
            "/api/scheduler/reports",
        ]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, (path, response.text))
        configured_job_count = len(
            json.loads(Path("config/crawl_targets.json").read_text(encoding="utf-8"))["jobs"]
        )
        platform_keys = {
            item["platform_key"] for item in self.client.get("/api/platforms").json()["data"]
        }
        self.assertEqual(platform_keys, {"bilibili", "douyin", "weibo", "xhs", "zhihu"})
        self.assertEqual(len(self.client.get("/api/scheduler/config").json()["data"]["jobs"]), configured_job_count)
        self.assertEqual(len(self.client.get("/api/scheduler/jobs").json()["data"]), configured_job_count)

    def test_no_write_or_command_routes_and_no_command_execution(self) -> None:
        paths = self.app.openapi()["paths"]
        write_routes = [
            (path, method)
            for path, operations in paths.items()
            for method in operations
            if method.upper() in {"POST", "PATCH", "DELETE"}
        ]
        self.assertEqual(write_routes, [])
        serialized_paths = json.dumps(paths, ensure_ascii=False).lower()
        self.assertNotIn("city_name", serialized_paths)
        for forbidden in ["sync-only", "dry-run", "bootstrap", "crawl_runner.py"]:
            self.assertNotIn(forbidden, serialized_paths)

        with mock.patch("trippostcollect.db.bootstrap.bootstrap_database") as bootstrap_mock:
            with mock.patch.object(subprocess, "run") as subprocess_mock:
                self.client.get("/api/scheduler/config")
                self.client.get("/api/scheduler/jobs")
                self.client.get("/api/maintenance/schema-status")
        bootstrap_mock.assert_not_called()
        subprocess_mock.assert_not_called()

        for method_name in ["post", "patch", "delete"]:
            response = getattr(self.client, method_name)(f"/api/records/{self.post_id}")
            self.assertIn(response.status_code, {404, 405})


if __name__ == "__main__":
    unittest.main()
