-- TripPostCollect content persistence schema.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS web_posts (
    id INTEGER PRIMARY KEY,
    platform_key TEXT NOT NULL REFERENCES source_platforms(platform_key) ON DELETE RESTRICT,
    source_capture_id INTEGER REFERENCES ctf_captures(id) ON DELETE SET NULL,
    platform_post_id TEXT,
    source_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    canonical_url TEXT,
    title TEXT,
    author_display_name TEXT,
    author_platform_id TEXT,
    author_profile_url TEXT,
    author_description TEXT,
    author_followers_count INTEGER,
    author_following_count INTEGER,
    author_posts_count INTEGER,
    author_platform_level TEXT,
    author_verified INTEGER,
    author_verified_text TEXT,
    published_at TEXT,
    captured_at TEXT NOT NULL,
    keyword TEXT,
    topic_relevant INTEGER NOT NULL DEFAULT 0,
    content_text TEXT,
    content_length INTEGER NOT NULL DEFAULT 0,
    post_likes_count INTEGER,
    post_favorites_count INTEGER,
    post_comments_count INTEGER,
    post_shares_count INTEGER,
    post_reposts_count INTEGER,
    post_views_count INTEGER,
    post_images_count INTEGER NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    author_json TEXT NOT NULL DEFAULT '{}',
    raw_sample_json TEXT NOT NULL DEFAULT '{}',
    artifact_dir TEXT,
    capture_method TEXT NOT NULL DEFAULT 'scrapling',
    status TEXT NOT NULL DEFAULT 'captured',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (capture_method IN ('scrapling', 'manual', 'import')),
    CHECK (topic_relevant IN (0, 1)),
    CHECK (status IN ('captured', 'partial', 'failed', 'skipped'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_web_posts_platform_post_id
ON web_posts(platform_key, platform_post_id)
WHERE platform_post_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_web_posts_platform_url
ON web_posts(platform_key, canonical_url)
WHERE canonical_url IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_web_posts_platform_captured
ON web_posts(platform_key, captured_at DESC);

CREATE TABLE IF NOT EXISTS post_detail_repair_waivers (
    id INTEGER PRIMARY KEY,
    web_post_id INTEGER NOT NULL UNIQUE REFERENCES web_posts(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    authorized_by TEXT NOT NULL,
    authorized_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_post_detail_repair_waivers_reason
ON post_detail_repair_waivers(reason, authorized_at);

CREATE TABLE IF NOT EXISTS web_post_images (
    id INTEGER PRIMARY KEY,
    web_post_id INTEGER NOT NULL REFERENCES web_posts(id) ON DELETE CASCADE,
    image_index INTEGER NOT NULL,
    image_url TEXT NOT NULL,
    image_role TEXT NOT NULL DEFAULT 'content',
    local_path TEXT,
    width INTEGER,
    height INTEGER,
    mime_type TEXT,
    sha256 TEXT,
    raw_image_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (image_role IN ('content', 'page'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_web_post_images_unique
ON web_post_images(web_post_id, image_role, image_index);

CREATE INDEX IF NOT EXISTS idx_web_post_images_post
ON web_post_images(web_post_id, image_index);
