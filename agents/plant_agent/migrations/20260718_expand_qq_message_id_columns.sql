-- QQ official message IDs currently reach at least 137 characters.
-- Keep source-message idempotency while allowing a bounded 256-character ID.
ALTER TABLE human_events
    ALTER COLUMN source_message_id TYPE VARCHAR(256);

ALTER TABLE pending_human_events
    ALTER COLUMN source_message_id TYPE VARCHAR(256),
    ALTER COLUMN confirmation_message_id TYPE VARCHAR(256);
