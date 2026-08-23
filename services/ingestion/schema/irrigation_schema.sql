CREATE TABLE IF NOT EXISTS mqtt_raw_events (
    id BIGSERIAL PRIMARY KEY,
    received_at TIMESTAMP NOT NULL,
    topic TEXT NOT NULL,
    payload TEXT NOT NULL,
    device_code TEXT,
    device_id TEXT,
    parsed_ok BOOLEAN DEFAULT FALSE,
    error TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS soil_sensor_readings (
    id BIGSERIAL PRIMARY KEY,
    device_code TEXT NOT NULL,
    device_id TEXT,
    recv_time TIMESTAMP NOT NULL,
    temp NUMERIC,
    humidity NUMERIC,
    ec NUMERIC,
    lux NUMERIC,
    air_humidity NUMERIC,
    watering_flag INTEGER DEFAULT 0,
    watering_sec NUMERIC DEFAULT 0,
    source TEXT DEFAULT 'mqtt',
    topic TEXT,
    raw_payload TEXT,
    operator_id TEXT,
    operator_name TEXT,
    request_id TEXT,
    command_payload TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS irrigation_events (
    id BIGSERIAL PRIMARY KEY,
    device_code TEXT NOT NULL,
    command_time TIMESTAMP NOT NULL,
    pump_topic TEXT,
    water_sec NUMERIC NOT NULL,
    reason TEXT,
    plan_label TEXT,
    source TEXT DEFAULT 'mqtt',
    status TEXT DEFAULT 'issued',
    raw_payload TEXT,
    operator_id TEXT,
    operator_name TEXT,
    request_id TEXT,
    command_payload TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS irrigation_trials (
    id BIGSERIAL PRIMARY KEY,
    device_code TEXT NOT NULL,
    event_id BIGINT,
    status TEXT NOT NULL,
    reason TEXT,
    zone TEXT,
    plan_label TEXT,
    water_sec NUMERIC,
    humidity_before NUMERIC,
    humidity_after NUMERIC,
    delta_m NUMERIC,
    expected_delta_m NUMERIC,
    quality_score NUMERIC,
    penalty NUMERIC,
    fc NUMERIC,
    target_low NUMERIC,
    k_p NUMERIC,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS irrigation_profiles (
    device_code TEXT NOT NULL,
    zone TEXT NOT NULL,
    stable_success INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    max_allowed_sec NUMERIC,
    kp_ema NUMERIC,
    strategy_stats TEXT,
    last_reason TEXT,
    updated_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (device_code, zone)
);

CREATE TABLE IF NOT EXISTS system_states (
    device_code TEXT PRIMARY KEY,
    phase TEXT,
    state TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ingest_health (
    id INTEGER PRIMARY KEY DEFAULT 1,
    last_success_at TIMESTAMP,
    last_error_at TIMESTAMP,
    last_error TEXT,
    raw_written BIGINT DEFAULT 0,
    sensor_written BIGINT DEFAULT 0,
    irrigation_written BIGINT DEFAULT 0,
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS soil1_data (
    id BIGSERIAL PRIMARY KEY,
    recv_time TIMESTAMP NOT NULL,
    device_id TEXT,
    temp NUMERIC,
    humidity NUMERIC,
    ec NUMERIC,
    watering_flag INTEGER DEFAULT 0,
    lux NUMERIC,
    watering_sec NUMERIC DEFAULT 0,
    air_humidity NUMERIC
);

CREATE TABLE IF NOT EXISTS soil2_data (
    id BIGSERIAL PRIMARY KEY,
    recv_time TIMESTAMP NOT NULL,
    device_id TEXT,
    temp NUMERIC,
    humidity NUMERIC,
    ec NUMERIC,
    watering_flag INTEGER DEFAULT 0,
    lux NUMERIC,
    watering_sec NUMERIC DEFAULT 0,
    air_humidity NUMERIC
);

CREATE TABLE IF NOT EXISTS soil3_data (
    id BIGSERIAL PRIMARY KEY,
    recv_time TIMESTAMP NOT NULL,
    device_id TEXT,
    temp NUMERIC,
    humidity NUMERIC,
    ec NUMERIC,
    watering_flag INTEGER DEFAULT 0,
    lux NUMERIC,
    watering_sec NUMERIC DEFAULT 0,
    air_humidity NUMERIC
);

CREATE TABLE IF NOT EXISTS soil_test_data (
    id BIGSERIAL PRIMARY KEY,
    recv_time TIMESTAMP NOT NULL,
    device_id TEXT,
    temp NUMERIC,
    humidity NUMERIC,
    ec NUMERIC,
    watering_flag INTEGER DEFAULT 0,
    lux NUMERIC,
    watering_sec NUMERIC DEFAULT 0,
    air_humidity NUMERIC
);


CREATE INDEX IF NOT EXISTS idx_mqtt_raw_events_received_at ON mqtt_raw_events(received_at);
CREATE INDEX IF NOT EXISTS idx_mqtt_raw_events_topic ON mqtt_raw_events(topic);
CREATE INDEX IF NOT EXISTS idx_sensor_device_time ON soil_sensor_readings(device_code, recv_time DESC);
CREATE INDEX IF NOT EXISTS idx_irrigation_events_device_time ON irrigation_events(device_code, command_time DESC);
CREATE INDEX IF NOT EXISTS idx_irrigation_trials_device_time ON irrigation_trials(device_code, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_soil1_data_time ON soil1_data(recv_time DESC);
CREATE INDEX IF NOT EXISTS idx_soil2_data_time ON soil2_data(recv_time DESC);
CREATE INDEX IF NOT EXISTS idx_soil3_data_time ON soil3_data(recv_time DESC);
CREATE INDEX IF NOT EXISTS idx_soil_test_data_time ON soil_test_data(recv_time DESC);
CREATE TABLE IF NOT EXISTS parameter_change_log (
  id BIGSERIAL PRIMARY KEY,
  change_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  device_code VARCHAR(32) NOT NULL,
  system_name VARCHAR(64) NOT NULL,
  parameter_name VARCHAR(64) NOT NULL,
  old_value DOUBLE PRECISION,
  new_value DOUBLE PRECISION,
  delta_value DOUBLE PRECISION,
  change_reason VARCHAR(128) NOT NULL,
  change_source VARCHAR(64) NOT NULL,
  audit_date DATE,
  evidence TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_parameter_change_log_device_time
  ON parameter_change_log (device_code, change_time DESC);

CREATE INDEX IF NOT EXISTS idx_parameter_change_log_param_time
  ON parameter_change_log (parameter_name, change_time DESC);

CREATE INDEX IF NOT EXISTS idx_irrigation_events_request_id ON irrigation_events(request_id);
