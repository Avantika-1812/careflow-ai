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


def _query_scoped(query: str, where: dict, n_results: int) -> list[dict]:
    """Run one Chroma similarity search restricted to `where`, returning raw hit dicts."""
    collection_size = _collection.count()
    if collection_size == 0:
        return []
    results = _collection.query(
        query_texts=[query], n_results=min(n_results, collection_size), where=where,
    )
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]
    return [{"text": d, "metadata": m, "distance": dist} for d, m, dist in zip(docs, metas, distances)]


def retrieve(query: str, role: str, patient_db_id: int | None, top_k: int = config.TOP_K) -> list[dict]:
    """Retrieve relevant chunks, filtered by role and scoped to a patient (plus global docs).

    Patient-specific and global documents are queried *separately* (via Chroma's `where`
    filter) rather than with one unscoped top-K search across the whole collection. With a
    large shared reference library (e.g. MTSamples) indexed alongside a handful of chunks per
    patient, a single flat query lets the reference library's sheer volume crowd the patient's
    own records out of the candidate pool entirely before role/patient filtering even applies --
    a query about a specific patient could silently retrieve zero of that patient's documents.

    Separate queries alone aren't enough, though: raw embedding distance isn't reliable enough
    to then merge-and-sort the two pools together, because long, narrative reference documents
    can score as more "semantically dense" for a generic query than a patient's own terse
    structured records -- so pure distance-sort can *still* crowd the patient out entirely
    (confirmed empirically: it did). Since a patient-scoped conversation is, by definition,
    about that patient, we instead reserve the majority of slots for patient-specific hits
    whenever a patient is in scope, filling only the remainder with the best global candidates
    (so guidelines/reference material can still surface alongside patient data, just not
    displace it).
    """
    global_allowed = _retrieve_scope(query, role, {"patient_id": 0}, top_k * 2)

    if patient_db_id is None:
        return global_allowed[:top_k]

    patient_allowed = _retrieve_scope(query, role, {"patient_id": patient_db_id}, top_k * 2)

    patient_slots = min(len(patient_allowed), max(top_k - 2, (top_k + 1) // 2))
    hits = patient_allowed[:patient_slots]
    hits += global_allowed[: top_k - len(hits)]
    return hits


def _retrieve_scope(query: str, role: str, where: dict, fetch_n: int) -> list[dict]:
    candidates = _query_scoped(query, where, fetch_n)
    allowed = [c for c in candidates if _role_can_access(c["metadata"].get("allowed_roles", ""), role)]
    allowed.sort(key=lambda c: c["distance"])
    return allowed


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


def _prepare_turn(query: str, role: str, patient_db_id: int | None, history: list[dict] | None):
    """Shared setup for both the streaming and non-streaming generation paths: retrieve,
    and either return a canned short-circuit answer (no API key / no hits) or a ready-to-send
    messages list + context_text (needed afterwards for grounding) + formatted sources."""
    hits = retrieve(query, role, patient_db_id)

    if _groq_client is None:
        return {
            "short_circuit": {
                "answer": (
                    "CareFlow AI is not fully configured: no GROQ_API_KEY was found. "
                    "Get a free key at console.groq.com and set it in your .env file to enable "
                    "answer generation. Retrieval is still working -- see 'sources' below."
                ),
                "sources": _format_sources(hits),
                "confidence": "low",
            }
        }

    if not hits:
        return {
            "short_circuit": {
                "answer": (
                    "I couldn't find any authorized documents relevant to that query. "
                    "This may be due to your access role, the selected patient, or the "
                    "information may not be in the indexed knowledge base."
                ),
                "sources": [],
                "confidence": "low",
            }
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

    return {"messages": messages, "context_text": context_text, "sources": _format_sources(hits)}


def generate_answer(
    query: str,
    role: str,
    patient_db_id: int | None,
    history: list[dict] | None = None,
) -> dict:
    prep = _prepare_turn(query, role, patient_db_id, history)
    if "short_circuit" in prep:
        return prep["short_circuit"]

    response = _groq_client.chat.completions.create(
        model=config.MODEL_NAME,
        max_tokens=1024,
        messages=prep["messages"],
    )

    answer_text = response.choices[0].message.content
    confidence = score_grounding(answer_text, prep["context_text"])

    return {
        "answer": answer_text,
        "sources": prep["sources"],
        "confidence": confidence,
    }


def generate_answer_stream(
    query: str,
    role: str,
    patient_db_id: int | None,
    history: list[dict] | None = None,
):
    """Generator version for token-by-token streaming over SSE. Yields dicts:
      {"type": "sources", "sources": [...]}          -- once, immediately
      {"type": "token", "text": "..."}                -- repeatedly, as tokens arrive
      {"type": "done", "answer": "...", "confidence": "high"}  -- once, at the end

    The caller (app.py) is responsible for persisting the assembled answer + confidence
    after the "done" event -- this function only generates, it never touches the database.
    """
    prep = _prepare_turn(query, role, patient_db_id, history)
    if "short_circuit" in prep:
        sc = prep["short_circuit"]
        yield {"type": "sources", "sources": sc["sources"]}
        yield {"type": "token", "text": sc["answer"]}
        yield {"type": "done", "answer": sc["answer"], "confidence": sc["confidence"]}
        return

    yield {"type": "sources", "sources": prep["sources"]}

    stream = _groq_client.chat.completions.create(
        model=config.MODEL_NAME,
        max_tokens=1024,
        messages=prep["messages"],
        stream=True,
    )

    full_answer = ""
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            full_answer += delta
            yield {"type": "token", "text": delta}

    confidence = score_grounding(full_answer, prep["context_text"])
    yield {"type": "done", "answer": full_answer, "confidence": confidence}
