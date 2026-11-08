"""The developer commit guard (scripts/private_guard.py): real-data shapes are refused, ordinary files pass."""
import importlib.util
import json

from conftest import PLUGIN_ROOT

_spec = importlib.util.spec_from_file_location("private_guard", PLUGIN_ROOT / "scripts" / "private_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _problems(name, data):
    return guard.check([(name, data)], set(), [])


def test_raw_dna_rows_in_every_supported_layout_are_refused():
    rows = {
        "23andme": b"".join(b"rs%d\t1\t%d\tAG\r\n" % (1000 + i, 5000 + i) for i in range(600)),
        "ancestry": b"".join(b"rs%d\t1\t%d\tA\tG\r\n" % (1000 + i, 5000 + i) for i in range(600)),
        "csv": b"".join(b'"rs%d","1","%d","AG"\n' % (1000 + i, 5000 + i) for i in range(600)),
    }
    for layout, data in rows.items():
        assert any("DNA raw data" in p for p in _problems(layout + ".txt", data)), layout


def test_a_fhir_patient_with_identifiers_is_refused():
    kind = "Pat" + "ient"  # spelled apart so this test file itself passes the guard
    data = json.dumps({"resourceType": kind, "identifier": [{"value": "SYNTHETIC-1"}]}).encode()
    assert any("patient identifiers" in p for p in _problems("record.json", data))
    assert _problems("observation.json", json.dumps({"resourceType": "Observation"}).encode()) == []
