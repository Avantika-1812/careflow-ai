
# CareFlow AI — Hospital Workflow Assistant

A RAG-based assistant that retrieves and summarizes information from authorized hospital documents using natural-language queries, with login/role-based access control, per-patient scoping, source citations, a grounding/confidence check on every answer, and a full audit trail.

## 🎥 Project Demo Video

Watch the **5-minute CareFlow AI project demonstration** to see how the application works in practice.

**▶️ [Watch the CareFlow AI Demo Video](https://drive.google.com/file/d/1PKQRJ3bZkrW4_EtybHB5yd6had0T4fU6/view?usp=sharing)**

The video provides a walkthrough of the running application, including the user interface, patient/document features, and AI-powered question-answering experience.

> The demonstration uses synthetic patient data only. CareFlow AI is an information-support tool and is not intended to provide diagnoses or replace professional clinical judgment.

## ⚠️ Before This Touches Real Patient Data

**This codebase is not yet cleared for real PHI.** Good engineering (what's in this repository) is necessary but not sufficient. Real patient data additionally requires, at minimum:

1. **A signed Business Associate Agreement (BAA)** with every vendor in the data path. Groq's free tier (the default LLM here) does not offer one. You would need Azure OpenAI, AWS Bedrock, or another HIPAA-eligible provider under a BAA before processing real PHI.
2. **Institutional privacy/compliance approval.** This is an organizational and legal requirement, not something a codebase can satisfy on its own.
3. **A security review** of the specific deployment, including network exposure, secrets management, backup/retention policy, and incident response.
4. **TLS everywhere and encryption at rest.** This repository runs plain HTTP locally. A real deployment needs HTTPS/TLS termination and encryption for the database and backups.

Until all of that is in place, treat every patient in this system as synthetic.

## 💰 Cost

The LLM is [Groq](https://console.groq.com), running an open model (default: `openai/gpt-oss-20b`) on its free tier.

The free tier is suitable for development and demonstration. A production deployment involving real PHI would require a suitable provider and an appropriate signed BAA.

## ✨ Features

- **Multi-tenant architecture:** Hospitals can register an organization through the signup flow. Each organization gets isolated patients, users, conversations, and audit logs.
- **Login + roles:** JWT-based authentication with role-gated access for clinicians, admins, and staff.
- **User management:** Organization admins can create accounts and assign roles from the Users tab.
- **Patient-scoped RAG:** Every conversation is scoped to one patient or to shared hospital guidelines.
- **Role-aware retrieval:** Documents are filtered according to the user's role and patient context.
- **Conversation memory:** Multi-turn chat; follow-up questions use prior turns as context.
- **Document upload:** Clinicians/admins can upload `.txt` and `.pdf` files per patient or as a shared guideline.
- **Document viewer:** Users can click cited sources to read the original document.
- **Source citations:** Responses identify the retrieved documents used to generate an answer.
- **Grounding/confidence check:** A deterministic high/medium/low badge estimates how much of an answer's wording is traceable to retrieved context.
- **Audit log:** Records each query, user, role, patient context, and retrieved documents.
- **Analytics dashboard:** Admin overview includes query volume, confidence distribution, queries by role, most-queried patients, and document-library composition.
- **Streaming responses:** Answers appear progressively using Server-Sent Events (SSE), with Markdown rendered as the response streams.
- **Security controls:** Includes rate limiting, security headers, upload-size limits, parameterized SQL, and production configuration checks.

## 🧰 Technology Stack

| Layer | Technology |
|---|---|
| Frontend | HTML, CSS, Vanilla JavaScript |
| Backend | Python, FastAPI |
| Relational Database | PostgreSQL |
| Vector Database | ChromaDB |
| LLM Provider | Groq |
| Retrieval Approach | Retrieval-Augmented Generation (RAG) |
| Authentication | JWT, PBKDF2 password hashing |
| Containerization | Docker, Docker Compose |

## 🏗️ Architecture

```text
documents/              Seed documents (synthetic patient records, labs, prescriptions, guidelines)

scripts/
  import_synthea.py     Imports Synthea-generated synthetic patients

backend/
  config.py             Application settings: DB URL, JWT, rate limits, CORS
  db.py                 PostgreSQL schema and pooled connections
  auth.py               Password hashing, JWT issue/verify, role-gating
  rag.py                Chunking, Chroma indexing, scoped retrieval, Groq generation
  seed.py               Demo users, patients, and sample documents
  app.py                FastAPI application and API endpoints

frontend/
  index.html            Application interface
  style.css             Frontend styling
  app.js                Frontend functionality

data/                   Chroma vector store and runtime data (gitignored)
docker-compose.yml      PostgreSQL and application services
Dockerfile              Application container image
```

## 🔐 How Access Control Works

Every document has an `allowed_roles` field (for example, `clinician,admin`) and an optional `patient_id`.

- `NULL` patient ID represents a shared document, such as a hospital guideline.
- Retrieval checks whether the user's role is allowed to access the document.
- Retrieval also checks whether the document belongs to the selected patient or is shared.
- Staff conversations cannot access patient-specific information.

This ensures that retrieval is filtered by both user role and patient context rather than relying only on frontend visibility.

## 🏢 How Multi-Tenancy Works

Every organization has an `organizations` record. Users and patients carry an `organization_id`.

The `_get_org_patient()` function in `app.py` checks whether a requested patient belongs to the current user's organization. If the patient does not belong to that organization, the request returns a 404 response.

This organization-level check is used by patient-related endpoints, including conversation creation, document upload/listing, and patient lookup.

The signup flow creates a new organization and its first admin account. The organization admin can then create additional user accounts and assign roles.

## 🎯 How the Grounding Check Works

After the LLM generates an answer, `rag.score_grounding()` compares significant words in the answer with words in the retrieved context.

- Higher overlap results in a higher confidence indicator.
- Lower overlap results in a lower confidence indicator.

This is a deterministic lexical-overlap heuristic and does not require an additional API call.

**Important:** The confidence indicator is not a guarantee of correctness and does not replace professional review.

## 🛡️ Security Hardening

- **Rate limiting:** Login requests have a separate limit (`LOGIN_RATE_LIMIT`, default 10/min) from general API traffic (`API_RATE_LIMIT`, default 120/min).
- **Security headers:** Includes `X-Content-Type-Options`, `X-Frame-Options`, and `Referrer-Policy`.
- **CORS closed by default:** `ALLOWED_ORIGINS` is empty unless explicitly configured.
- **Upload size cap:** Uploads are limited to 20 MB using `MAX_UPLOAD_BYTES`.
- **Production-gated JWT secret:** When `CAREFLOW_ENV=production`, the app requires an explicitly configured `JWT_SECRET`.
- **Parameterized SQL:** Database queries use parameterized statements rather than string-built SQL.

## 🚀 Setup

### Prerequisites

- Python 3.10 or later
- Docker Desktop
- Git
- A Groq API key

### Option A: Docker Compose (Recommended)

Clone the repository:

```bash
git clone https://github.com/Avantika-1812/careflow-ai.git
cd careflow-ai
```

Create the environment file:

```bash
cp .env.example .env
```

Open `.env` and configure your `GROQ_API_KEY`.

You can obtain an API key from:

https://console.groq.com/keys

Start the application:

```bash
docker compose up --build
```

Open the application in your browser:

```text
http://localhost:8000
```

**On Windows:** Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) with the WSL2 backend and [Git for Windows](https://git-scm.com/download/win). In PowerShell, use:

```powershell
Copy-Item .env.example .env
```

Then edit `.env`, configure your API key, and run:

```powershell
docker compose up --build
```

Make sure Docker Desktop is running before executing Docker commands.

### Option B: Run Locally Against a PostgreSQL Container

```bash
git clone https://github.com/Avantika-1812/careflow-ai.git
cd careflow-ai

python -m venv venv
```

Activate the virtual environment.

**Windows PowerShell:**

```powershell
.\venv\Scripts\Activate.ps1
```

**Linux/macOS:**

```bash
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create the environment file:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Configure `GROQ_API_KEY` in `.env`.

Start PostgreSQL:

```bash
docker compose up -d db
```

Start the backend:

```bash
uvicorn backend.app:app --reload --port 8000
```

Open:

```text
http://localhost:8000
```

On first run, the application initializes the database schema and seeds demo users, patients, and documents.

## 👤 Demo Accounts

The following accounts are available in the seeded demo environment:

| Role | Username | Password |
|---|---|---|
| Clinician | `dr.patel` | `clinician123` |
| Admin | `admin` | `admin123` |
| Staff | `front.desk` | `staff123` |

> **Security warning:** These are demonstration credentials. Change or remove them before any real deployment.

## 📊 Data Sources

No real patient information is included in this system. The project uses synthetic and publicly available sample datasets.

### 1. Hand-Written Seed Patients

Located in `backend/seed.py` and `documents/`.

Includes three fictional patients with sample records, laboratory reports, and prescriptions for quick manual testing.

### 2. Synthea-Generated Patients

[Synthea](https://github.com/synthetichealth/synthea) is an open-source tool that generates realistic but entirely fictional patient populations.

It can generate demographics, encounters, conditions, medications, and observations.

Generate synthetic patients (requires Java 17 or later):

```bash
curl -sL -o synthea.jar \
  https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar

java -jar synthea.jar --exporter.csv.export=true --exporter.fhir.export=false -p 25 Massachusetts
```

Import into CareFlow AI:

```bash
python -m scripts.import_synthea ./output/csv
```

### 3. MTSamples Clinical Reference Library

[MTSamples](https://www.mtsamples.com/) provides publicly available de-identified medical transcription sample reports across multiple specialties.

The collection includes examples such as discharge summaries, radiology reports, SOAP notes, and other clinical narratives.

These are imported as shared reference documents rather than being attached to individual patients.

### 4. Kaggle Healthcare Dataset

The [Healthcare Dataset](https://www.kaggle.com/datasets/prasad22/healthcare-dataset) contains 55,500 synthetic hospital admission records.

It can be useful for demonstrating larger datasets and patient-management workflows.

Download the CSV:

```bash
curl -sL -o data/kaggle_cache/healthcare_dataset.csv \
  https://raw.githubusercontent.com/imranbdcse/healthcaredatasets/DataScience/healthcare_dataset.csv
```

Import a smaller dataset for a quick demonstration:

```bash
python -m scripts.import_kaggle_healthcare --limit 5000
```

Import the full dataset:

```bash
python -m scripts.import_kaggle_healthcare
```

**Note:** If the application is already running when you execute an import script, restart it afterwards so the application can reopen its Chroma vector-store connection.

## ➕ Adding Your Own Data

- **Upload documents:** Log in as a clinician or admin, select a patient, open Documents, and use the Upload Document option. Supported formats are `.txt` and `.pdf`.
- **Add users:** Organization admins can create users from the Users tab by providing a name, username, temporary password, and role.
- **Add patients:** Admins can add patients through the patient interface or using the `POST /patients` endpoint.
- **Bulk patient import:** Use the Synthea import workflow described above or update `backend/seed.py`.

## ⚠️ Known Limitations / What's Still Missing Before Production

This project is a demonstration and development implementation. The following items require additional work before production use:

- **No billing/plans enforcement:** Organizations have a `plan` column that defaults to `'trial'`, but usage limits and payment processing are not enforced.
- **Globally unique usernames:** Usernames are globally unique rather than unique within each organization. Two organizations cannot currently have the same username.
- **No cross-tenant platform-admin panel:** There is no platform-level interface to manage all organizations or suspend tenants.
- **No email verification or password reset:** These workflows need to be implemented for production use.
- **Authentication enhancements:** PBKDF2 and JWT provide a development authentication baseline. A production deployment should consider SSO, MFA, and appropriate identity-provider integration.
- **LLM provider:** Groq's free tier does not provide a BAA. A suitable HIPAA-eligible provider under a signed BAA is required before processing real PHI.
- **TLS:** The application serves plain HTTP locally. Production requires HTTPS/TLS termination through a properly configured reverse proxy or load balancer.
- **Encryption at rest:** PostgreSQL and Chroma data are not encrypted at the storage layer by this repository. Infrastructure-level encryption must be configured.
- **Embeddings:** Chroma's default embedding function is a baseline. Production may require a domain-tuned or higher-quality embedding model.
- **Document ingestion:** Upload supports `.txt` and `.pdf` text extraction. OCR for scanned records and HL7/FHIR/EHR-export parsing are not implemented.
- **Hallucination mitigation:** The system prompt and grounding-overlap heuristic reduce but do not eliminate hallucination risk. The confidence indicator is not a semantic or model-verified correctness check.
- **Audit log retention/export:** Audit records are stored in PostgreSQL, but retention policies and export tooling are not yet implemented.
- **Backups and disaster recovery:** An automated backup and recovery strategy for PostgreSQL volumes still needs to be configured.
- **Chroma scaling:** Chroma currently uses a local-filesystem store in `data/chroma/`. The app and vector store must share the same persistent directory. Running multiple application replicas requires a shared volume or a networked vector database such as hosted Chroma, Qdrant, or pgvector.

## 🔮 Future Improvements

Potential future improvements include:

- Integration with HL7/FHIR-compatible healthcare systems.
- OCR support for scanned medical documents.
- More advanced clinical embedding models.
- Enhanced authentication with MFA and SSO.
- Automated audit-log retention and export.
- Database backup and disaster-recovery workflows.
- Production-ready monitoring and deployment infrastructure.
- Scalable networked vector database integration.

> CareFlow AI is an information-support and academic demonstration project. It is not a diagnostic system, and clinical decisions must remain with qualified healthcare professionals.
