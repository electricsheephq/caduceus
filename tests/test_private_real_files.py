"""Local-only checks on the maintainer's real files (CADUCEUS_PRIVATE_DIR). Never run in CI.

These assert and print totals and structural counts only. They never print or compare genotypes, names or annotation text,
so a failure message cannot echo private data.
"""
import collections

import pytest

from caduceus.core import sources
from caduceus.core.contracts import SourceKind
from caduceus.core.policy import Policy

pytestmark = pytest.mark.private


def _dtc_file(private_dir):
    found = sorted(private_dir.glob("genome_*_v3_Full_*.zip"))
    if not found:
        pytest.skip("no 23andMe v3 zip in CADUCEUS_PRIVATE_DIR")
    return found[0]


def test_private_dtc_counts_and_pack_coverage(vault, private_dir):
    from caduceus.genome.calls import get_calls
    from caduceus.genome.dtc import import_dtc
    from caduceus.knowledge.pack import load_pack

    src = sources.store_source(vault, _dtc_file(private_dir), SourceKind.DTC)[0]
    result = import_dtc(vault, src)
    counts = {k: result.counts.get(k) for k in ("markers", "internal_ids", "no_calls", "haploid")}
    print("dtc", result.status, counts, "warnings", len(result.warnings))
    assert result.status == "succeeded"
    assert (counts["markers"], counts["internal_ids"], counts["no_calls"], counts["haploid"]) == (963049, 13147, 2615, 30215)
    rsids = sorted({m["rsid"] for e in load_pack().entries for m in e.get("markers", [])} | {"rs429358", "rs7412"})
    states = collections.Counter(call.state.value for call in get_calls(vault, rsids).values())
    print("pack markers", len(rsids), dict(states))
    assert states["called"] > 0


def test_private_hero_tools_run_cleanly(vault, private_dir):
    """The demo's tool calls on the real files: shapes and status counts only."""
    from caduceus import ops
    from caduceus.genome.dtc import import_dtc
    from caduceus.genome.promethease import import_promethease

    for path, kind, fn in ((_dtc_file(private_dir), SourceKind.DTC, import_dtc),
                           (private_dir / "promethease.zip", SourceKind.PROMETHEASE, import_promethease)):
        assert fn(vault, sources.store_source(vault, path, kind)[0]).status == "succeeded"
    pack, calls = ops._pack_calls(vault)
    for drug in ("semaglutide", "tirzepatide", "clopidogrel", "simvastatin", "warfarin"):
        for level in ("open", "standard"):
            result = ops._pgx(pack, drug, calls, Policy(level=level), vault=vault)
            # Totals only: per-status counts would describe the maintainer's results.
            print(drug, level, "entries", len(result["entries"]), "withheld", result.get("withheld", 0))
            assert {e["status"] for e in result["entries"]} <= {"flag", "no_flag", "not_assessed"}
