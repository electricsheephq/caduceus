-- Research metadata only: requests never contain the user's genotypes.
CREATE TABLE IF NOT EXISTS watch (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    terms_json TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    last_since TEXT
);
CREATE TABLE IF NOT EXISTS research_item (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    journal TEXT,
    year INTEGER,
    pub_date TEXT,
    doi TEXT,
    pmid TEXT,
    pmcid TEXT,
    url TEXT,
    summary TEXT NOT NULL DEFAULT '' CHECK (length(summary) <= 600),
    matched_json TEXT NOT NULL DEFAULT '{}',
    category TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    watch_id INTEGER NOT NULL REFERENCES watch(id),
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS research_item_pmid ON research_item(pmid);
CREATE INDEX IF NOT EXISTS research_item_doi ON research_item(doi);
CREATE TABLE IF NOT EXISTS delivery (
    id INTEGER PRIMARY KEY,
    watch_id INTEGER NOT NULL REFERENCES watch(id),
    run_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('seed', 'new', 'none')),
    item_ids_json TEXT NOT NULL,
    errors_json TEXT NOT NULL
);
