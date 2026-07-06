CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ctf_captures (
    id INTEGER PRIMARY KEY,
    capture_kind TEXT NOT NULL,
    site_key TEXT NOT NULL REFERENCES source_platforms(platform_key) ON DELETE RESTRICT,
    target_url TEXT NOT NULL,
    final_url TEXT,
    title TEXT,
    published_at TEXT,
    ok INTEGER NOT NULL DEFAULT 0,
    nav_error TEXT NOT NULL DEFAULT '',
    content_ready INTEGER NOT NULL DEFAULT 0,
    ready_reasons_json TEXT NOT NULL DEFAULT '[]',
    ready_state TEXT,
    body_text_length INTEGER NOT NULL DEFAULT 0,
    root_text_length INTEGER NOT NULL DEFAULT 0,
    image_count INTEGER NOT NULL DEFAULT 0,
    loaded_image_count INTEGER NOT NULL DEFAULT 0,
    enriched INTEGER,
    flag_count INTEGER NOT NULL DEFAULT 0,
    primary_flag TEXT,
    flags_json TEXT NOT NULL DEFAULT '[]',
    total_image_requests INTEGER NOT NULL DEFAULT 0,
    successful_image_responses INTEGER NOT NULL DEFAULT 0,
    http_failed_image_responses INTEGER NOT NULL DEFAULT 0,
    request_failed_images INTEGER NOT NULL DEFAULT 0,
    saved_images INTEGER NOT NULL DEFAULT 0,
    captured_at TEXT NOT NULL,
    artifact_dir TEXT NOT NULL,
    capture_meta_path TEXT NOT NULL,
    rendered_html_path TEXT,
    visible_text_path TEXT,
    network_path TEXT,
    storage_path TEXT,
    images_json_path TEXT,
    failed_images_json_path TEXT,
    screenshot_path TEXT,
    navigation_json TEXT NOT NULL DEFAULT '{}',
    enrichment_json TEXT NOT NULL DEFAULT '{}',
    cookie_events_json TEXT NOT NULL DEFAULT '[]',
    policy_events_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL DEFAULT '{}',
    image_summary_json TEXT NOT NULL DEFAULT '{}',
    validation_json TEXT NOT NULL DEFAULT '{}',
    raw_meta_json TEXT NOT NULL DEFAULT '{}',
    source_summary_path TEXT,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (capture_kind IN ('flag', 'resource')),
    CHECK (ok IN (0, 1)),
    CHECK (content_ready IN (0, 1)),
    CHECK (enriched IS NULL OR enriched IN (0, 1)),
    CHECK (flag_count >= 0),
    CHECK (total_image_requests >= 0),
    CHECK (successful_image_responses >= 0),
    CHECK (http_failed_image_responses >= 0),
    CHECK (request_failed_images >= 0),
    CHECK (saved_images >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ctf_captures_artifact_dir
ON ctf_captures(artifact_dir);

CREATE INDEX IF NOT EXISTS idx_ctf_captures_site_time
ON ctf_captures(site_key, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_ctf_captures_kind_ok
ON ctf_captures(capture_kind, ok, captured_at DESC);

CREATE TABLE IF NOT EXISTS ctf_capture_images (
    id INTEGER PRIMARY KEY,
    ctf_capture_id INTEGER NOT NULL REFERENCES ctf_captures(id) ON DELETE CASCADE,
    image_index INTEGER NOT NULL,
    image_url TEXT NOT NULL,
    status INTEGER,
    ok INTEGER NOT NULL DEFAULT 0,
    content_type TEXT,
    resource_type TEXT,
    saved_path TEXT,
    bytes INTEGER,
    error TEXT,
    raw_image_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (ok IN (0, 1)),
    CHECK (image_index >= 0),
    CHECK (bytes IS NULL OR bytes >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ctf_capture_images_unique
ON ctf_capture_images(ctf_capture_id, image_index);

CREATE INDEX IF NOT EXISTS idx_ctf_capture_images_capture
ON ctf_capture_images(ctf_capture_id, image_index);
