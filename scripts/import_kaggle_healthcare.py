"""
Import the Kaggle "Healthcare Dataset" into CareFlow AI as patient admission records.

This is the well-known synthetic healthcare dataset originally published on Kaggle
(kaggle.com/datasets/prasad22/healthcare-dataset) -- 55,500 fully synthetic ("dummy data")
hospital admission records: Name, Age, Gender, Blood Type, Medical Condition, Admission/
Discharge dates, Doctor, Hospital, Insurance Provider, Billing Amount, Room Number,
Admission Type, Medication, Test Results. No real patients -- Kaggle's own dataset card
describes it as synthetic data generated for a classification-problem tutorial.

Kaggle itself requires an account + API key to download via their API, which only the
project owner has. This script instead reads from a plain CSV already fetched from a public
GitHub mirror of the same file (no auth needed) and cached at data/kaggle_cache/ -- same
"fetch once outside the app, then import from a local cache" pattern as the Synthea and
MTSamples importers.

Each row becomes one patient + one admission-record document. With 55,500 rows, importing
everything takes a while (one Postgres insert + one Chroma embed per row) -- use --limit for
a faster partial import (e.g. for a demo), or omit it to import the full dataset.

Usage:
    python -m scripts.import_kaggle_healthcare --limit 1000   # fast: first 1,000 rows
    python -m scripts.import_kaggle_healthcare                # full 55,500 rows (slower)
    python -m scripts.import_kaggle_healthcare --dry-run

If the app is already running while you run this, restart it afterwards -- see the note in
scripts/import_mtsamples.py for why.
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import db, rag  # noqa: E402

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "kaggle_cache" / "healthcare_dataset.csv"
PATIENT_ROLES = ["clinician", "admin"]


def _approx_dob(age: str) -> str | None:
    try:
        birth_year = 2026 - int(age)
        return f"{birth_year}-01-01"
    except (ValueError, TypeError):
        return None


def build_record_text(row: dict, mrn: str) -> str:
    try:
        billing = f"${float(row['Billing Amount']):,.2f}"
    except (ValueError, KeyError):
        billing = row.get("Billing Amount", "unknown")

    return "\n".join([
        "PATIENT ADMISSION RECORD (Kaggle Healthcare Dataset -- synthetic data)",
        f"Patient ID: {mrn}",
        f"Name: {row['Name']}",
        f"Age: {row['Age']}",
        f"Gender: {row['Gender']}",
        f"Blood Type: {row['Blood Type']}",
        "",
        "Admission Details:",
        f"- Admission Date: {row['Date of Admission']}",
        f"- Admission Type: {row['Admission Type']}",
        f"- Discharge Date: {row['Discharge Date']}",
        f"- Attending Doctor: {row['Doctor']}",
        f"- Hospital: {row['Hospital']}",
        f"- Room Number: {row['Room Number']}",
        f"- Insurance Provider: {row['Insurance Provider']}",
        f"- Billing Amount: {billing}",
        "",
        "Clinical Information:",
        f"- Medical Condition: {row['Medical Condition']}",
        f"- Medication Prescribed: {row['Medication']}",
        f"- Test Results: {row['Test Results']}",
    ])


def import_kaggle_healthcare(limit: int | None = None, dry_run: bool = False):
    if not CSV_PATH.exists():
        print(f"No CSV found at {CSV_PATH}.")
        print("Fetch it first -- see README 'Adding your own data' for the source URL.")
        return

    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if limit:
        rows = rows[:limit]

    print(f"Loaded {len(rows)} rows from the Kaggle healthcare dataset.")
    if dry_run:
        for i, row in enumerate(rows[:10]):
            print(f"  [dry-run] would import KAGGLE-{i+1:06d}: {row['Name']} ({row['Medical Condition']})")
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")
        return

    db.init_db()
    imported = 0
    with db.session() as conn:
        for i, row in enumerate(rows):
            mrn = f"KAGGLE-{i + 1:06d}"
            exists = conn.execute("SELECT id FROM patients WHERE mrn = %s", (mrn,)).fetchone()
            if exists:
                continue

            patient_row = conn.execute(
                "INSERT INTO patients (mrn, name, dob, sex) VALUES (%s, %s, %s, %s) RETURNING id",
                (mrn, row["Name"], _approx_dob(row.get("Age")), row.get("Gender")),
            ).fetchone()
            patient_db_id = patient_row["id"]

            text = build_record_text(row, mrn)
            rag.ingest_document(
                conn=conn, patient_db_id=patient_db_id,
                title=f"Admission Record - {row['Name']} ({mrn})",
                doc_type="patient_record", allowed_roles=PATIENT_ROLES, text=text,
                source_filename="kaggle_healthcare_dataset", uploaded_by="kaggle_import",
            )
            imported += 1
            if imported % 500 == 0:
                print(f"  ... {imported} imported so far")

    print(f"Imported {imported} new patients from the Kaggle healthcare dataset.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import the Kaggle healthcare admissions dataset into CareFlow AI.")
    parser.add_argument("--limit", type=int, default=None, help="Only import the first N rows (omit for all 55,500)")
    parser.add_argument("--dry-run", action="store_true", help="List what would be imported without writing")
    args = parser.parse_args()
    import_kaggle_healthcare(limit=args.limit, dry_run=args.dry_run)
    db._pool.close()
