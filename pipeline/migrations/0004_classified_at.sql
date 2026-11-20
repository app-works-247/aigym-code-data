-- 0004_classified_at.sql: remember when classify last looked at a repo.
-- Nightly classifies only repos never classified (NULL); the weekly job re-classifies the rest.
-- Additive only: a nullable column, no data changes.

ALTER TABLE repos ADD COLUMN classified_at TIMESTAMPTZ;
CREATE INDEX repos_unclassified_idx ON repos (first_seen) WHERE classified_at IS NULL;
