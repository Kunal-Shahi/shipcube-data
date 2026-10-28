-- 0002: record the person behind the proposer and the approver, so a token and its owner's
-- console session count as the same human (red-team issue #6).
ALTER TABLE migrations ADD COLUMN proposed_by_user TEXT;
ALTER TABLE migrations ADD COLUMN approved_by_user TEXT;
