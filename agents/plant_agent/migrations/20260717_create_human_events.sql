CREATE TABLE IF NOT EXISTS human_events (
    id BIGSERIAL PRIMARY KEY,
    device_code VARCHAR(32) NOT NULL CHECK (device_code = 'soil2'),
    event_type VARCHAR(32) NOT NULL CHECK (event_type = 'manual_watering'),
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    duration_sec NUMERIC(10, 3),
    volume_ml NUMERIC(10, 3),
    note VARCHAR(200),
    source VARCHAR(32) NOT NULL DEFAULT 'qqbot',
    operator_id VARCHAR(128) NOT NULL,
    operator_name VARCHAR(200),
    source_message_id VARCHAR(128) NOT NULL UNIQUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (duration_sec IS NULL OR duration_sec > 0),
    CHECK (volume_ml IS NULL OR volume_ml > 0)
);

CREATE INDEX IF NOT EXISTS idx_human_events_device_type_occurred_at
    ON human_events (device_code, event_type, occurred_at DESC);
