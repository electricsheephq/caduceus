-- Samples live separately from vault provenance; source_id refers to the vault.
CREATE TABLE IF NOT EXISTS sample (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    unit TEXT,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    tz_offset_min INTEGER NOT NULL,
    local_day TEXT NOT NULL,
    value REAL,
    value_text TEXT,
    category TEXT NOT NULL DEFAULT 'wellness',
    dedupe_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS workout (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    duration REAL,
    duration_unit TEXT,
    statistics_json TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'wellness',
    dedupe_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS activity_summary (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    record_json TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'wellness',
    dedupe_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS daily_rollup (
    metric TEXT NOT NULL,
    day TEXT NOT NULL,
    value REAL NOT NULL,
    n INTEGER NOT NULL,
    source_name TEXT NOT NULL,
    calc_version TEXT NOT NULL,
    PRIMARY KEY (metric, day)
);
