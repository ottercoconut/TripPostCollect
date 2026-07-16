CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS crawl_jobs (
    id INTEGER PRIMARY KEY,
    job_key TEXT NOT NULL UNIQUE,
    site_key TEXT NOT NULL,
    target_url TEXT NOT NULL,
    job_kind TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 100,
    schedule_seconds INTEGER NOT NULL DEFAULT 86400,
    next_run_at TEXT NOT NULL,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_attempt_id INTEGER,
    last_status TEXT,
    last_failure_type TEXT,
    params_json TEXT NOT NULL DEFAULT '{}',
    behavior_profile_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (enabled IN (0, 1)),
    CHECK (job_kind IN ('mediacrawler_search', 'ctf_resource_crawl', 'douban_group_search')),
    CHECK (status IN ('pending', 'leased', 'completed', 'retry_wait', 'blocked', 'login_required', 'captcha_detected', 'failed_final', 'disabled')),
    CHECK (schedule_seconds >= 0),
    CHECK (max_attempts >= 1),
    CHECK (consecutive_failures >= 0)
);

CREATE INDEX IF NOT EXISTS idx_crawl_jobs_due
ON crawl_jobs(enabled, status, next_run_at, priority);

CREATE INDEX IF NOT EXISTS idx_crawl_jobs_site
ON crawl_jobs(site_key, job_kind, next_run_at);

CREATE TABLE IF NOT EXISTS crawl_attempts (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    failure_type TEXT,
    retryable INTEGER NOT NULL DEFAULT 0,
    wait_seconds INTEGER NOT NULL DEFAULT 0,
    command_json TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    artifact_dir TEXT,
    capture_meta_paths_json TEXT NOT NULL DEFAULT '[]',
    import_result_json TEXT NOT NULL DEFAULT '{}',
    stdout_tail TEXT NOT NULL DEFAULT '',
    stderr_tail TEXT NOT NULL DEFAULT '',
    classification_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (attempt_no >= 1),
    CHECK (retryable IN (0, 1)),
    CHECK (wait_seconds >= 0)
);

CREATE INDEX IF NOT EXISTS idx_crawl_attempts_job
ON crawl_attempts(job_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_crawl_attempts_run
ON crawl_attempts(run_id, id);

CREATE TABLE IF NOT EXISTS crawl_run_reports (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    jobs_selected INTEGER NOT NULL DEFAULT 0,
    completed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    blocked_count INTEGER NOT NULL DEFAULT 0,
    report_json TEXT NOT NULL DEFAULT '{}',
    report_path TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS profile_health_checks (
    id INTEGER PRIMARY KEY,
    site_key TEXT NOT NULL,
    profile_dir TEXT NOT NULL,
    state_path TEXT,
    status TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    CHECK (status IN ('unknown', 'ok', 'missing', 'login_required', 'captcha_detected', 'expired', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_profile_health_site
ON profile_health_checks(site_key, checked_at DESC);
