"""
Import MTSamples into CareFlow AI as a clinical reference library.

MTSamples (mtsamples.com) is a public collection of real, de-identified medical
transcription sample reports -- actual clinical narrative text (H&Ps, discharge summaries,
radiology reports, SOAP notes) across many specialties, originally compiled by Tara Boyle
and widely used in clinical NLP research (CC0 / public domain). Unlike Synthea's structured,
templated synthetic records, this gives CareFlow AI real unstructured clinical prose to
retrieve against -- a meaningfully different and complementary test of the RAG pipeline.

These samples are standalone reference reports, not part of any single patient's
longitudinal record, so they're ingested as global documents (patient_id = NULL, same
mechanism as the hospital guideline) rather than attached to a patient. They're clinical
content, so -- like patient records -- they stay restricted to clinician/admin, not staff.

Source data: downloaded from a public Hugging Face mirror (anurag-raapid/mt-samples) as
.parquet, which needs pyarrow to read -- pyarrow conflicts with this project's pinned numpy
(chromadb's onnxruntime needs numpy<2, recent pyarrow needs numpy>=2). Rather than fight that
version conflict in the app's own venv, the one-time parquet -> JSON extraction is done with a
disposable venv (see README "Adding your own data"), and this script only ever reads the
resulting plain JSON cache at data/mtsamples_cache/samples.json -- no pyarrow dependency here.

Usage:
    python -m scripts.import_mtsamples              # import everything from the JSON cache
    python -m scripts.import_mtsamples --limit 20    # import only the first 20
    python -m scripts.import_mtsamples --dry-run     # list what would be imported

If the app is already running (locally or via `docker compose up`) while you run this,
restart it afterwards: Chroma's already-open collection handle in that process won't see
rows written by this separate process until it reopens the store. `rag.heal_vector_store_if_empty()`
won't catch this case (Chroma isn't *empty*, just stale in that one process) -- a restart is
the fix, same as it would be for any other process that mutates a SQLite file out from under
a second process that already has it open.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import db, rag  # noqa: E402

CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "mtsamples_cache" / "samples.json"
REFERENCE_ROLES = ["clinician", "admin"]


def import_mtsamples(limit: int | None = None, dry_run: bool = False):
    if not CACHE_FILE.exists():
        print(f"No cache found at {CACHE_FILE}.")
        print("Extract it first -- see README 'Adding your own data' for the two-step process.")
        return

    samples = json.loads(CACHE_FILE.read_text())
    if limit:
        samples = samples[:limit]

    print(f"Loaded {len(samples)} MTSamples reference reports from cache.")
    if dry_run:
        for s in samples[:20]:
            print(f"  [dry-run] would import \"{s['title']}\" ({s['specialty']})")
        if len(samples) > 20:
            print(f"  ... and {len(samples) - 20} more")
        return

    db.init_db()
    imported = 0
    with db.session() as conn:
        for i, s in enumerate(samples):
            title = f"{s['title']} — {s['specialty']} (MTSamples #{i + 1})"
            exists = conn.execute(
                "SELECT id FROM documents WHERE title = %s AND source_filename = 'mtsamples'", (title,)
            ).fetchone()
            if exists:
                continue
            rag.ingest_document(
                conn=conn, patient_db_id=None, title=title, doc_type="clinical_reference",
                allowed_roles=REFERENCE_ROLES, text=s["text"],
                source_filename="mtsamples", uploaded_by="mtsamples_import",
            )
            imported += 1

    print(f"Imported {imported} new MTSamples reference documents into CareFlow AI.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import MTSamples clinical reference reports into CareFlow AI.")
    parser.add_argument("--limit", type=int, default=None, help="Only import the first N samples")
    parser.add_argument("--dry-run", action="store_true", help="List what would be imported without writing")
    args = parser.parse_args()
    import_mtsamples(limit=args.limit, dry_run=args.dry_run)
    db._pool.close()
