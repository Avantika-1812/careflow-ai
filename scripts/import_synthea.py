"""
Import Synthea-generated synthetic patients into CareFlow AI.

Synthea (https://github.com/synthetichealth/synthea) generates realistic, fully synthetic
patient populations -- demographics, encounters, conditions, medications, observations --
with no real PHI involved. This script reads its CSV export and converts each patient into
CareFlow's document format (one patient-record doc, one lab-report doc from observations,
one prescription doc from medications), then ingests them the same way seed.py does.

Usage:
    python -m scripts.import_synthea /path/to/synthea/output/csv [--limit N]

Run Synthea first, e.g.:
    java -jar synthea-with-dependencies.jar --exporter.csv.export=true \\
        --exporter.fhir.export=false -p 25 Massachusetts

If the app is already running while you run this, restart it afterwards: Chroma's already-open
collection handle in that process won't see rows written by this separate process until it
reopens the store.
"""
import argparse
import csv
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import auth, db, rag  # noqa: E402

PATIENT_ROLES = ["clinician", "admin"]


def _age(birthdate: str) -> int:
    y, m, d = (int(p) for p in birthdate.split("-"))
    today = date.today()
    return today.year - y - ((today.month, today.day) < (m, d))


def _read_csv(csv_dir: Path, name: str) -> list[dict]:
    path = csv_dir / name
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_patient_record_text(patient: dict, encounters: list[dict], conditions: list[dict]) -> str:
    name = f"{patient['FIRST']} {patient['LAST']}"
    lines = [
        "PATIENT RECORD (Synthea-generated synthetic data)",
        f"Patient ID: {patient['Id']}",
        f"Name: {name}",
        f"DOB: {patient['BIRTHDATE']}",
        f"Sex: {'Male' if patient['GENDER'] == 'M' else 'Female'}",
        f"Race/Ethnicity: {patient.get('RACE', 'unknown')}/{patient.get('ETHNICITY', 'unknown')}",
        "",
        "Visit History:",
    ]
    enc_by_id = {e["Id"]: e for e in encounters}
    for enc in sorted(encounters, key=lambda e: e["START"])[:25]:
        reason = enc.get("REASONDESCRIPTION") or enc.get("ENCOUNTERCLASS", "visit")
        lines.append(f"- {enc['START'][:10]}: {enc.get('DESCRIPTION', 'Encounter')} ({reason})")

    active_conditions = [c for c in conditions if not c.get("STOP")]
    if active_conditions:
        lines.append("")
        lines.append("Chronic/Active Conditions:")
        for c in active_conditions:
            lines.append(f"- {c['DESCRIPTION']} (onset {c['START'][:10]})")

    resolved = [c for c in conditions if c.get("STOP")]
    if resolved:
        lines.append("")
        lines.append("Resolved Conditions:")
        for c in resolved:
            lines.append(f"- {c['DESCRIPTION']} ({c['START'][:10]} to {c['STOP'][:10]})")

    return "\n".join(lines)


def build_lab_report_text(patient: dict, observations: list[dict]) -> str | None:
    if not observations:
        return None
    lines = [
        "LABORATORY / OBSERVATION REPORT (Synthea-generated synthetic data)",
        f"Patient ID: {patient['Id']}",
        f"Name: {patient['FIRST']} {patient['LAST']}",
        "",
    ]
    by_date = defaultdict(list)
    for o in observations:
        if o.get("CATEGORY") not in ("laboratory", "vital-signs"):
            continue
        by_date[o["DATE"][:10]].append(o)

    for dt in sorted(by_date.keys())[-15:]:
        lines.append(f"{dt}:")
        for o in by_date[dt]:
            unit = f" {o['UNITS']}" if o.get("UNITS") else ""
            lines.append(f"  - {o['DESCRIPTION']}: {o['VALUE']}{unit}")
        lines.append("")

    return "\n".join(lines) if by_date else None


