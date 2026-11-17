-- User confirmations and corrections; additive for existing vaults.
CREATE TABLE IF NOT EXISTS phenotype_note (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL DEFAULT 1 REFERENCES subject(id),
    marker TEXT,
    trait TEXT,
    stance TEXT NOT NULL CHECK (stance IN ('confirms', 'refutes', 'unsure')),
    text TEXT NOT NULL,
    category TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS phenotype_note_marker_time ON phenotype_note(marker, created_at, id);
