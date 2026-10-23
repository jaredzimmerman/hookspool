-- hookspool schema 2 (ARCHITECTURE §3, §6.4). Per-event results of an unwrap delivery
-- (one request per event): how many events the attempt delivered and the event whose request
-- failed. Both NULL for a document delivery. Ids and counts only, never a body.

ALTER TABLE attempts ADD COLUMN events_delivered INTEGER;
ALTER TABLE attempts ADD COLUMN failed_event_id INTEGER;
