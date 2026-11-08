-- vault.db core tables. Owner: orchestrator. Every statement must stay idempotent.

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subject (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- An original file, copied once into sources/<sha256>/ and never overwritten.
CREATE TABLE IF NOT EXISTS source_artifact (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES subject(id),
    kind TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    added_at TEXT NOT NULL
);

-- One attempt to import a source with one parser version.
CREATE TABLE IF NOT EXISTS import_run (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source_artifact(id),
    parser TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'interrupted', 'cancelled')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    counts_json TEXT NOT NULL DEFAULT '{}',
    errors_json TEXT NOT NULL DEFAULT '[]',
    progress_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS import_run_source ON import_run(source_id, parser, status);
CREATE UNIQUE INDEX IF NOT EXISTS import_run_once
    ON import_run(source_id, parser, parser_version) WHERE status = 'succeeded';

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_event (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

-- Medications the user confirmed, or that came from records. drug is the generic name, lower case.
CREATE TABLE IF NOT EXISTS medication_event (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL DEFAULT 1 REFERENCES subject(id),
    drug TEXT NOT NULL,
    brand TEXT,
    event TEXT NOT NULL CHECK (event IN ('start', 'dose_change', 'stop')),
    dose TEXT,
    occurred_on TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    source_id INTEGER REFERENCES source_artifact(id),
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (drug, event, occurred_on, source)
);

-- Labs and other measurements from records (FHIR Observation) or entered by the user.
CREATE TABLE IF NOT EXISTS observation (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL DEFAULT 1 REFERENCES subject(id),
    code_system TEXT,
    code TEXT,
    name TEXT NOT NULL,
    value_num REAL,
    value_text TEXT,
    unit TEXT,
    effective_at TEXT NOT NULL,
    ref_low REAL,
    ref_high REAL,
    interpretation TEXT,
    category TEXT NOT NULL DEFAULT 'records',
    source TEXT NOT NULL,
    source_id INTEGER REFERENCES source_artifact(id),
    dedupe_key TEXT NOT NULL UNIQUE,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS observation_code_time ON observation(code, effective_at);
CREATE INDEX IF NOT EXISTS observation_name_time ON observation(name, effective_at);

CREATE TABLE IF NOT EXISTS condition (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL DEFAULT 1 REFERENCES subject(id),
    code_system TEXT,
    code TEXT,
    name TEXT NOT NULL,
    clinical_status TEXT,
    onset TEXT,
    category TEXT NOT NULL DEFAULT 'records',
    source TEXT NOT NULL,
    source_id INTEGER REFERENCES source_artifact(id),
    dedupe_key TEXT NOT NULL UNIQUE,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL DEFAULT 1 REFERENCES subject(id),
    ts TEXT NOT NULL,
    text TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'wellness',
    created_at TEXT NOT NULL
);

-- The search index: one row per searchable record, mirrored into FTS5 by the triggers below.
CREATE TABLE IF NOT EXISTS search_doc (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL,
    ts TEXT,
    UNIQUE (kind, ref)
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    title, body, content='search_doc', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS search_doc_ai AFTER INSERT ON search_doc BEGIN
    INSERT INTO search_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS search_doc_ad AFTER DELETE ON search_doc BEGIN
    INSERT INTO search_fts(search_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS search_doc_au AFTER UPDATE ON search_doc BEGIN
    INSERT INTO search_fts(search_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
    INSERT INTO search_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
