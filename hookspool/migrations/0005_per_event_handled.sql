-- hookspool schema 5 (ARCHITECTURE §6.4.2). Per-event settlement:
-- a batch_handled table records which events of a batch the receiver has fully processed,
-- per batch, so a retry only delivers the events that are not yet handled.
-- Handled state lasts as long as the batch and is cleared when the batch reaches
-- delivered, dead or dropped.

CREATE TABLE batch_handled (
    batch_id  INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    event_id  INTEGER NOT NULL,
    handled_at REAL NOT NULL,
    PRIMARY KEY (batch_id, event_id)
);

CREATE INDEX idx_batch_handled_batch ON batch_handled(batch_id);
