import re
import uuid

import chromadb
from groq import Groq

from . import config, db

_client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
_collection = _client.get_or_create_collection(name="careflow_documents")

_groq_client = Groq(api_key=config.GROQ_API_KEY) if config.GROQ_API_KEY else None

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with", "is",
    "are", "was", "were", "be", "been", "being", "this", "that", "these", "those", "it",
    "as", "at", "by", "from", "has", "have", "had", "not", "no", "do", "does", "did",
    "will", "would", "should", "could", "can", "may", "might", "must", "i", "you", "he",
    "she", "they", "we", "patient", "source",
}


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    chunks = []
    start = 0
    text = text.strip()
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return [c for c in chunks if c.strip()]


def index_chunks(
    *,
    document_id: int,
    patient_db_id: int | None,
    title: str,
    doc_type: str,
    allowed_roles_csv: str,
    text: str,
) -> int:
    """Chunk `text` and add it to the Chroma collection under an existing document_id.
    Used both by fresh ingestion and by reindex_from_postgres.py (data/chroma can be wiped
    or lost independently of Postgres -- see that script's docstring for why)."""
    chunks = _chunk_text(text, config.CHUNK_SIZE, config.CHUNK_OVERLAP)
    if chunks:
        ids = [str(uuid.uuid4()) for _ in chunks]
        metadatas = [
            {
                "document_id": document_id,
                "patient_id": patient_db_id if patient_db_id is not None else 0,
                "title": title,
                "doc_type": doc_type,
                "allowed_roles": allowed_roles_csv,
                "chunk_index": i,
            }
            for i in range(len(chunks))
        ]
        _collection.add(ids=ids, documents=chunks, metadatas=metadatas)
    return len(chunks)


