-- hookspool schema 6 (ARCHITECTURE §6.4.3). Receipts for work still running:
-- batches gains two states (awaiting_receipt, uncertain) and a receipt_id column;
-- a receipts table holds receipt id, batch id, disposition mapping result and expiry.
-- awaiting_receipt: admitted, waiting for its receipt.
-- uncertain: cannot be settled; opens an issue and waits for the operator; never auto-dropped.

CREATE TABLE batches_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    route           TEXT NOT NULL,
    source          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    key             TEXT NOT NULL,
    state           TEXT NOT NULL
                    CHECK (state IN ('open', 'sealed', 'leased', 'retry', 'delivered', 'dead', 'dropped', 'awaiting_receipt', 'uncertain')),
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
    result          TEXT,
    split_from      INTEGER,
    receipt_id      TEXT
);
INSERT INTO batches_new (id, route, source, destination, key, state, first_at, last_at, quiet_s, max_s, max_events, max_bytes, not_before, count, bytes, sealed_at, content_hash, attempts, next_attempt_at, lease_until, lease_owner, last_error, last_class, delivered_at, dropped_at, result, split_from, receipt_id)
  SELECT id, route, source, destination, key, state, first_at, last_at, quiet_s, max_s, max_events, max_bytes, not_before, count, bytes, sealed_at, content_hash, attempts, next_attempt_at, lease_until, lease_owner, last_error, last_class, delivered_at, dropped_at, result, split_from, NULL FROM batches;
DROP TABLE batches;
ALTER TABLE batches_new RENAME TO batches;
CREATE INDEX batches_group ON batches (route, key, state);
CREATE INDEX batches_due ON batches (state, next_attempt_at);
CREATE INDEX batches_dest ON batches (destination, state);
CREATE INDEX batches_delivered ON batches (state, delivered_at);
CREATE INDEX batches_receipt ON batches (receipt_id);

CREATE TABLE receipts (
    receipt_id   TEXT PRIMARY KEY,
    batch_id     INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    disposition  TEXT,
    result       TEXT,
    expiry       REAL,
    settled_at   REAL,
    created_at   REAL NOT NULL
);
CREATE INDEX idx_receipts_batch ON receipts(batch_id);
CREATE INDEX idx_receipts_expiry ON receipts(expiry);