-- vault.db genome tables. Owner: orchestrator. Written by lane A1 (callset, genotype_call),
-- lane A2 (annotation_snapshot, marker_alias) and lane C (annotation_assertion, derived_call).

-- One imported DNA raw-data file.
CREATE TABLE IF NOT EXISTS callset (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL DEFAULT 1 REFERENCES subject(id),
    source_id INTEGER NOT NULL UNIQUE REFERENCES source_artifact(id),
    vendor TEXT NOT NULL,
    chip_version TEXT NOT NULL DEFAULT '',
    build TEXT NOT NULL,
    marker_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- genotype is the plus-strand string as the file gives it: 'AG', 'A' (haploid), 'DI', '--' (no-call).
CREATE TABLE IF NOT EXISTS genotype_call (
    callset_id INTEGER NOT NULL REFERENCES callset(id),
    marker TEXT NOT NULL,
    chrom TEXT NOT NULL,
    pos INTEGER NOT NULL,
    genotype TEXT NOT NULL,
    PRIMARY KEY (callset_id, marker)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS genotype_call_marker ON genotype_call(marker);
CREATE INDEX IF NOT EXISTS genotype_call_locus ON genotype_call(chrom, pos);

-- Another name for a marker: a merged rsID, or a Promethease redirect.
CREATE TABLE IF NOT EXISTS marker_alias (
    alias TEXT NOT NULL,
    marker TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (alias, marker)
);

-- A variant-report record exactly as decoded (historical layer; kept locally, never redistributed).
CREATE TABLE IF NOT EXISTS annotation_snapshot (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source_artifact(id),
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    marker TEXT,
    title TEXT,
    geno TEXT,
    magnitude REAL,
    repute TEXT,
    summary TEXT,
    genes_json TEXT NOT NULL DEFAULT '[]',
    category TEXT NOT NULL DEFAULT 'general',
    record_json TEXT NOT NULL,
    UNIQUE (source_id, ordinal)
);
CREATE INDEX IF NOT EXISTS annotation_snapshot_marker ON annotation_snapshot(marker);
CREATE INDEX IF NOT EXISTS annotation_snapshot_magnitude ON annotation_snapshot(magnitude);

-- A current claim about a marker from a named, versioned, licensed source.
CREATE TABLE IF NOT EXISTS annotation_assertion (
    id INTEGER PRIMARY KEY,
    marker TEXT NOT NULL,
    allele TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    release TEXT NOT NULL,
    license TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    claim_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (marker, allele, source, release)
);

-- A call derived from several markers (APOE, CYP2C19 diplotype), with how sure it is.
CREATE TABLE IF NOT EXISTS derived_call (
    id INTEGER PRIMARY KEY,
    callset_id INTEGER NOT NULL REFERENCES callset(id),
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('called', 'ambiguous', 'not_assessed')),
    category TEXT NOT NULL DEFAULT 'pgx',
    basis_json TEXT NOT NULL,
    pack_version TEXT NOT NULL,
    UNIQUE (callset_id, name, pack_version)
);
