"""A synthetic demo persona: DNA raw data, a variant report and an Apple Health export with health records.

Every value is invented and the persona is nobody. The genotypes were chosen to illustrate published public
science (GLP1R, GIPR, TCF7L2, APOE, CYP2C19, SLCO1B1, CYP2C9, VKORC1), with positions checked against dbSNP
(GRCh37, plus strand). The variant-report text is our own wording, never copied from any annotation source.

Story: type 2 diabetes, on semaglutide (Ozempic 0.5 mg weekly) since 2026-05-04. Weight fell about 4 kg over
12 weeks, then plateaued; HbA1c fell from 8.4% to 7.8%, less than trials report on average.

    python tests/synth/persona.py OUT_DIR [--markers N]
"""
from __future__ import annotations

import json
import random
import sys
import uuid
import zipfile
from datetime import date, datetime, time, timedelta
from io import TextIOWrapper
from pathlib import Path
from xml.sax.saxutils import quoteattr
from zoneinfo import ZoneInfo

from .promethease import html_report

ZONE = ZoneInfo("America/Chicago")
FIRST_DAY, LAST_DAY = date(2026, 2, 2), date(2026, 11, 7)
START = date(2026, 5, 4)            # semaglutide start
NAMESPACE = uuid.UUID("6f1c3a52-6a77-4c8e-9f5e-0c2d4b1a9e10")

# rsid: (chromosome, GRCh37 position, genotype as a 23andMe file writes it)
GENOTYPES = {
    "rs6923761": ("6", 39034072, "AA"),      # GLP1R Gly168Ser, two Ser
    "rs10305420": ("6", 39016636, "CC"),     # GLP1R Pro7Leu, no T
    "rs1800437": ("19", 46181392, "GG"),     # GIPR Glu354Gln, no C
    "rs7903146": ("10", 114758349, "CT"),    # TCF7L2, one T
    "rs429358": ("19", 45411941, "TT"),      # APOE, with rs7412: e3/e3
    "rs7412": ("19", 45412079, "CC"),
    "rs4244285": ("10", 96541616, "AG"),     # CYP2C19*2, one copy
    "rs4986893": ("10", 96540410, "GG"),     # no CYP2C19*3
    "rs12248560": ("10", 96521657, "CC"),    # no CYP2C19*17
    "rs4149056": ("12", 21331549, "CT"),     # SLCO1B1 decreased function, one copy
    "rs1799853": ("10", 96702047, "CC"),     # no CYP2C9*2
    "rs1057910": ("10", 96741053, "AA"),     # no CYP2C9*3
    "rs9923231": ("16", 31107689, "CT"),     # VKORC1 -1639, one copy
    "rs4988235": ("2", 136608646, "AG"),     # MCM6, lactase persistence
    "rs1815739": ("11", 66328095, "CT"),     # ACTN3 R577X
    "rs762551": ("15", 75041917, "AC"),      # CYP1A2*1F
    "rs1801133": ("1", 11856378, "AG"),      # MTHFR 677
    "rs12913832": ("15", 28365618, "AG"),    # HERC2
    "rs17822931": ("16", 48258198, "CC"),    # ABCC11
    "rs4680": ("22", 19951271, "AG"),        # COMT Val158Met
}

