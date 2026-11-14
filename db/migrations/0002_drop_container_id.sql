-- jobs.container_id was never written: sandboxes are found by their job and token labels
-- instead (DECISIONS.md, 84). Dropping it only changes the catalog, so the table is not rewritten.
ALTER TABLE jobs DROP COLUMN container_id;
