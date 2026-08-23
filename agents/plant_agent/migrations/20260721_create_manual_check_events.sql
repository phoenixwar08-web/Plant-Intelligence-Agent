-- P7.2: independent, confirmation-backed audit records for manual checks.
-- Apply only after the Stage 3 roles and credentials have been provisioned.
BEGIN;

CREATE TABLE IF NOT EXISTS manual_check_events (
    id BIGSERIAL PRIMARY KEY,
    device_code VARCHAR(32) NOT NULL CHECK (device_code = 'soil2'),
    check_code VARCHAR(64) NOT NULL,
    check_source VARCHAR(64) NOT NULL,
    check_evidence TEXT NOT NULL,
    check_generated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    result VARCHAR(32) NOT NULL CHECK (result IN ('no_issue', 'issue_found', 'not_completed')),
    note VARCHAR(200),
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    source VARCHAR(32) NOT NULL DEFAULT 'qqbot' CHECK (source = 'qqbot'),
    operator_id VARCHAR(128) NOT NULL,
    operator_name VARCHAR(200),
    source_message_id VARCHAR(256) NOT NULL UNIQUE,
    confirmation_status VARCHAR(32) NOT NULL DEFAULT 'confirmed'
        CHECK (confirmation_status = 'confirmed'),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_manual_check_events_device_code_time
    ON manual_check_events (device_code, check_code, occurred_at DESC);

CREATE TABLE IF NOT EXISTS pending_manual_check_events (
    id BIGSERIAL PRIMARY KEY,
    channel VARCHAR(32) NOT NULL CHECK (channel = 'qqbot'),
    agent_id VARCHAR(64) NOT NULL CHECK (agent_id = 'qqbot4'),
    conversation_id VARCHAR(256) NOT NULL,
    sender_id VARCHAR(128) NOT NULL,
    sender_name VARCHAR(200),
    source_message_id VARCHAR(256) NOT NULL,
    event_payload_json TEXT NOT NULL,
    payload_hash CHAR(64) NOT NULL,
    active_owner_hash CHAR(64),
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    confirmation_message_id VARCHAR(256) UNIQUE,
    manual_check_event_id BIGINT UNIQUE REFERENCES manual_check_events(id),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT uq_pending_manual_check_source_identity
        UNIQUE (channel, conversation_id, sender_id, source_message_id),
    CONSTRAINT chk_pending_manual_check_status
        CHECK (status IN ('pending', 'confirmed', 'cancelled', 'expired')),
    CONSTRAINT chk_pending_manual_check_active_owner
        CHECK ((status = 'pending' AND active_owner_hash IS NOT NULL)
            OR (status <> 'pending' AND active_owner_hash IS NULL)),
    CONSTRAINT chk_pending_manual_check_confirmation
        CHECK ((status = 'confirmed' AND manual_check_event_id IS NOT NULL AND confirmed_at IS NOT NULL)
            OR (status <> 'confirmed'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_manual_check_active_owner
    ON pending_manual_check_events (active_owner_hash)
    WHERE active_owner_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pending_manual_check_lookup
    ON pending_manual_check_events (channel, agent_id, conversation_id, sender_id, status, expires_at);

REVOKE ALL PRIVILEGES ON TABLE manual_check_events, pending_manual_check_events
  FROM plant_agent_reader, plant_human_event_writer, PUBLIC;
REVOKE ALL PRIVILEGES ON SEQUENCE manual_check_events_id_seq, pending_manual_check_events_id_seq
  FROM plant_agent_reader, plant_human_event_writer, PUBLIC;

GRANT SELECT ON TABLE manual_check_events TO plant_agent_reader;
GRANT SELECT, INSERT, UPDATE ON TABLE manual_check_events, pending_manual_check_events
  TO plant_human_event_writer;
GRANT USAGE, SELECT ON SEQUENCE manual_check_events_id_seq, pending_manual_check_events_id_seq
  TO plant_human_event_writer;

COMMIT;

-- Rollback after the P7.2 application path has been disabled:
-- REVOKE ALL PRIVILEGES ON TABLE manual_check_events, pending_manual_check_events
--   FROM plant_agent_reader, plant_human_event_writer;
-- REVOKE ALL PRIVILEGES ON SEQUENCE manual_check_events_id_seq, pending_manual_check_events_id_seq
--   FROM plant_agent_reader, plant_human_event_writer;
-- DROP TABLE pending_manual_check_events;
-- DROP TABLE manual_check_events;
