-- hookspool schema 3 (ARCHITECTURE §3, §14.4). The dispatcher's durable clock for poll sources: one row per
-- poll instance. A tick is enqueued and next_at moved in one transaction, so a crash between the two cannot
-- happen; the tick's dedup id (poll:<instance>:<scheduled next_at>) guards it as well. Times are Unix seconds.

CREATE TABLE polls (
    instance          TEXT PRIMARY KEY,
    next_at           REAL NOT NULL,
    last_tick_at      REAL,
    last_batch_id     INTEGER,
    delivered_at      REAL,
    delivered_tick_at REAL,
    skipped           INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL
);
