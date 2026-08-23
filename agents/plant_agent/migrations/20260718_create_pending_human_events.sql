BEGIN;

ALTER TABLE human_events
    ADD COLUMN confirmation_status VARCHAR(32) NOT NULL DEFAULT 'confirmed';

UPDATE human_events
SET confirmation_status = 'legacy_unconfirmed'
WHERE source = 'legacy_unconfirmed';

ALTER TABLE human_events
    ADD CONSTRAINT chk_human_events_confirmation_status
    CHECK (confirmation_status IN ('confirmed', 'legacy_unconfirmed'));

CREATE TABLE IF NOT EXISTS pending_human_events (
    id BIGSERIAL PRIMARY KEY,
    channel VARCHAR(32) NOT NULL CHECK (channel = 'qqbot'),
    agent_id VARCHAR(64) NOT NULL CHECK (agent_id = 'qqbot4'),
    conversation_id VARCHAR(256) NOT NULL,
    sender_id VARCHAR(128) NOT NULL,
    sender_name VARCHAR(200),
    source_message_id VARCHAR(128) NOT NULL,
    event_payload_json TEXT NOT NULL,
    payload_hash CHAR(64) NOT NULL,
    active_owner_hash CHAR(64),
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    confirmation_message_id VARCHAR(128) UNIQUE,
    human_event_id BIGINT UNIQUE REFERENCES human_events(id),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT uq_pending_human_source_identity
        UNIQUE (channel, conversation_id, sender_id, source_message_id),
    CONSTRAINT chk_pending_human_status
        CHECK (status IN ('pending', 'confirmed', 'cancelled', 'expired')),
    CONSTRAINT chk_pending_human_active_owner
        CHECK ((status = 'pending' AND active_owner_hash IS NOT NULL)
            OR (status <> 'pending' AND active_owner_hash IS NULL)),
    CONSTRAINT chk_pending_human_confirmation
        CHECK ((status = 'confirmed' AND human_event_id IS NOT NULL AND confirmed_at IS NOT NULL)
            OR (status <> 'confirmed'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_human_active_owner
    ON pending_human_events (active_owner_hash)
    WHERE active_owner_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pending_human_lookup
    ON pending_human_events (channel, agent_id, conversation_id, sender_id, status, expires_at);

COMMIT;