# Variant-report rows, in our own words: (rsid, genes, magnitude, repute, summary, drugs)
REPORT = [
    ("rs6923761", ["GLP1R"], 2, "Bad", "Two Ser168 copies of GLP1R. In one pooled study each copy came with a slightly "
     "smaller HbA1c drop on GLP-1 drugs; a later oral-semaglutide study found no effect.",
     ["semaglutide", "liraglutide", "dulaglutide"]),
    ("rs10305420", ["GLP1R"], 1, "", "No Leu7 copy of GLP1R. The T allele came with slightly more self-reported "
     "weight loss on semaglutide or tirzepatide.", ["semaglutide", "tirzepatide"]),
    ("rs1800437", ["GIPR"], 0.5, "", "No Gln354 copy of GIPR. The C allele was linked to vomiting on tirzepatide "
     "in one large survey.", ["tirzepatide"]),
    ("rs7903146", ["TCF7L2"], 2.5, "Bad", "One copy of the T allele, which is linked to a higher risk of type 2 "
     "diabetes.", []),
    ("rs429358", ["APOE"], 1, "", "Common genotype at one of the two markers that set the APOE type.", []),
    ("rs7412", ["APOE"], 1, "", "Common genotype at one of the two markers that set the APOE type.", []),
    ("rs4244285", ["CYP2C19"], 2.5, "Bad", "One CYP2C19*2 copy, so an intermediate metabolizer. Clopidogrel may work "
     "less well; CPIC has guidance.", ["clopidogrel"]),
    ("rs4986893", ["CYP2C19"], 0, "", "No CYP2C19*3 copy.", []),
    ("rs12248560", ["CYP2C19"], 0, "", "No CYP2C19*17 copy.", []),
    ("rs4149056", ["SLCO1B1"], 2, "Bad", "One decreased-function SLCO1B1 copy. Statin levels run higher, with more "
     "risk of muscle side effects; CPIC has statin guidance.", ["simvastatin", "atorvastatin"]),
    ("rs1799853", ["CYP2C9"], 0, "", "No CYP2C9*2 copy.", []),
    ("rs1057910", ["CYP2C9"], 0, "", "No CYP2C9*3 copy.", []),
    ("rs9923231", ["VKORC1"], 2, "", "One VKORC1 -1639A copy; warfarin needs tend to be lower. CPIC has guidance.",
     ["warfarin"]),
    ("rs4988235", ["MCM6"], 1, "Good", "One lactase-persistence allele, so likely able to digest lactose as an "
     "adult.", []),
    ("rs1815739", ["ACTN3"], 1, "", "One copy of the ACTN3 577X variant; its effect on muscle is small and it is "
     "common.", []),
    ("rs762551", ["CYP1A2"], 1, "", "One CYP1A2*1F copy; caffeine clearance is likely average.", []),
    ("rs1801133", ["MTHFR"], 1.5, "", "One MTHFR 677T copy: slightly lower enzyme activity, usually with no health "
     "effect.", []),
    ("rs12913832", ["HERC2"], 1, "", "One blue-eye-linked and one brown-eye-linked allele; eyes other than blue are "
     "more likely.", []),
    ("rs17822931", ["ABCC11"], 0.5, "", "Wet earwax type, the most common type outside East Asia.", []),
    ("rs4680", ["COMT"], 1, "", "One Val and one Met copy at COMT Val158Met: intermediate enzyme activity.", []),
]
GENOSETS = [("APOE e3/e3", ["APOE"], 2, "Good", "APOE e3/e3, the most common APOE type.")]

CONDITIONS = [
    ("Type 2 diabetes mellitus without complications", "E11.9", "44054006", "2023-09-14"),
    ("Hyperlipidemia, unspecified", "E78.5", "55822004", "2024-01-10"),
]
MEDICATIONS = [
    ("Ozempic (semaglutide) 0.5 mg/dose pen", "0.5 mg under the skin once weekly", START.isoformat()),
    ("Metformin 1000 mg tablet", "1000 mg twice daily", "2023-09-20"),
    ("Atorvastatin 20 mg tablet", "20 mg once daily", "2024-01-15"),
]
# (LOINC, display, unit, (reference low, high), [(day, value)])
LABS = [
    ("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "%", (4.0, 5.6),
     [("2025-11-03", 8.1), ("2026-04-27", 8.4), ("2026-08-10", 7.9), ("2026-10-26", 7.8)]),
    ("1558-6", "Fasting glucose [Mass/volume] in Serum or Plasma", "mg/dL", (70, 99),
     [("2026-04-27", 168), ("2026-08-10", 151), ("2026-10-26", 147)]),
    ("13457-7", "Cholesterol in LDL [Mass/volume] in Serum or Plasma by calculation", "mg/dL", (None, 99),
     [("2025-11-03", 96), ("2026-10-26", 89)]),
    ("2160-0", "Creatinine [Mass/volume] in Serum or Plasma", "mg/dL", (0.5, 1.1), [("2026-04-27", 0.9)]),
]

_CHROMOSOMES = [(str(n), size) for n, size in zip(range(1, 23), (
    249, 243, 198, 191, 181, 171, 159, 146, 141, 136, 135, 134, 115, 107, 103, 90, 81, 78, 59, 63, 48, 51))]
_CHROMOSOMES += [("X", 155), ("Y", 59), ("MT", 0)]


def _skip_markers() -> set[str]:
    """Markers the persona must not get random genotypes at: its own, and every knowledge-pack marker."""
    skip = set(GENOTYPES)
    try:
        from ..knowledge.pack import load_pack
    except ImportError:
        return skip
    for entry in load_pack().entries:
        skip.update(m["rsid"] for m in entry.get("markers", []) if isinstance(m, dict) and m.get("rsid"))
    return skip | {"rs429358", "rs7412"}


