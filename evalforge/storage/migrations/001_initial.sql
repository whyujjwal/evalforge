-- 001_initial.sql
-- The first schema. Run on any empty database. Idempotent via IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    suite_name   TEXT NOT NULL,
    status       TEXT NOT NULL,
    config_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_suite_name ON runs(suite_name);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS task_results (
    run_id        TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    status        TEXT NOT NULL,
    outputs_json  TEXT NOT NULL,
    tokens_input  INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0.0,
    error         TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (run_id, task_id),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scores (
    run_id        TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    rubric_name   TEXT NOT NULL,
    value         REAL NOT NULL,
    rationale     TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, task_id, rubric_name),
    FOREIGN KEY (run_id, task_id) REFERENCES task_results(run_id, task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scores_rubric ON scores(rubric_name);

CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER NOT NULL,
    run_id       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    ts           TEXT NOT NULL,
    task_id      TEXT,
    node_id      TEXT,
    attempt      INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);

INSERT OR IGNORE INTO schema_version (version) VALUES (1);
