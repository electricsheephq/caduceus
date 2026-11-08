import json
import threading
import time
import zipfile

import pytest

from caduceus.core import sources
from caduceus.health import apple
from caduceus.health.trends import trend
from tests.synth.apple import make_export


def _source(vault, path):
    return sources.store_source(vault, path, "apple_health")[0]


def test_import_dates_dedupe_sources_sleep_and_workouts(samples, vault, tmp_path):
    path = make_export(tmp_path / "health.zip", days=40, zip_slip=True)
    src = _source(vault, path)
    path.unlink()  # Only the stored copy may be read.
    result = apple.import_apple_health(samples, vault, src)
    assert result.status == "succeeded", result.warnings
    assert result.counts["samples"] == 40*9
    assert result.counts["records"] == 40*11
    assert result.counts["me_skipped"] == 1
    dates = samples.execute("SELECT start_utc,tz_offset_min FROM sample WHERE type LIKE '%BodyMass' ORDER BY start_utc").fetchall()
    assert dates[0]["start_utc"] == "2024-02-01T20:00:00Z"
    assert dates[-1]["start_utc"] == "2024-03-11T19:00:00Z"
    assert {r["tz_offset_min"] for r in dates} == {-480, -420}
    assert {p["value"] for p in trend(samples, "steps", bucket="day")["points"]} == {8000}
    assert trend(samples, "steps", bucket="day")["points"][0]["sources"] == ["Watch"]
    sleep = {p["day"]: p["value"] for p in trend(samples, "sleep_hours", bucket="day")["points"]}
    assert sleep["2024-03-10"] == pytest.approx(7)
    assert sleep["2024-03-09"] == pytest.approx(8)
    assert samples.execute("SELECT count(*) FROM sample WHERE type LIKE '%BloodPressure%'").fetchone()[0] == 80
    stats = json.loads(samples.execute("SELECT statistics_json FROM workout").fetchone()[0])
    assert stats[0]["sum"] == "100"
    assert samples.execute("SELECT count(*) FROM activity_summary").fetchone()[0] == 1
    assert vault.execute("SELECT count(*) FROM observation").fetchone()[0] == 2
    assert not (tmp_path.parent / "evil").exists()
    assert apple.import_apple_health(samples, vault, src).status == "skipped"


@pytest.mark.parametrize("late", [False, True])
def test_entities_refused_and_recorded(samples, vault, tmp_path, late):
    path = make_export(tmp_path / "entity.zip", days=1, entity=True)
    if late:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("export.xml", b'<?xml version="1.0"?>'+b' '*70000+
                             b'<!DOCTYPE HealthData [<!ENTITY bad "no">]><HealthData/>')
    result = apple.import_apple_health(samples, vault, _source(vault, path))
    assert result.status == "failed"
    assert "UnsafeXml" in result.warnings[0]
    assert sources.run_info(vault, result.run_id)["status"] == "failed"
    assert samples.execute("SELECT count(*) FROM sample").fetchone()[0] == 0


def test_cancel_then_resume_and_crash_dedupe(samples, vault, tmp_path, monkeypatch):
    src = _source(vault, make_export(tmp_path / "resume.zip", days=0, extra_records=22000))
    cancel = threading.Event()
    update = sources.update_progress
    def stop(conn, run, counts):
        update(conn, run, counts)
        cancel.set()
    monkeypatch.setattr(sources, "update_progress", stop)
    first = apple.import_apple_health(samples, vault, src, cancel=cancel)
    assert first.status == "cancelled"
    assert samples.execute("SELECT count(*) FROM sample").fetchone()[0] == 10000
    # Simulate a process dying after a batch committed on the retry.
    def crash(*args):
        raise RuntimeError("synthetic interrupted batch")
    monkeypatch.setattr(sources, "update_progress", crash)
    assert apple.import_apple_health(samples, vault, src).status == "failed"
    monkeypatch.setattr(sources, "update_progress", update)
    resumed = apple.import_apple_health(samples, vault, src)
    assert resumed.status == "succeeded"
    assert samples.execute("SELECT count(*) FROM sample").fetchone()[0] == 22000
    assert resumed.counts["samples"] == 12000
    assert len(trend(samples, "heart_rate", bucket="day")["points"]) == 1


def test_precancel_and_inflate_limit(samples, vault, tmp_path, monkeypatch):
    src = _source(vault, make_export(tmp_path / "small.zip", days=1))
    cancel = threading.Event()
    cancel.set()
    assert apple.import_apple_health(samples, vault, src, cancel=cancel).status == "cancelled"
    monkeypatch.setattr(apple, "MAX_MEMBER", 10)
    result = apple.import_apple_health(samples, vault, src)
    assert result.status == "failed"
    assert samples.execute("SELECT count(*) FROM sample").fetchone()[0] == 0


def test_million_records_under_one_minute(samples, vault, tmp_path):
    src = _source(vault, make_export(tmp_path / "million.zip", days=0, extra_records=1_000_000))
    started = time.monotonic()
    result = apple.import_apple_health(samples, vault, src)
    elapsed = time.monotonic()-started
    assert result.status == "succeeded", result.warnings
    assert result.counts["samples"] == 1_000_000
    assert elapsed < 60, f"million-record import exceeded 60s: {elapsed:.1f}s"
