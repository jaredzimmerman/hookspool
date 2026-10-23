-- hookspool schema v2 (ARCHITECTURE §3). Applied by `hookspool dispatch` at startup or
-- `hookspool migrate`, both under dispatch.lock, inside one BEGIN IMMEDIATE transaction.
-- All timestamps are Unix seconds (REAL); sizes are bytes.

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    route           TEXT NOT NULL,
    source          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    key             TEXT NOT NULL,
    state           TEXT NOT NULL
                    CHECK (state IN ('open', 'sealed', 'leased', 'retry', 'delivered', 'dead', 'dropped')),
    first_at        REAL NOT NULL,
    last_at         REAL NOT NULL,
    quiet_s         REAL NOT NULL,
    max_s           REAL NOT NULL,
    max_events      INTEGER NOT NULL,
    max_bytes       INTEGER NOT NULL,
    not_before      REAL,
    count           INTEGER NOT NULL DEFAULT 0,
    bytes           INTEGER NOT NULL DEFAULT 0,
    sealed_at       REAL,
    content_hash    TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL,
    lease_until     REAL,
    lease_owner     TEXT,
    last_error      TEXT,
    last_class      TEXT
                    CHECK (last_class IS NULL OR last_class IN
                           ('transport', 'throttle', 'batch', 'config', 'permanent', 'poison', 'quarantine')),
    delivered_at    REAL,
    dropped_at      REAL,
    result          TEXT
);
CREATE INDEX batches_group ON batches (route, key, state);
CREATE INDEX batches_due ON batches (state, next_attempt_at);
CREATE INDEX batches_dest ON batches (destination, state);
CREATE INDEX batches_delivered ON batches (state, delivered_at);

CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    route       TEXT NOT NULL,
    batch_id    INTEGER NOT NULL REFERENCES batches (id),
    event_type  TEXT NOT NULL DEFAULT '',
    dedup_id    TEXT,
    received_at REAL NOT NULL,
    payload     TEXT,
    raw         BLOB,
    headers     TEXT,
    size        INTEGER NOT NULL,
    excluded_at REAL
);
CREATE INDEX events_batch ON events (batch_id, id);
CREATE INDEX events_source ON events (source, received_at);

-- No foreign key on event_id: the dedup window may outlive the event's retention.
CREATE TABLE dedup (
    source    TEXT NOT NULL,
    dedup_id  TEXT NOT NULL,
    event_id  INTEGER NOT NULL,
    seen_at   REAL NOT NULL,
    dup_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, dedup_id)
);
CREATE INDEX dedup_seen ON dedup (source, seen_at);

-- No foreign key on batch_id: attempts are kept longer than delivered batches.
CREATE TABLE attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL,
    n           INTEGER NOT NULL,
    destination TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    outcome     TEXT CHECK (outcome IS NULL OR outcome IN ('ok', 'retry', 'dead', 'discarded')),
    klass       TEXT,
    status      INTEGER,
    error       TEXT,
    duration_ms INTEGER,
    probe       INTEGER NOT NULL DEFAULT 0,
    counted     INTEGER NOT NULL DEFAULT 0,
    delivery_id TEXT
);
CREATE INDEX attempts_batch ON attempts (batch_id, id);
CREATE INDEX attempts_started ON attempts (started_at);

CREATE TABLE destinations (
    name                 TEXT PRIMARY KEY,
    state                TEXT NOT NULL DEFAULT 'closed'
                         CHECK (state IN ('closed', 'open', 'half_open', 'paused')),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    opened_at            REAL,
    cooldown_s           REAL,
    probe_until          REAL,
    last_probe_batch_id  INTEGER,
    paused_at            REAL,
    paused_by            TEXT,
    last_ok_at           REAL,
    last_error           TEXT,
    retry_after_until    REAL
);

CREATE TABLE issues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL CHECK (kind IN ('delivery', 'source_silent', 'spool', 'config')),
    destination     TEXT,
    route           TEXT,
    state           TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'acked', 'resolved', 'ignored')),
    first_at        REAL NOT NULL,
    last_at         REAL NOT NULL,
    count           INTEGER NOT NULL DEFAULT 1,
    sample_batch_id INTEGER,
    last_error      TEXT,
    alerted_at      REAL
);
CREATE INDEX issues_state ON issues (state, last_at);

CREATE TABLE alerts (
    kind       TEXT PRIMARY KEY,
    last_at    REAL NOT NULL,
    suppressed INTEGER NOT NULL DEFAULT 0
);
