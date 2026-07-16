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
    run_id TEXT NOT NULL UNIQUE,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

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
