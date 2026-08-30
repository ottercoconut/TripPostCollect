CREATE TABLE IF NOT EXISTS xhs_accounts (
    account_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'login_pending',
    profile_dir TEXT NOT NULL UNIQUE,
    encrypted_state_path TEXT NOT NULL UNIQUE,
    identity_hash TEXT UNIQUE,
    last_verified_at TEXT,
    last_used_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (status IN (
        'login_pending', 'active', 'login_required', 'quarantined', 'retired'
    ))
);

CREATE INDEX IF NOT EXISTS idx_xhs_accounts_eligible
ON xhs_accounts(status, account_id);

CREATE TABLE IF NOT EXISTS xhs_account_events (
    id INTEGER PRIMARY KEY,
    account_id TEXT REFERENCES xhs_accounts(account_id) ON DELETE SET NULL,
    run_id TEXT,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_xhs_account_events_account
ON xhs_account_events(account_id, id DESC);

CREATE TABLE IF NOT EXISTS xhs_account_leases (
    account_id TEXT PRIMARY KEY REFERENCES xhs_accounts(account_id) ON DELETE CASCADE,
    lease_id TEXT NOT NULL UNIQUE,
    owner_token TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL UNIQUE,
    lease_kind TEXT NOT NULL,
    owner_host_id TEXT NOT NULL,
    owner_boot_id TEXT NOT NULL,
    owner_pid INTEGER NOT NULL,
    owner_process_started_at TEXT NOT NULL,
    owner_process_start_token TEXT NOT NULL,
    owner_pgid INTEGER NOT NULL,
    execution_state_path TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    lease_duration_seconds INTEGER NOT NULL,
    child_shutdown_budget_seconds INTEGER NOT NULL,
    root_finalize_budget_seconds INTEGER NOT NULL,
    identity_version INTEGER NOT NULL DEFAULT 1,
    CHECK (lease_kind IN ('crawl', 'login', 'repair')),
    CHECK (owner_pid > 0),
    CHECK (owner_pgid > 0),
    CHECK (lease_duration_seconds > 0),
    CHECK (child_shutdown_budget_seconds >= 0),
    CHECK (root_finalize_budget_seconds >= 0),
    CHECK (identity_version = 1)
);

CREATE TABLE IF NOT EXISTS xhs_lease_processes (
    lease_id TEXT NOT NULL REFERENCES xhs_account_leases(lease_id) ON DELETE CASCADE,
    process_role TEXT NOT NULL,
    host_id TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    pid INTEGER NOT NULL,
    process_started_at TEXT NOT NULL,
    process_start_token TEXT NOT NULL,
    pgid INTEGER NOT NULL,
    registered_at TEXT NOT NULL,
    exited_at TEXT,
    PRIMARY KEY (lease_id, process_role, pid, process_start_token),
    CHECK (process_role IN ('child', 'exporter', 'browser')),
    CHECK (pid > 0),
    CHECK (pgid > 0)
);

CREATE INDEX IF NOT EXISTS idx_xhs_lease_processes_live
ON xhs_lease_processes(lease_id, exited_at, process_role);

CREATE TABLE IF NOT EXISTS xhs_runs (
    run_id TEXT PRIMARY KEY,
    target_key TEXT NOT NULL,
    account_id TEXT REFERENCES xhs_accounts(account_id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    execution_state_path TEXT NOT NULL,
    child_summary_path TEXT,
    report_json TEXT NOT NULL DEFAULT '{}',
    CHECK (status IN ('planned', 'running', 'completed', 'failed', 'blocked'))
);

CREATE INDEX IF NOT EXISTS idx_xhs_runs_account
ON xhs_runs(account_id, started_at DESC);

CREATE TABLE IF NOT EXISTS xhs_discovery_checkpoints (
    id INTEGER PRIMARY KEY,
    target_key TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES xhs_accounts(account_id) ON DELETE CASCADE,
    keyword TEXT NOT NULL,
    query_fingerprint TEXT NOT NULL,
    resume_page INTEGER NOT NULL DEFAULT 1,
    resume_search_id TEXT,
    source_has_more INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    last_batch_complete INTEGER NOT NULL DEFAULT 1,
    last_stop_reason TEXT NOT NULL DEFAULT '',
    last_run_id TEXT,
    last_summary_path TEXT,
    campaign_candidate_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(target_key, account_id, query_fingerprint),
    CHECK (resume_page >= 1),
    CHECK (source_has_more IS NULL OR source_has_more IN (0, 1)),
    CHECK (status IN ('active', 'exhausted')),
    CHECK (last_batch_complete IN (0, 1)),
    CHECK (campaign_candidate_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_xhs_discovery_checkpoints_target
ON xhs_discovery_checkpoints(target_key, account_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS xhs_discovery_seen_candidates (
    target_key TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES xhs_accounts(account_id) ON DELETE CASCADE,
    query_fingerprint TEXT NOT NULL,
    platform_post_id TEXT NOT NULL,
    first_run_id TEXT NOT NULL,
    last_run_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (target_key, account_id, query_fingerprint, platform_post_id)
);

CREATE INDEX IF NOT EXISTS idx_xhs_discovery_seen_candidates_account
ON xhs_discovery_seen_candidates(account_id, query_fingerprint, last_seen_at DESC);