def _filler(markers: int, seed: int):
    """(chromosome, position, rsid, genotype) rows spread over the genome, in file order."""
    rng, skip = random.Random(seed), _skip_markers()
    sizes = [size for _, size in _CHROMOSOMES]
    counts = [round(markers * size / sum(sizes)) for size in sizes]
    counts[-1] = min(30, markers)  # a few mitochondrial markers
    number = 1000
    for (chrom, size), count in zip(_CHROMOSOMES, counts):
        span = max(size, 1) * 1_000_000
        for position in sorted(rng.sample(range(10_000, span), count)) if count else ():
            number += 1
            while f"rs{number}" in skip:
                number += 1
            if chrom == "Y":
                genotype = "--"                      # the persona is female
            elif chrom == "MT":
                genotype = rng.choice("ACGT")
            else:
                pair = rng.choice(("AG", "CT", "AC", "GT"))
                roll = rng.random()
                genotype = ("--" if roll < 0.01 else pair[0] * 2 if roll < 0.62 else
                            pair if roll < 0.92 else pair[1] * 2)
            yield chrom, position, f"rs{number}", genotype


def write_dna(path, *, markers: int = 600_000, seed: int = 7) -> Path:
    """23andMe v5 layout: a zip holding one tab-separated text file, sorted by chromosome and position."""
    path = Path(path)
    order = {chrom: i for i, (chrom, _) in enumerate(_CHROMOSOMES)}
    known = [(chrom, pos, rsid, gt) for rsid, (chrom, pos, gt) in GENOTYPES.items()]
    rows = sorted([*_filler(markers, seed), *known], key=lambda row: (order[row[0]], row[1]))
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("genome_Demo_Persona_v5_Full_20261101.txt", date_time=(2026, 11, 1, 9, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        with archive.open(info, "w") as raw, TextIOWrapper(raw, encoding="utf-8", newline="\n") as text:
            text.write("# Synthetic demo persona in the 23andMe v5 layout. Invented data; not a real person.\n")
            text.write("# reference human assembly build 37 (also known as Annotation Release 104)\n")
            text.write("# rsid\tchromosome\tposition\tgenotype\n")
            for chrom, pos, rsid, genotype in rows:
                text.write(f"{rsid}\t{chrom}\t{pos}\t{genotype}\n")
    return path


def _geno(genotype: str) -> str:
    return "(" + ";".join(genotype) + ")"


def write_variant_report(path) -> Path:
    """A report in the Promethease container format, holding only the rows above."""
    path = Path(path)
    rows, rsinfo = [], {}
    for rsid, genes, magnitude, repute, summary, drugs in REPORT:
        geno = _geno(GENOTYPES[rsid][2])
        row = {"rsnum": rsid, "geno": geno, "magnitude": magnitude, "repute": repute, "genes": genes,
               "genosummary": summary}
        if drugs:
            row["med"] = drugs
        rows.append(row)
        rsinfo[rsid] = {geno: {"mag": magnitude, "repute": repute}}
    for title, genes, magnitude, repute, summary in GENOSETS:
        rows.append({"title": title, "genes": genes, "magnitude": magnitude, "repute": repute, "genosummary": summary})
    html = html_report(rows=rows, maps=(rsinfo, {}))
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report/promethease.html", html)
        archive.writestr("report/readme.txt", "Synthetic demo persona. Invented report; not a real person.")
    return path


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S %z")


def _record(kind: str, value, start: datetime, end: datetime | None = None, *, source: str, unit: str = "") -> str:
    prefix = "HKCategoryTypeIdentifier" if kind == "SleepAnalysis" else "HKQuantityTypeIdentifier"
    attrs = {"type": prefix + kind, "sourceName": source, "unit": unit, "value": str(value),
             "creationDate": _stamp(end or start), "startDate": _stamp(start), "endDate": _stamp(end or start)}
    if not unit:
        del attrs["unit"]
    return "<Record " + " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items()) + "/>\n"


def weight_on(day: date, rng: random.Random) -> float:
    """About 104 kg, then 4.3 kg lost over the first 12 weeks on semaglutide, then level."""
    weeks = max(0, (day - START).days) / 7
    loss = 4.3 * (1 - (1 - min(weeks, 12) / 12) ** 1.6) - 0.02 * max(0.0, weeks - 12)
    return round(104.2 - loss + rng.uniform(-0.35, 0.35), 1)


def _day_records(day: date, rng: random.Random):
    at = lambda h, m=0: datetime.combine(day, time(h, m), ZONE)  # noqa: E731
    on = day >= START
    if rng.random() < 0.86:
        yield _record("BodyMass", weight_on(day, rng), at(7, rng.randint(0, 25)), source="Demo Scale", unit="kg")
    yield _record("RestingHeartRate", round(rng.gauss(68 if on else 66, 1.5)), at(6, 30),
                  source="Demo Watch", unit="count/min")
    total = rng.randint(5200, 9400)
    for i, hour in enumerate((8, 10, 12, 14, 17, 19)):
        part = total // 6 + (total % 6 if i == 5 else 0)
        yield _record("StepCount", part, at(hour), at(hour + 1), source="Demo Watch", unit="count")
        if i % 2 == 0:  # the phone counts some of the same steps
            yield _record("StepCount", int(part * 0.8), at(hour, 5), at(hour, 55), source="Demo iPhone", unit="count")
    for minute in range(8 * 60, 22 * 60, 30):
        yield _record("HeartRate", round(rng.gauss(74, 7)), at(minute // 60, minute % 60),
                      source="Demo Watch", unit="count/min")
    bed = datetime.combine(day - timedelta(days=1), time(23, 0), ZONE) + timedelta(minutes=rng.randint(-25, 40))
    wake = at(6, 30) + timedelta(minutes=rng.randint(-20, 25))
    yield _record("SleepAnalysis", "HKCategoryValueSleepAnalysisInBed", bed - timedelta(minutes=10), wake,
                  source="Demo Watch")
    cuts = [bed, bed + (wake - bed) * 0.35, bed + (wake - bed) * 0.5, bed + (wake - bed) * 0.8, wake]
    for stage, start, end in zip(("AsleepCore", "AsleepDeep", "AsleepCore", "AsleepREM"), cuts, cuts[1:]):
        yield _record("SleepAnalysis", "HKCategoryValueSleepAnalysis" + stage, start, end, source="Demo Watch")


def _resource_id(label: str) -> str:
    return str(uuid.uuid5(NAMESPACE, label))


def clinical_resources() -> list[dict]:
    """FHIR R4 resources, one per file, as Apple Health Records exports them."""
    resources = []
    for text, icd10, snomed, onset in CONDITIONS:
        resources.append({
            "resourceType": "Condition", "id": _resource_id(text),
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                           "code": "active"}]},
            "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
                                               "code": "confirmed"}]},
            "code": {"text": text, "coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": icd10},
                                              {"system": "http://snomed.info/sct", "code": snomed}]},
            "onsetDateTime": onset})
    for text, dosage, start in MEDICATIONS:
        resources.append({
            "resourceType": "MedicationStatement", "id": _resource_id(text), "status": "active",
            "medicationCodeableConcept": {"text": text}, "effectivePeriod": {"start": start},
            "dosage": [{"text": dosage}]})
    for code, display, unit, (low, high), points in LABS:
        reference = {key: {"value": bound, "unit": unit} for key, bound in (("low", low), ("high", high))
                     if bound is not None}
        for day, value in points:
            flag = "H" if high is not None and value > high else "L" if low is not None and value < low else "N"
            resources.append({
                "resourceType": "Observation", "id": _resource_id(f"{code}-{day}"), "status": "final",
                "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                          "code": "laboratory"}]}],
                "code": {"text": display, "coding": [{"system": "http://loinc.org", "code": code, "display": display}]},
                "effectiveDateTime": f"{day}T08:30:00-05:00",
                "valueQuantity": {"value": value, "unit": unit, "system": "http://unitsofmeasure.org", "code": unit},
                "referenceRange": [reference],
                "interpretation": [{"coding": [{
                    "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation", "code": flag}]}]})
    return resources


