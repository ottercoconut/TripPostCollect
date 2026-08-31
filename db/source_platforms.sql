-- TripPostCollect source platform schema.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS source_platforms (
    platform_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL,
    default_url TEXT NOT NULL,
    login_url TEXT,
    requires_login INTEGER NOT NULL DEFAULT 0,
    cookie_domains_json TEXT NOT NULL DEFAULT '[]',
    recommended_scrapling_mode TEXT NOT NULL,
    browser_engine TEXT NOT NULL DEFAULT 'playwright',
    mobile_context INTEGER NOT NULL DEFAULT 0,
    default_wait_ms INTEGER NOT NULL DEFAULT 800,
    min_delay_seconds INTEGER NOT NULL DEFAULT 60,
    max_requests_per_session INTEGER NOT NULL DEFAULT 20,
    daily_request_budget INTEGER NOT NULL DEFAULT 50,
    cooldown_minutes INTEGER NOT NULL DEFAULT 30,
    account_risk_level TEXT NOT NULL DEFAULT 'medium',
    content_focus TEXT,
    browser_profile_dir TEXT,
    storage_state_path TEXT,
    login_state_exists INTEGER NOT NULL DEFAULT 0,
    last_verified_at TEXT,
    crawl_policy_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (status IN ('active', 'inactive')),
    CHECK (requires_login IN (0, 1)),
    CHECK (mobile_context IN (0, 1)),
    CHECK (login_state_exists IN (0, 1)),
    CHECK (recommended_scrapling_mode IN ('static', 'dynamic', 'stealth')),
    CHECK (browser_engine IN ('playwright', 'patchright')),
    CHECK (min_delay_seconds >= 0),
    CHECK (max_requests_per_session >= 0),
    CHECK (daily_request_budget >= 0)
);

CREATE INDEX IF NOT EXISTS idx_source_platforms_status ON source_platforms(status, platform_key);
CREATE INDEX IF NOT EXISTS idx_source_platforms_mode ON source_platforms(recommended_scrapling_mode, platform_key);
