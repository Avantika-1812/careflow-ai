"""
Rebuild the Chroma vector index from Postgres, without touching Postgres data.

Why this exists: document *metadata and full text* live in Postgres, but the *searchable
vectors* live in Chroma's local files (data/chroma/). These two stores can drift apart --
for example if data/chroma/ is cleared, lost, or simply never created on a machine that
already has a populated Postgres database (as happened when this project was moved between
directories: the Postgres named volume followed the project, but the Chroma bind-mount did
not). When that happens, documents exist and look fine in the UI, but every query returns
"no relevant documents found" because retrieval has nothing to search.

Usage:
    python -m scripts.reindex_chroma            # reindex every document
    python -m scripts.reindex_chroma --dry-run   # report what would be reindexed, no writes
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import db, rag  # noqa: E402


def reindex_all(dry_run: bool = False):
    db.init_db()
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id, patient_id, title, doc_type, allowed_roles, full_text FROM documents ORDER BY id"
        ).fetchall()

    if not rows:
        print("No documents found in Postgres -- nothing to reindex.")
        return

    print(f"Found {len(rows)} documents in Postgres.")
    if dry_run:
        for r in rows:
            print(f"  [dry-run] would reindex id={r['id']} \"{r['title']}\" ({r['doc_type']})")
        return

    total_chunks = 0
    for r in rows:
        if not r["full_text"]:
            print(f"  skip id={r['id']} \"{r['title']}\" -- no full_text stored, cannot reindex")
            continue
        n = rag.index_chunks(
            document_id=r["id"],
            patient_db_id=r["patient_id"],
            title=r["title"],
            doc_type=r["doc_type"],
            allowed_roles_csv=r["allowed_roles"],
            text=r["full_text"],
        )
        total_chunks += n
        print(f"  reindexed id={r['id']} \"{r['title']}\" -> {n} chunks")

    print(f"Done. {total_chunks} chunks indexed across {len(rows)} documents.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rebuild the Chroma vector index from Postgres.")
    parser.add_argument("--dry-run", action="store_true", help="List what would be reindexed without writing")
    args = parser.parse_args()
    reindex_all(dry_run=args.dry_run)