def write_apple_export(path, *, seed: int = 7) -> Path:
    """An Apple Health export.zip: export.xml plus clinical-records/*.json."""
    path = Path(path)
    rng = random.Random(seed)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        with archive.open("apple_health_export/export.xml", "w") as raw, \
                TextIOWrapper(raw, encoding="utf-8", newline="\n") as xml:
            xml.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE HealthData [\n<!ELEMENT HealthData ANY>\n]>\n')
            xml.write('<HealthData locale="en_US">\n <ExportDate value="2026-11-07 20:00:00 -0600"/>\n')
            xml.write(' <Me HKCharacteristicTypeIdentifierDateOfBirth="1972-03-14" '
                      'HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexFemale"/>\n')
            day = FIRST_DAY
            while day <= LAST_DAY:
                xml.write("".join(_day_records(day, rng)))
                day += timedelta(days=1)
            xml.write("</HealthData>\n")
        for resource in clinical_resources():
            name = f"apple_health_export/clinical-records/{resource['resourceType']}-{resource['id']}.json"
            archive.writestr(name, json.dumps(resource, indent=1))
    return path


def write_persona(out_dir, *, markers: int = 600_000, seed: int = 7) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    return {"dna": write_dna(out / "persona-23andme-v5.zip", markers=markers, seed=seed),
            "report": write_variant_report(out / "persona-variant-report.zip"),
            "apple": write_apple_export(out / "persona-apple-health.zip", seed=seed)}


def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        sys.exit("usage: persona.py OUT_DIR [--markers N]")
    count = int(args[args.index("--markers") + 1]) if "--markers" in args else 600_000
    for kind, written in write_persona(args[0], markers=count).items():
        print(f"{kind}: {written} ({written.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
