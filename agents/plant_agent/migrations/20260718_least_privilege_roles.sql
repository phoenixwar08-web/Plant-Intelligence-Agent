-- Stage 3: provision only fixed plant-Agent roles. Passwords are supplied by
-- the root-only deployment procedure, never by this migration or application.

-- openGauss requires a password when creating a LOGIN role. The root-only
-- provisioner creates the two roles with generated passwords immediately
-- before this migration, so the secret never appears in versioned SQL.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plant_agent_reader')
     OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plant_human_event_writer') THEN
    RAISE EXCEPTION 'Phase 3 roles must be created by the root-only provisioner first';
  END IF;
END
$$;

ALTER ROLE plant_agent_reader NOSYSADMIN NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE plant_human_event_writer NOSYSADMIN NOCREATEDB NOCREATEROLE NOINHERIT;

REVOKE ALL PRIVILEGES ON DATABASE soil_data FROM plant_agent_reader, plant_human_event_writer;
GRANT CONNECT ON DATABASE soil_data TO plant_agent_reader, plant_human_event_writer;

REVOKE ALL PRIVILEGES ON SCHEMA public FROM plant_agent_reader, plant_human_event_writer;
GRANT USAGE ON SCHEMA public TO plant_agent_reader, plant_human_event_writer;
-- This is already absent in the audited environment. Keep the invariant
-- explicit so the writer cannot gain CREATE through PUBLIC in a future reset.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

REVOKE ALL PRIVILEGES ON TABLE soil_sensor_readings, irrigation_events, human_events, pending_human_events
  FROM plant_agent_reader, plant_human_event_writer, PUBLIC;
REVOKE ALL PRIVILEGES ON SEQUENCE pending_human_events_id_seq, human_events_id_seq
  FROM plant_agent_reader, plant_human_event_writer, PUBLIC;

-- Read role: state builder and trend analyzer only.
GRANT SELECT ON TABLE soil_sensor_readings, irrigation_events, human_events TO plant_agent_reader;

-- Writer: confirmation state machine only; it must not read plant-control data.
GRANT SELECT, INSERT, UPDATE ON TABLE pending_human_events, human_events TO plant_human_event_writer;
GRANT USAGE, SELECT ON SEQUENCE pending_human_events_id_seq, human_events_id_seq TO plant_human_event_writer;

-- Manual rollback (only after the application feature switch is disabled):
-- REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM plant_agent_reader, plant_human_event_writer;
-- REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM plant_agent_reader, plant_human_event_writer;
-- REVOKE CONNECT ON DATABASE soil_data FROM plant_agent_reader, plant_human_event_writer;
-- DROP ROLE plant_human_event_writer;
-- DROP ROLE plant_agent_reader;
