-- ColdTrace schema, view, roles and grants — docs/DESIGN.md §4.
-- Idempotent: safe to re-run. Run via `uv run python -m scripts.setup_db`.
--
-- Roles
--   coldtrace_admin  full access to ColdTrace tables; used by migration and ingest scripts
--   coldtrace_agent  the app/agent role. Exactly four grants (bottom of file):
--                    SELECT + INSERT on audit_log, USAGE on its sequence, SELECT on the view.
--                    No UPDATE, no DELETE, no access to raw telemetry or trucks.
--
-- This fixes gap #1 of the reference project, whose setup script never granted the
-- INSERT its audit logger needed (docs/DESIGN.md §2.2).

BEGIN;

-- --- Types -------------------------------------------------------------------------

DO $$ BEGIN
    CREATE TYPE trip_status AS ENUM ('in_transit', 'at_depot', 'completed');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- --- trucks (§4.1) ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS trucks (
    truck_id      VARCHAR(20)  PRIMARY KEY,
    plate_number  VARCHAR(20)  NOT NULL,
    cargo_type    VARCHAR(50)  NOT NULL,
    min_temp_c    FLOAT        NOT NULL,
    max_temp_c    FLOAT        NOT NULL,
    home_depot    VARCHAR(100) NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CHECK (min_temp_c < max_temp_c)
);

-- --- telemetry (§4.2) ---------------------------------------------------------------
-- No data_quality_flag column: flags are computed at query time by src/data_quality.py.

CREATE TABLE IF NOT EXISTS telemetry (
    reading_id            BIGSERIAL    PRIMARY KEY,
    truck_id              VARCHAR(20)  NOT NULL REFERENCES trucks (truck_id),
    shipment_id           VARCHAR(20)  NOT NULL,
    recorded_at           TIMESTAMPTZ  NOT NULL,
    ingested_at           TIMESTAMPTZ  NOT NULL,
    lat                   FLOAT        NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon                   FLOAT        NOT NULL CHECK (lon BETWEEN -180 AND 180),
    trip_status           trip_status  NOT NULL,
    temperature_c         FLOAT        NOT NULL,  -- no range CHECK: faulty readings must land
    cargo_condition_code  VARCHAR(10)  NOT NULL CHECK (cargo_condition_code IN ('OK', 'WARN', 'CRIT')),
    delay_probability     FLOAT        NOT NULL CHECK (delay_probability BETWEEN 0 AND 1),
    route_risk_index      FLOAT        NOT NULL,
    UNIQUE (truck_id, recorded_at)
);

CREATE INDEX IF NOT EXISTS telemetry_shipment_time_idx ON telemetry (shipment_id, recorded_at);
CREATE INDEX IF NOT EXISTS telemetry_time_idx ON telemetry (recorded_at);

-- --- vw_fleet_with_quality (§4.2) ---------------------------------------------------
-- The only telemetry object the agent role can read. Joins each reading to its truck's
-- cargo thresholds; the tool functions fetch from here and apply_quality_checks() adds
-- data_quality_flag in Python.

CREATE OR REPLACE VIEW vw_fleet_with_quality AS
SELECT
    t.reading_id,
    t.truck_id,
    t.shipment_id,
    t.recorded_at,
    t.ingested_at,
    t.lat,
    t.lon,
    t.trip_status::text AS trip_status,
    t.temperature_c,
    t.cargo_condition_code,
    t.delay_probability,
    t.route_risk_index,
    k.cargo_type,
    k.min_temp_c,
    k.max_temp_c,
    k.home_depot
FROM telemetry t
JOIN trucks k USING (truck_id);

-- --- audit_log (§4.4) ---------------------------------------------------------------
-- Append-only. One row per dispatcher question; each accept/override appends a row
-- that amends it. created_at has no default: it is set in Python so row_hash covers it.

CREATE TABLE IF NOT EXISTS audit_log (
    log_id                SERIAL       PRIMARY KEY,
    amends_log_id         INT          REFERENCES audit_log (log_id),
    session_id            UUID         NOT NULL,
    created_at            TIMESTAMPTZ  NOT NULL,
    dispatcher_question   TEXT,
    tool_calls            JSONB        NOT NULL DEFAULT '[]',
    data_quality_flags    JSONB        NOT NULL DEFAULT '[]',
    final_recommendation  TEXT,
    sop_clause_cited      VARCHAR(100),
    human_decision        VARCHAR(20)  NOT NULL DEFAULT 'pending'
                          CHECK (human_decision IN ('pending', 'accepted', 'overridden')),
    override_reason       TEXT,
    token_count_in        INTEGER      CHECK (token_count_in >= 0),
    token_count_out       INTEGER      CHECK (token_count_out >= 0),
    previous_row_hash     CHAR(64),    -- NULL only for the first row in the chain
    row_hash              CHAR(64)     NOT NULL UNIQUE,
    -- Agent rows carry the question and stay pending; decision rows amend an agent row.
    CHECK (
        (amends_log_id IS NULL AND dispatcher_question IS NOT NULL AND human_decision = 'pending')
        OR (amends_log_id IS NOT NULL AND human_decision <> 'pending')
    ),
    CHECK (human_decision <> 'overridden' OR override_reason IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS audit_log_session_idx ON audit_log (session_id);
CREATE INDEX IF NOT EXISTS audit_log_amends_idx ON audit_log (amends_log_id);
CREATE INDEX IF NOT EXISTS audit_log_tool_calls_idx ON audit_log USING GIN (tool_calls jsonb_path_ops);

-- --- Roles --------------------------------------------------------------------------
-- Created without passwords; scripts/setup_db.py sets them from the environment so no
-- secret ever lives in this file.

DO $$ BEGIN
    CREATE ROLE coldtrace_admin LOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE ROLE coldtrace_agent LOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- --- Grants -------------------------------------------------------------------------
-- Start from nothing on every ColdTrace object, then grant exactly what each role needs.

REVOKE ALL ON trucks, telemetry, audit_log, vw_fleet_with_quality FROM PUBLIC;
REVOKE ALL ON trucks, telemetry, audit_log, vw_fleet_with_quality FROM coldtrace_agent;
REVOKE ALL ON SEQUENCE telemetry_reading_id_seq, audit_log_log_id_seq FROM PUBLIC, coldtrace_agent;

GRANT USAGE ON SCHEMA public TO coldtrace_admin, coldtrace_agent;

GRANT ALL ON trucks, telemetry, audit_log, vw_fleet_with_quality TO coldtrace_admin;
GRANT ALL ON SEQUENCE telemetry_reading_id_seq, audit_log_log_id_seq TO coldtrace_admin;

-- coldtrace_agent: these four lines are the entire privilege set.
GRANT SELECT ON audit_log TO coldtrace_agent;                        -- read last hash to extend the chain
GRANT INSERT ON audit_log TO coldtrace_agent;                        -- append rows
GRANT USAGE ON SEQUENCE audit_log_log_id_seq TO coldtrace_agent;     -- nextval() for log_id
GRANT SELECT ON vw_fleet_with_quality TO coldtrace_agent;            -- telemetry, via the view only

COMMIT;
