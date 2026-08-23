-- Preserve audit facts while making only verified or attested human events visible.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'human_events' AND column_name = 'trust_status'
    ) THEN
        ALTER TABLE human_events ADD COLUMN trust_status VARCHAR(32) NOT NULL DEFAULT 'legacy_unverified';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'manual_check_events' AND column_name = 'trust_status'
    ) THEN
        ALTER TABLE manual_check_events ADD COLUMN trust_status VARCHAR(32) NOT NULL DEFAULT 'legacy_unverified';
    END IF;
END
$$;

ALTER TABLE human_events DROP CONSTRAINT IF EXISTS chk_human_events_trust_status;
ALTER TABLE human_events ADD CONSTRAINT chk_human_events_trust_status
    CHECK (trust_status IN ('attested', 'legacy_verified', 'legacy_unverified', 'invalid_bypassed'));
ALTER TABLE manual_check_events DROP CONSTRAINT IF EXISTS chk_manual_check_events_trust_status;
ALTER TABLE manual_check_events ADD CONSTRAINT chk_manual_check_events_trust_status
    CHECK (trust_status IN ('attested', 'legacy_verified', 'legacy_unverified', 'invalid_bypassed'));

-- This row was created by a model-forged context without the user's QQ confirmation.
UPDATE manual_check_events SET trust_status = 'invalid_bypassed' WHERE id = 1;

-- Event 2 has a genuine QQ source-message id and an observed user confirmation in qqbot4 history.
UPDATE human_events SET trust_status = 'legacy_verified' WHERE id = 2;

CREATE INDEX IF NOT EXISTS idx_human_events_trust_status
    ON human_events (device_code, trust_status, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_manual_check_events_trust_status
    ON manual_check_events (device_code, trust_status, occurred_at DESC);
