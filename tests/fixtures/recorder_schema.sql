-- Minimal 2026.7-era HA recorder schema subset (schema rev ~50) for tests.
-- Mirrors the columns the database/ surface actually touches:
--   states.metadata_id -> states_meta.entity_id, epoch-float *_ts columns,
--   events.event_type_id -> event_types, statistics(_meta), recorder_runs.

CREATE TABLE states_meta (
    metadata_id INTEGER PRIMARY KEY,
    entity_id   TEXT NOT NULL UNIQUE
);

CREATE TABLE state_attributes (
    attributes_id INTEGER PRIMARY KEY,
    hash          INTEGER,
    shared_attrs  TEXT
);

CREATE TABLE states (
    state_id            INTEGER PRIMARY KEY,
    metadata_id         INTEGER REFERENCES states_meta (metadata_id),
    state               TEXT,
    attributes_id       INTEGER REFERENCES state_attributes (attributes_id),
    last_updated_ts     REAL,
    last_changed_ts     REAL,
    last_reported_ts    REAL,
    old_state_id        INTEGER,
    origin_idx          INTEGER,
    context_id_bin      BLOB,
    context_user_id_bin BLOB
);
CREATE INDEX ix_states_metadata_id_last_updated_ts
    ON states (metadata_id, last_updated_ts);

CREATE TABLE event_types (
    event_type_id INTEGER PRIMARY KEY,
    event_type    TEXT NOT NULL UNIQUE
);

CREATE TABLE events (
    event_id       INTEGER PRIMARY KEY,
    event_type_id  INTEGER REFERENCES event_types (event_type_id),
    time_fired_ts  REAL,
    data_id        INTEGER,
    origin_idx     INTEGER,
    context_id_bin BLOB
);
CREATE INDEX ix_events_event_type_id_time_fired_ts
    ON events (event_type_id, time_fired_ts);

CREATE TABLE statistics_meta (
    id                  INTEGER PRIMARY KEY,
    statistic_id        TEXT NOT NULL UNIQUE,
    source              TEXT,
    unit_of_measurement TEXT,
    has_mean            INTEGER,
    has_sum             INTEGER,
    name                TEXT
);

CREATE TABLE statistics (
    id            INTEGER PRIMARY KEY,
    created_ts    REAL,
    metadata_id   INTEGER REFERENCES statistics_meta (id),
    start_ts      REAL,
    mean          REAL,
    min           REAL,
    max           REAL,
    last_reset_ts REAL,
    state         REAL,
    sum           REAL
);
CREATE UNIQUE INDEX ix_statistics_statistic_id_start_ts
    ON statistics (metadata_id, start_ts);

CREATE TABLE statistics_short_term (
    id            INTEGER PRIMARY KEY,
    created_ts    REAL,
    metadata_id   INTEGER REFERENCES statistics_meta (id),
    start_ts      REAL,
    mean          REAL,
    min           REAL,
    max           REAL,
    last_reset_ts REAL,
    state         REAL,
    sum           REAL
);

CREATE TABLE recorder_runs (
    run_id             INTEGER PRIMARY KEY,
    start              DATETIME,
    "end"              DATETIME,
    closed_incorrectly INTEGER DEFAULT 0,
    created            DATETIME
);

CREATE TABLE schema_changes (
    change_id      INTEGER PRIMARY KEY,
    schema_version INTEGER,
    changed        DATETIME
);