def ingest_document(
    *,
    conn,
    patient_db_id: int | None,
    title: str,
    doc_type: str,
    allowed_roles: list[str],
    text: str,
    source_filename: str | None = None,
    uploaded_by: str | None = None,
) -> dict:
    """Chunk `text`, record it in the documents table, and index it in Chroma."""
    allowed_roles_csv = ",".join(allowed_roles)
    chunk_count = len(_chunk_text(text, config.CHUNK_SIZE, config.CHUNK_OVERLAP))

    row = conn.execute(
        """INSERT INTO documents (patient_id, title, doc_type, allowed_roles, source_filename, uploaded_by, chunk_count, full_text)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (patient_db_id, title, doc_type, allowed_roles_csv, source_filename, uploaded_by, chunk_count, text),
    ).fetchone()
    document_id = row["id"]

    chunks_indexed = index_chunks(
        document_id=document_id, patient_db_id=patient_db_id, title=title,
        doc_type=doc_type, allowed_roles_csv=allowed_roles_csv, text=text,
    )

    return {"document_id": document_id, "chunks_indexed": chunks_indexed}


def delete_document(document_id: int):
    _collection.delete(where={"document_id": document_id})
    with db.session() as conn:
        conn.execute("DELETE FROM documents WHERE id = %s", (document_id,))


def heal_vector_store_if_empty():
    """Startup self-check: if Postgres has documents but Chroma has zero vectors, the two
    stores have drifted apart (e.g. data/chroma/ was wiped, lost, or never carried over to a
    new machine/directory while the Postgres volume did) -- every query would silently return
    "no relevant documents found" with no error. Detect and repair it automatically rather
    than failing silently. See scripts/reindex_chroma.py for the manual/offline equivalent."""
    if _collection.count() > 0:
        return
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id, patient_id, title, doc_type, allowed_roles, full_text FROM documents"
        ).fetchall()
    if not rows:
        return
    print(f"[rag] Vector store is empty but {len(rows)} documents exist in Postgres -- reindexing now.")
    total = 0
    for r in rows:
        if not r["full_text"]:
            continue
        total += index_chunks(
            document_id=r["id"], patient_db_id=r["patient_id"], title=r["title"],
            doc_type=r["doc_type"], allowed_roles_csv=r["allowed_roles"], text=r["full_text"],
        )
    print(f"[rag] Reindexed {total} chunks across {len(rows)} documents.")


def _role_can_access(allowed_roles_csv: str, role: str) -> bool:
    if not allowed_roles_csv:
        return True
    return role in allowed_roles_csv.split(",")


def retrieve(query: str, role: str, patient_db_id: int | None, top_k: int = config.TOP_K) -> list[dict]:
    """Retrieve relevant chunks, filtered by role and scoped to a patient (plus global docs)."""
    fetch_n = max(top_k * 4, 16)
    results = _collection.query(query_texts=[query], n_results=fetch_n)

    hits = []
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    for doc, meta, dist in zip(docs, metas, distances):
        if not _role_can_access(meta.get("allowed_roles", ""), role):
            continue
        doc_patient_id = meta.get("patient_id") or 0
        is_global = doc_patient_id == 0
        if not is_global and doc_patient_id != patient_db_id:
            continue
        hits.append({"text": doc, "metadata": meta, "distance": dist})
        if len(hits) >= top_k:
            break

    return hits


def _format_sources(hits: list[dict]) -> list[dict]:
    return [
        {
            "document_id": h["metadata"]["document_id"],
            "title": h["metadata"]["title"],
            "doc_type": h["metadata"]["doc_type"],
        }
        for h in hits
    ]


_WORD_RE = re.compile(r"[a-z0-9]+")


def _significant_words(text: str) -> set[str]:
    words = _WORD_RE.findall(text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def score_grounding(answer_text: str, context_text: str) -> str:
    """Deterministic heuristic: what fraction of the answer's significant words also
    appear in the retrieved context. Not a substitute for clinical review -- just a
    cheap, explainable signal surfaced in the UI to flag weakly-supported answers."""
    answer_words = _significant_words(answer_text)
    if not answer_words:
        return "high"
    context_words = _significant_words(context_text)
    overlap = len(answer_words & context_words) / len(answer_words)
    if overlap >= config.GROUNDING_HIGH_THRESHOLD:
        return "high"
    if overlap >= config.GROUNDING_MEDIUM_THRESHOLD:
        return "medium"
    return "low"


SYSTEM_PROMPT = """You are CareFlow AI, a hospital workflow assistant that helps healthcare \
professionals quickly find and summarize information from authorized hospital documents \
(patient records, lab reports, prescriptions, discharge summaries, and hospital guidelines).

Rules you must follow:
1. Answer ONLY using the information in the provided CONTEXT (and prior conversation turns). \
Do not use outside medical knowledge to fill gaps, and do not speculate beyond what the \
context supports.
2. If the context does not contain enough information to answer, say so plainly instead of \
guessing.
3. Always cite the source document title(s) you used, inline, like [Source: <title>].
4. You are an information-support tool, not a diagnostic system. Do not provide a diagnosis, \
treatment recommendation, or clinical judgment of your own -- only surface what is documented. \
If asked for a clinical decision, remind the user that a licensed clinician must make that call.
5. Be concise and use clear formatting (short paragraphs or bullet points) suited to a busy \
clinical workflow.
"""


def generate_answer(
    query: str,
    role: str,
    patient_db_id: int | None,
    history: list[dict] | None = None,
) -> dict:
    hits = retrieve(query, role, patient_db_id)

    if _groq_client is None:
        return {
            "answer": (
                "CareFlow AI is not fully configured: no GROQ_API_KEY was found. "
                "Get a free key at console.groq.com and set it in your .env file to enable "
                "answer generation. Retrieval is still working -- see 'sources' below."
            ),
            "sources": _format_sources(hits),
            "confidence": "low",
        }

    if not hits:
        return {
            "answer": (
                "I couldn't find any authorized documents relevant to that query. "
                "This may be due to your access role, the selected patient, or the "
                "information may not be in the indexed knowledge base."
            ),
            "sources": [],
            "confidence": "low",
        }

    context_blocks = []
    for h in hits:
        m = h["metadata"]
        context_blocks.append(f"[Document: {m['title']} ({m['doc_type']})]\n{h['text']}")
    context_text = "\n\n---\n\n".join(context_blocks)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in (history or [])[-(config.HISTORY_TURNS * 2):]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": f"CONTEXT:\n{context_text}\n\nQUESTION: {query}"})

    response = _groq_client.chat.completions.create(
        model=config.MODEL_NAME,
        max_tokens=1024,
        messages=messages,
    )

    answer_text = response.choices[0].message.content
    confidence = score_grounding(answer_text, context_text)

    return {
        "answer": answer_text,
        "sources": _format_sources(hits),
        "confidence": confidence,
    }
