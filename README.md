# CareFlow AI — Hospital Workflow Assistant

A RAG-based assistant that retrieves and summarizes information from authorized hospital
documents using natural-language queries, with login/role-based access control, per-patient
scoping, source citations, a grounding/confidence check on every answer, and a full audit trail.

## ⚠️ Before this touches real patient data

**This codebase is not yet cleared for real PHI.** Good engineering (what's in this repo) is
necessary but not sufficient — real patient data additionally requires, at minimum:

1. **A signed Business Associate Agreement (BAA)** with every vendor in the data path. Groq's
   free tier (the default LLM here) does not offer one — you'd need Azure OpenAI, AWS Bedrock,
   or another HIPAA-eligible provider instead, under a BAA, before a single real query runs.
2. **Your institution's privacy/compliance office sign-off.** This is an organizational and
   legal requirement, not something a codebase can satisfy on its own.
3. **A security review** of this specific deployment (network exposure, secrets management,
   backup/retention policy, incident response plan) — see "Known limitations" below for the
   concrete gaps as of now.
4. **TLS everywhere** (this repo runs plain HTTP locally; a real deployment needs HTTPS
   termination in front of it) and encryption at rest for the database and backups.

Until all of that is in place, treat every patient in this system as synthetic — because right
now, all of them are (see "Data" below).

## Cost

The LLM is [Groq](https://console.groq.com) running an open model (default: `openai/gpt-oss-20b`)
on its free tier — fine for development, but swap it for a BAA-covered provider before go-live
(see above).

## Features

- **Login + roles**: JWT-based auth with role-gated access (clinician, admin, staff) — patient
  list, documents, conversations, and the admin audit log all respect it.
- **User management**: admins create accounts and assign roles from the Users tab — no self-signup.
- **Multi-patient, patient-scoped RAG**: every conversation is scoped to one patient (or to
  hospital guidelines only); retrieval is structurally incapable of leaking one patient's data
  into another's answer, or leaking any patient data to a `staff` account.
- **Conversation memory**: multi-turn chat; follow-ups use prior turns as context.
- **Document upload**: clinicians/admins upload `.txt`/`.pdf` files per patient (or as a global
  guideline), chunked and indexed immediately.
- **Document viewer**: click any cited source to read the original document.
- **Grounding / confidence check**: a deterministic high/medium/low badge on every answer, based
  on how much of the answer's wording is actually traceable to the retrieved context.
- **Audit log**: every query, by whom, which role, which patient, which documents were retrieved.
- **Rate limiting, security headers, upload size caps**: see "Security hardening" below.

## Architecture

```
documents/           Seed documents (patient records, labs, prescriptions, guideline) -- synthetic
scripts/
  import_synthea.py    Converts Synthea-generated synthetic patients into CareFlow's doc format
backend/
  config.py             Settings: DB URL, JWT, rate limits, CORS, grounding thresholds
  db.py                 Postgres schema + pooled connections (psycopg3 + psycopg_pool)
  auth.py               Password hashing (PBKDF2), JWT issue/verify, role-gating dependencies
  rag.py                Chunking, Chroma indexing, patient/role-scoped retrieval,
                         conversation-aware Groq generation, grounding heuristic
  seed.py               One-time seed: demo users, patients, and hand-written documents
  app.py                FastAPI app: auth, patients, documents, conversations, admin, rate
                         limiting, security headers
frontend/
  index.html, style.css, app.js    Vanilla-JS dashboard SPA (login, chat, docs, admin)
data/                  Chroma vector store + dev-only JWT secret (created at runtime, gitignored)
docker-compose.yml      Postgres + app, for a one-command local/real deployment
Dockerfile              App container image
```

### How access control works

Every document row has `allowed_roles` (e.g. `clinician,admin`) and an optional `patient_id`
(`NULL` = global, like a hospital guideline). Retrieval filters on both: a chunk is only returned
if the caller's role is in `allowed_roles` **and** the chunk is either global or belongs to the
conversation's patient. A `staff` conversation can never have a `patient_id`, so staff queries are
structurally incapable of seeing patient data — not just hidden in the UI.

### How the grounding check works

After the LLM generates an answer, `rag.score_grounding()` compares the answer's significant words
against the retrieved context's words (deterministic set overlap, no extra API call). High overlap
→ "high confidence"; low overlap → "low confidence". This is a heuristic safety net, not a
substitute for review.

### Security hardening in place

- **Rate limiting** (slowapi): login capped separately (`LOGIN_RATE_LIMIT`, default 10/min) from
  general API traffic (`API_RATE_LIMIT`, default 120/min) to blunt brute-force and abuse.
- **Security headers**: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` on every
  response; `Strict-Transport-Security` added automatically when `CAREFLOW_ENV=production`.
- **CORS closed by default**: `ALLOWED_ORIGINS` is empty unless you set it — the frontend is
  served same-origin, so no cross-origin access is needed out of the box.
- **Upload size cap**: 20MB (`MAX_UPLOAD_BYTES`), rejected with `413` before being read into memory.
- **Production-gated JWT secret**: with `CAREFLOW_ENV=production`, the app refuses to boot without
  an explicit `JWT_SECRET` — no silent auto-generated fallback in production.
- **Parameterized SQL** throughout (psycopg placeholders) — no string-built queries.

## Setup

### Option A: Docker Compose (recommended — matches how this would actually deploy)

```bash
cd careflow-ai
cp .env.example .env
# edit .env: set GROQ_API_KEY (get a free one at https://console.groq.com/keys)
docker compose up --build
```

This starts Postgres and the app together. Open http://localhost:8000.

### Option B: Run locally against a Postgres container

```bash
cd careflow-ai
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# set GROQ_API_KEY in .env
docker compose up -d db          # Postgres only, exposed on localhost:5432
uvicorn backend.app:app --reload --port 8000
```

Open http://localhost:8000. On first run the app creates the schema and seeds demo users,
patients, and documents automatically.

**Demo accounts:**

| Role      | Username     | Password       |
|-----------|--------------|----------------|
| Clinician | `dr.patel`   | `clinician123` |
| Admin     | `admin`      | `admin123`     |
| Staff     | `front.desk` | `staff123`     |

**Change or remove these before anything resembling a real deployment.**

## Data

Everything in this system is synthetic — made up for demonstration, never real patient
information:

- **Hand-written seed patients** (`backend/seed.py` + `documents/`): 3 small, fully fictional
  patients with hand-authored records/labs/prescriptions, for quick manual testing.
- **Synthea-generated patients** (optional, larger scale): [Synthea](https://github.com/synthetichealth/synthea)
  is an open-source tool that generates realistic but entirely fictional patient populations —
  demographics, encounters, conditions, medications, observations — with no real PHI involved
  anywhere in the pipeline. To generate and import a batch:

  ```bash
  # 1. Generate N synthetic patients as CSV (requires Java 17+)
  curl -sL -o synthea.jar \
    https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar
  java -jar synthea.jar --exporter.csv.export=true --exporter.fhir.export=false -p 25 Massachusetts

  # 2. Import into CareFlow (converts Synthea's CSVs into patient_record/lab_report/prescription docs)
  cd careflow-ai && source venv/bin/activate
  python -m scripts.import_synthea ./output/csv
  ```

  This is the realistic path to "dozens/hundreds of patients" instead of hand-writing them —
  useful for load-testing retrieval and demoing at scale, still with zero real PHI.

## Adding your own data

- **Through the UI**: log in as clinician/admin, select a patient, open **Documents** →
  **+ Upload document**, and upload a `.txt` or `.pdf`. It's chunked and searchable immediately.
- **New users**: admins create accounts from the **Users** tab (full name, username, temporary
  password, role).
- **New patients**: admins can add patients via `POST /patients` (no UI form yet).
- **Bulk patients**: see "Data" above for Synthea import, or edit `backend/seed.py` directly.

## Known limitations / what's still missing before production

- **Auth**: PBKDF2 + JWT is reasonable, but a real deployment needs real identity (SSO/EHR
  integration, MFA), not hardcoded demo accounts.
- **LLM provider**: Groq's free tier has no BAA. Swap to a HIPAA-eligible provider (Azure OpenAI,
  AWS Bedrock, etc.) under a signed BAA before any real PHI is involved — see the warning at top.
- **TLS**: this app serves plain HTTP. A real deployment needs TLS termination (e.g. a reverse
  proxy or load balancer) in front of it, and `Strict-Transport-Security` alone doesn't add TLS.
- **Encryption at rest**: Postgres and Chroma data aren't encrypted at the storage layer here —
  that needs to be configured at the infrastructure level (e.g. encrypted EBS/disk volumes).
- **Embeddings**: Chroma's default embedding function is a reasonable baseline; production would
  likely use a domain-tuned or higher-quality embedding model.
- **Document ingestion**: upload supports `.txt`/`.pdf` text extraction only — no OCR for scanned
  records, no HL7/FHIR/EHR-export parsing.
- **Hallucination mitigation**: the system prompt plus the grounding-overlap heuristic reduce but
  don't eliminate hallucination risk. The confidence badge is a lexical heuristic, not a semantic
  or model-verified check — a starting point for review, never a guarantee.
- **Audit log retention/export**: logs accumulate in Postgres with no retention policy or export
  tooling yet — needed for real compliance review workflows.
- **Backups/DR**: no automated backup strategy configured for the Postgres volume yet.
- **Chroma is a local-filesystem store, not a networked service**: document *metadata* lives in
  Postgres (shared, networked), but the vector index lives in `data/chroma/` on whatever
  filesystem the app process can see. `docker-compose.yml` bind-mounts `./data` into the
  container for exactly this reason — the app (wherever it runs) and the vector store must share
  the same directory, or retrieval silently returns zero hits for documents that exist in Postgres
  but were never embedded on that filesystem. This also means **Chroma won't survive running
  multiple app replicas** behind a load balancer without a shared volume or a real networked
  vector DB (e.g. a hosted Chroma, Qdrant, or pgvector) — fine for one instance, not for scaling out.
