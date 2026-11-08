"""Generate artificial HealthKit/FHIR data; no copied reports or personal data."""
import json
import zipfile
from datetime import datetime, timedelta
from xml.sax.saxutils import quoteattr
from zoneinfo import ZoneInfo

LA = ZoneInfo("America/Los_Angeles")
PIVOT = "2024-03-01"


def clinical_bundle():
    observations = [{
        "resourceType": "Observation", "id": f"synthetic-a1c-{i}",
        "code": {"coding": [{"system": "http://loinc.org", "code": code, "display": "HbA1c"}]},
        "valueQuantity": {"value": value, "unit": "%"}, "effectiveDateTime": day,
        "referenceRange": [{"low": {"value": 4}, "high": {"value": 5.6}}],
        "interpretation": [{"coding": [{"code": "H", "display": "High"}]}],
    } for i, (code, value, day) in enumerate([
        ("4548-4", 8.2, "2024-02-01"), ("17856-6", 6.5, "2024-06-01")])]
    medication = {"resourceType": "MedicationStatement", "id": "synthetic-med-1",
                  "status": "active",
                  "medicationCodeableConcept": {"text": "Semaglutide"},
                  "effectivePeriod": {"start": PIVOT}}
    condition = {"resourceType": "Condition", "id": "synthetic-condition-1",
                 "code": {"text": "Type 2 diabetes", "coding": [{"code": "E11", "system": "synthetic"}]},
                 "clinicalStatus": {"coding": [{"code": "active"}]}, "onsetDateTime": "2024-01-01"}
    return {"resourceType": "Bundle", "type": "collection",
            "entry": [{"resource": r} for r in observations + [medication, condition]]}


def _stamp(value):
    return value.strftime("%Y-%m-%d %H:%M:%S %z")


def record(kind, value, start, end=None, source="Watch", unit="count"):
    attrs = {"type": ("HKCategoryTypeIdentifier" if kind == "SleepAnalysis" else "HKQuantityTypeIdentifier")+kind,
             "sourceName": source, "unit": unit, "value": str(value),
             "startDate": _stamp(start), "endDate": _stamp(end or start)}
    return "<Record " + " ".join(k+"="+quoteattr(v) for k,v in attrs.items()) + "/>\n"


def make_export(path, *, days=210, extra_records=0, entity=False, zip_slip=False):
    """Weight drops for 12 weeks after March 1, then stays level."""
    base = datetime(2024, 2, 1, 12, tzinfo=LA)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        with archive.open("apple_health_export/export.xml", "w") as stream:
            def put(text):
                stream.write(text.encode())
            put('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE HealthData [<!ELEMENT HealthData ANY>')
            if entity:
                put('<!ENTITY xxe SYSTEM "file:///synthetic-never-read">')
            put(']>\n<HealthData><Me dateOfBirth="1900-01-01"/>\n')
            pivot = datetime(2024, 3, 1, 12, tzinfo=LA)
            for i in range(days):
                when = base + timedelta(days=i)
                after = max(0, (when-pivot).days)
                kg = 100 - min(after, 84)/7
                # Deliberately mix pounds and kilograms.
                put(record("BodyMass", kg if i % 2 == 0 else kg/0.45359237, when,
                           unit="kg" if i % 2 == 0 else "lb", source="Scale"))
                put(record("StepCount", 8000, when, source="Watch"))
                put(record("StepCount", 6000, when, source="iPhone"))
                put(record("RestingHeartRate", 60, when, unit="count/min"))
                sleep_start = (when-timedelta(days=1)).replace(hour=22)
                sleep_end = when.replace(hour=6)
                middle = when.replace(hour=2)
                put(record("SleepAnalysis", "HKCategoryValueSleepAnalysisAsleepCore", sleep_start, middle, unit=""))
                put(record("SleepAnalysis", "HKCategoryValueSleepAnalysisAsleepREM", middle, sleep_end, unit=""))
                put(record("SleepAnalysis", "HKCategoryValueSleepAnalysisInBed", sleep_start, sleep_end, unit=""))
                systolic = record("BloodPressureSystolic", 120, when, unit="mmHg")
                diastolic = record("BloodPressureDiastolic", 80, when, unit="mmHg")
                put('<Correlation type="HKCorrelationTypeIdentifierBloodPressure">'+systolic+diastolic+'</Correlation>')
                put(systolic+diastolic)
            if days:
                stamp = quoteattr(_stamp(base))
                put(f'<Workout workoutActivityType="HKWorkoutActivityTypeWalking" sourceName="Watch" startDate={stamp} endDate={stamp} duration="30" durationUnit="min">')
                put('<WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned" sum="100" unit="kcal"/></Workout>')
                put('<ActivitySummary dateComponents="2024-02-01" activeEnergyBurned="400"/>')
            template = ('<Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Bulk" '
                        'unit="count/min" value="65" startDate="{0}" endDate="{0}"/>\n')
            for first in range(0, extra_records, 1000):
                put("".join(template.format((base+timedelta(seconds=i)).isoformat(" "))
                            for i in range(first, min(first+1000, extra_records))))
            put('</HealthData>')
        archive.writestr("apple_health_export/clinical-records/synthetic.json", json.dumps(clinical_bundle()))
        if zip_slip:
            archive.writestr("../evil/export.xml", "<HealthData>"+
                             record("StepCount", 999999, base, source="Evil")+"</HealthData>")
    return path
