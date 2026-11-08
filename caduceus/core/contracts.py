"""Types shared by the core, the importers and the tools. Frozen at D0: change only through review.

See docs/contracts.md for every lane's function signatures.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class SourceKind(StrEnum):
    DTC = "dtc"                    # consumer DNA raw data: 23andMe, AncestryDNA
    PROMETHEASE = "promethease"    # a Promethease report (.zip or .html)
    APPLE_HEALTH = "apple_health"  # Apple Health export.zip (may contain clinical-records/ FHIR JSON)
    FHIR = "fhir"                  # a FHIR R4 Bundle or resource as JSON
    OTHER = "other"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"    # left running by a crash or restart; the next run resumes it
    CANCELLED = "cancelled"


class CallState(StrEnum):
    CALLED = "called"              # the file has a genotype for this marker
    NO_CALL = "no_call"            # the chip tested the marker but the read failed ("--")
    NOT_ON_CHIP = "not_on_chip"    # this callset does not include the marker
    UNRESOLVED = "unresolved"      # the name maps to several markers or cannot be resolved


@dataclass(frozen=True)
class SourceArtifact:
    id: int
    kind: str
    sha256: str
    original_name: str
    stored_path: str               # relative to the data directory: sources/<sha256>/<name>
    size_bytes: int
    added_at: str


@dataclass(frozen=True)
class ImportRun:
    id: int
    source_id: int
    parser: str
    parser_version: str
    started_at: str


@dataclass
class ImportResult:
    status: str                    # a RunStatus value, or "skipped" when this version already imported it
    run_id: int | None = None
    source_id: int | None = None
    counts: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Call:
    marker: str                    # the name that was asked for
    state: CallState
    genotype: str | None = None    # plus strand as the file gives it: "AG", "A" (haploid), "DI"
    chrom: str | None = None
    pos: int | None = None
    callset_id: int | None = None
    resolved_marker: str | None = None  # set when the name resolved through marker_alias

    def as_dict(self) -> dict:
        data = asdict(self)
        data["state"] = self.state.value
        return data