def build_prescription_text(patient: dict, medications: list[dict]) -> str | None:
    if not medications:
        return None
    lines = [
        "PRESCRIPTION HISTORY (Synthea-generated synthetic data)",
        f"Patient ID: {patient['Id']}",
        f"Name: {patient['FIRST']} {patient['LAST']}",
        "",
    ]
    for m in sorted(medications, key=lambda m: m["START"]):
        status = f"ongoing since {m['START'][:10]}" if not m.get("STOP") else f"{m['START'][:10]} to {m['STOP'][:10]}"
        reason = f" -- for {m['REASONDESCRIPTION']}" if m.get("REASONDESCRIPTION") else ""
        lines.append(f"- {m['DESCRIPTION']} ({status}){reason}")

    return "\n".join(lines)


def import_synthea(csv_dir: Path, limit: int | None = None):
    patients = _read_csv(csv_dir, "patients.csv")
    encounters = _read_csv(csv_dir, "encounters.csv")
    conditions = _read_csv(csv_dir, "conditions.csv")
    observations = _read_csv(csv_dir, "observations.csv")
    medications = _read_csv(csv_dir, "medications.csv")

    if not patients:
        print(f"No patients.csv found under {csv_dir}. Did you run Synthea with --exporter.csv.export=true?")
        return

    if limit:
        patients = patients[:limit]

    enc_by_patient = defaultdict(list)
    for e in encounters:
        enc_by_patient[e["PATIENT"]].append(e)
    cond_by_patient = defaultdict(list)
    for c in conditions:
        cond_by_patient[c["PATIENT"]].append(c)
    obs_by_patient = defaultdict(list)
    for o in observations:
        obs_by_patient[o["PATIENT"]].append(o)
    med_by_patient = defaultdict(list)
    for m in medications:
        med_by_patient[m["PATIENT"]].append(m)

    db.init_db()
    imported = 0
    with db.session() as conn:
        for p in patients:
            if _age(p["BIRTHDATE"]) < 0:
                continue
            name = f"{p['FIRST'].rstrip('0123456789')} {p['LAST'].rstrip('0123456789')}"
            mrn = f"SYNTHEA-{p['Id'][:8].upper()}"

            existing = conn.execute("SELECT id FROM patients WHERE mrn = %s", (mrn,)).fetchone()
            if existing:
                continue

            row = conn.execute(
                "INSERT INTO patients (mrn, name, dob, sex) VALUES (%s, %s, %s, %s) RETURNING id",
                (mrn, name.strip(), p["BIRTHDATE"], "Male" if p["GENDER"] == "M" else "Female"),
            ).fetchone()
            patient_db_id = row["id"]

            patient_text = build_patient_record_text(p, enc_by_patient[p["Id"]], cond_by_patient[p["Id"]])
            rag.ingest_document(
                conn=conn, patient_db_id=patient_db_id, title=f"Patient Record - {name.strip()}",
                doc_type="patient_record", allowed_roles=PATIENT_ROLES, text=patient_text,
                source_filename="synthea_import", uploaded_by="synthea_import",
            )

            lab_text = build_lab_report_text(p, obs_by_patient[p["Id"]])
            if lab_text:
                rag.ingest_document(
                    conn=conn, patient_db_id=patient_db_id, title=f"Lab/Vitals Report - {name.strip()}",
                    doc_type="lab_report", allowed_roles=PATIENT_ROLES, text=lab_text,
                    source_filename="synthea_import", uploaded_by="synthea_import",
                )

            rx_text = build_prescription_text(p, med_by_patient[p["Id"]])
            if rx_text:
                rag.ingest_document(
                    conn=conn, patient_db_id=patient_db_id, title=f"Prescription History - {name.strip()}",
                    doc_type="prescription", allowed_roles=PATIENT_ROLES, text=rx_text,
                    source_filename="synthea_import", uploaded_by="synthea_import",
                )

            imported += 1

    print(f"Imported {imported} Synthea patients (plus their records/labs/prescriptions) into CareFlow AI.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import Synthea CSV output into CareFlow AI.")
    parser.add_argument("csv_dir", type=Path, help="Path to Synthea's output/csv directory")
    parser.add_argument("--limit", type=int, default=None, help="Only import the first N patients")
    args = parser.parse_args()
    import_synthea(args.csv_dir, args.limit)
    db._pool.close()
