"""Lookup states, alias ambiguity and callset selection, using synthetic calls only."""
import pytest

from caduceus.core import sources
from caduceus.core.contracts import CallState, SourceKind
from caduceus.core.db import write_txn
from caduceus.genome.calls import NoCallset, callsets, get_calls
from caduceus.genome.dtc import import_dtc
from synth.dtc import make_23andme


def _import(vault, tmp_path, name, inject):
    path = make_23andme(tmp_path / name, n=2, inject=inject)
    src, _ = sources.store_source(vault, path, SourceKind.DTC)
    assert import_dtc(vault, src).status == "succeeded"
    return vault.execute("SELECT id FROM callset WHERE source_id=?", (src.id,)).fetchone()[0]


def test_no_callset(vault):
    assert callsets(vault) == []
    with pytest.raises(NoCallset):
        get_calls(vault, ["rs1"])


def test_states_and_aliases(vault, tmp_path):
    cid = _import(vault, tmp_path, "dna.txt", {"rs1": "AG", "rs2": "--", "i1": "D"})
    with write_txn(vault):
        vault.executemany("INSERT INTO marker_alias(alias, marker, source) VALUES (?,?,?)", [
            ("rs100", "rs1", "synthetic"), ("old-name", "i1", "synthetic"),
            ("rs200", "rs1", "synthetic"), ("rs200", "rs2", "synthetic"),
            ("missing-alias", "rs999", "synthetic"),
        ])
        vault.execute("INSERT INTO genotype_call VALUES (?, 'rs3', 'X', 42, '')", (cid,))
    names = [" RS1 ", "rs2", "RS3", "I1", "rs999", "RS100", "old-name", "rs200", "missing-alias", "unknown"]
    calls = get_calls(vault, names)
    assert list(calls) == names
    assert calls[" RS1 "].state == CallState.CALLED and calls[" RS1 "].genotype == "AG"
    assert calls[" RS1 "].marker == " RS1 " and calls[" RS1 "].resolved_marker is None
    assert calls["rs2"].state == calls["RS3"].state == CallState.NO_CALL
    assert calls["I1"].state == CallState.CALLED and calls["I1"].genotype == "D"
    assert calls["rs999"].state == CallState.NOT_ON_CHIP
    assert calls["RS100"].resolved_marker == "rs1" and calls["RS100"].genotype == "AG"
    assert calls["old-name"].resolved_marker == "i1"
    assert calls["rs200"].state == calls["unknown"].state == CallState.UNRESOLVED
    assert calls["missing-alias"].state == CallState.NOT_ON_CHIP
    assert calls["missing-alias"].resolved_marker == "rs999"
    assert all(call.callset_id == cid for call in calls.values())
    assert (calls[" RS1 "].chrom, calls[" RS1 "].pos) == ("1", 1001)


def test_newest_and_explicit_callset(vault, tmp_path):
    first = _import(vault, tmp_path, "one_v3_dna.txt", {"rs1": "AA"})
    second = _import(vault, tmp_path, "two_v5_dna.txt", {"rs1": "GG"})
    assert [row["id"] for row in callsets(vault)] == [second, first]
    assert get_calls(vault, ["rs1"])["rs1"].genotype == "GG"
    assert get_calls(vault, ["rs1"], callset_id=first)["rs1"].genotype == "AA"
    with write_txn(vault):
        vault.execute("UPDATE callset SET created_at='2099-01-01T00:00:00Z' WHERE id=?", (first,))
    assert get_calls(vault, ["rs1"])["rs1"].callset_id == first
    with pytest.raises(NoCallset):
        get_calls(vault, ["rs1"], callset_id=999)
    assert get_calls(vault, []) == {}
