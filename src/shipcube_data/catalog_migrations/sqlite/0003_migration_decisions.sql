-- 0003: what the console's migration review shows: who drafted a proposal (agent, muse-spark-1.3,
-- rules), the person's decision note and time, and the dry-run diff.
ALTER TABLE migrations ADD COLUMN drafted_by TEXT;
ALTER TABLE migrations ADD COLUMN decision_note TEXT;
ALTER TABLE migrations ADD COLUMN decided_at TEXT;
ALTER TABLE migrations ADD COLUMN diff TEXT;
