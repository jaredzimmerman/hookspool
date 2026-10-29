-- hookspool schema 4 (ARCHITECTURE §3, §17). A batch cut by `split` records the batch it was cut
-- from in `batches.split_from`, nullable and without a foreign key: retention prunes the dropped
-- parent first and the children stay. Ids and counts only, never a body.

ALTER TABLE batches ADD COLUMN split_from INTEGER;
