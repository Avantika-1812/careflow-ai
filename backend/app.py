import io
import json
import re
import secrets

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pypdf import PdfReader
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from . import auth, config, db, rag, seed


class NoCacheStaticFiles(StaticFiles):
    """Serves the frontend with Cache-Control: no-cache so edits always show up on reload
    instead of being served from a stale browser cache (bit us during development)."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response

limiter = Limiter(key_func=get_remote_address, default_limits=[config.API_RATE_LIMIT])

app = FastAPI(title="CareFlow AI - Hospital Workflow Assistant")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if config.ENV == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ---------- Pydantic schemas ----------

class LoginRequest(BaseModel):
    username: str
    password: str


class SignupRequest(BaseModel):
    organization_name: str
    full_name: str
    username: str
    password: str


class PatientCreate(BaseModel):
    mrn: str
    name: str
    dob: str | None = None
    sex: str | None = None


class ConversationCreate(BaseModel):
    patient_id: int | None = None
    title: str | None = None


class UserCreate(BaseModel):
    username: str
    password: str
    full_name: str
    role: str


class MessageCreate(BaseModel):
    query: str


# ---------- Startup ----------

@app.on_event("startup")
def startup_event():
    db.init_db()
    default_org_id = db.ensure_default_organization()
    seed.seed_if_empty(default_org_id)
    rag.heal_vector_store_if_empty()


# ---------- Health ----------

@app.get("/health")
def health():
    return {"status": "ok", "llm_configured": config.GROQ_API_KEY is not None}


# ---------- Auth ----------

def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "org"


@app.post("/signup")
@limiter.limit(config.LOGIN_RATE_LIMIT)
def signup(request: Request, req: SignupRequest):
    """Self-serve: creates a brand-new organization plus its first admin account. This is
    the thing that makes the product actually sellable rather than something only I can
    provision -- a hospital finds the site, signs up, and is isolated from every other
    hospital on the platform from the first row written."""
    org_name = req.organization_name.strip()
    full_name = req.full_name.strip()
    username = req.username.strip()
    if not org_name or not full_name or not username:
        raise HTTPException(status_code=400, detail="Organization name, your name, and a username are required.")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    base_slug = _slugify(org_name)
    password_hash, salt = auth.hash_password(req.password)

    with db.session() as conn:
        slug = base_slug
        for attempt in range(6):
            exists = conn.execute("SELECT id FROM organizations WHERE slug = %s", (slug,)).fetchone()
            if not exists:
                break
            slug = f"{base_slug}-{secrets.token_hex(2)}"
        else:
            raise HTTPException(status_code=500, detail="Could not allocate a unique organization slug.")

        org_row = conn.execute(
            "INSERT INTO organizations (name, slug, plan) VALUES (%s, %s, 'trial') RETURNING id, name",
            (org_name, slug),
        ).fetchone()

        try:
            user_row = conn.execute(
                """INSERT INTO users (username, password_hash, salt, role, full_name, organization_id)
                   VALUES (%s, %s, %s, 'admin', %s, %s) RETURNING id""",
                (username, password_hash, salt, full_name, org_row["id"]),
            ).fetchone()
        except Exception:
            raise HTTPException(status_code=400, detail="That username is already taken.")

    token = auth.create_access_token(user_row["id"], username, "admin", full_name, org_row["id"], org_row["name"])
    return {
        "token": token,
        "user": {
            "id": user_row["id"], "username": username, "role": "admin", "full_name": full_name,
            "organization_id": org_row["id"], "organization_name": org_row["name"],
        },
    }


@app.post("/auth/login")
@limiter.limit(config.LOGIN_RATE_LIMIT)
def login(request: Request, req: LoginRequest):
    user = auth.authenticate(req.username, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    token = auth.create_access_token(
        user["id"], user["username"], user["role"], user["full_name"],
        user["organization_id"], user["organization_name"],
    )
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "full_name": user["full_name"],
            "organization_id": user["organization_id"],
            "organization_name": user["organization_name"],
        },
    }


@app.get("/auth/me")
def me(user: dict = Depends(auth.get_current_user)):
    return user


# ---------- Patients ----------

PATIENT_LIST_LIMIT = 100


@app.get("/patients")
def list_patients(user: dict = Depends(auth.require_role("clinician", "admin")), q: str | None = None):
    """Capped + searched, not a full dump (see git history for why), and always scoped to the
    caller's own organization -- a patient from one hospital must never appear in another
    hospital's list, full stop."""
    org_id = user["organization_id"]
    with db.session() as conn:
        if q:
            where = "WHERE organization_id = %s AND (name ILIKE %s OR mrn ILIKE %s)"
            params = (org_id, f"%{q}%", f"%{q}%")
        else:
            where = "WHERE organization_id = %s"
            params = (org_id,)
        total = conn.execute(f"SELECT count(*) AS c FROM patients {where}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM patients {where} ORDER BY name LIMIT %s", params + (PATIENT_LIST_LIMIT,)
        ).fetchall()
    return {"items": [dict(r) for r in rows], "total": total}


@app.post("/patients")
def create_patient(req: PatientCreate, user: dict = Depends(auth.require_role("admin"))):
    with db.session() as conn:
        try:
            row = conn.execute(
                "INSERT INTO patients (mrn, name, dob, sex, organization_id) VALUES (%s, %s, %s, %s, %s) RETURNING *",
                (req.mrn, req.name, req.dob, req.sex, user["organization_id"]),
            ).fetchone()
        except Exception:
            raise HTTPException(status_code=400, detail="A patient with that MRN already exists in your organization.")
    return dict(row)


def _get_org_patient(conn, patient_id: int, organization_id: int):
    """Fetch a patient only if it belongs to the given organization -- the core cross-tenant
    guard. Returns None (treated as 404/400 by callers) rather than ever returning another
    organization's patient."""
    return conn.execute(
        "SELECT * FROM patients WHERE id = %s AND organization_id = %s", (patient_id, organization_id)
    ).fetchone()


# ---------- Documents ----------

@app.get("/documents")
def list_documents(user: dict = Depends(auth.get_current_user), patient_id: int | None = None):
    with db.session() as conn:
        if patient_id is not None:
            if _get_org_patient(conn, patient_id, user["organization_id"]) is None:
                raise HTTPException(status_code=404, detail="Patient not found.")
            rows = conn.execute(
                "SELECT * FROM documents WHERE patient_id = %s OR patient_id IS NULL ORDER BY created_at DESC",
                (patient_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM documents WHERE patient_id IS NULL ORDER BY created_at DESC"
            ).fetchall()
    visible = [dict(r) for r in rows if user["role"] in r["allowed_roles"].split(",")]
    for d in visible:
        d.pop("full_text", None)
    return visible


@app.get("/documents/{document_id}")
def get_document(document_id: int, user: dict = Depends(auth.get_current_user)):
    with db.session() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = %s", (document_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found.")
        if row["patient_id"] is not None:
            if _get_org_patient(conn, row["patient_id"], user["organization_id"]) is None:
                raise HTTPException(status_code=404, detail="Document not found.")
    if user["role"] not in row["allowed_roles"].split(","):
        raise HTTPException(status_code=403, detail="You don't have access to this document.")
    return dict(row)


@app.post("/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    doc_type: str = Form(...),
    allowed_roles: str = Form("clinician,admin"),
    patient_id: int | None = Form(None),
    user: dict = Depends(auth.require_role("clinician", "admin")),
):
    raw = await file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(raw) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds the {config.MAX_UPLOAD_BYTES // (1024*1024)}MB upload limit.")

    filename = file.filename or "upload"
    if filename.lower().endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(raw))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            raise HTTPException(status_code=400, detail="Could not parse PDF file.")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Unsupported file encoding; upload a .txt or .pdf file.")

    if not text.strip():
        raise HTTPException(status_code=400, detail="No extractable text found in the uploaded file.")

    roles = [r.strip() for r in allowed_roles.split(",") if r.strip() in config.VALID_ROLES]
    if not roles:
        roles = ["clinician", "admin"]

    with db.session() as conn:
        if patient_id is not None:
            if _get_org_patient(conn, patient_id, user["organization_id"]) is None:
                raise HTTPException(status_code=400, detail="Patient not found.")
        result = rag.ingest_document(
            conn=conn,
            patient_db_id=patient_id,
            title=title,
            doc_type=doc_type,
            allowed_roles=roles,
            text=text,
            source_filename=filename,
            uploaded_by=user["username"],
        )
    return result


@app.delete("/documents/{document_id}")
def delete_document(document_id: int, user: dict = Depends(auth.require_role("admin"))):
    with db.session() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = %s", (document_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found.")
        if row["patient_id"] is None:
            # Shared platform content (e.g. the clinical reference library) isn't owned by any
            # one organization -- no tenant admin should be able to delete it for everyone else.
            raise HTTPException(status_code=403, detail="Shared reference documents cannot be deleted here.")
        if _get_org_patient(conn, row["patient_id"], user["organization_id"]) is None:
            raise HTTPException(status_code=404, detail="Document not found.")
    rag.delete_document(document_id)
    return {"deleted": True}


# ---------- Conversations ----------

@app.post("/conversations")
def create_conversation(req: ConversationCreate, user: dict = Depends(auth.get_current_user)):
    if req.patient_id is not None and user["role"] not in ("clinician", "admin"):
        raise HTTPException(status_code=403, detail="Your role cannot access patient-specific conversations.")
    with db.session() as conn:
        if req.patient_id is not None:
            patient = _get_org_patient(conn, req.patient_id, user["organization_id"])
            if patient is None:
                raise HTTPException(status_code=400, detail="Patient not found.")
            title = req.title or f"Chat - {patient['name']}"
        else:
            title = req.title or "General guideline chat"
        row = conn.execute(
            "INSERT INTO conversations (user_id, patient_id, title) VALUES (%s, %s, %s) RETURNING *",
            (user["id"], req.patient_id, title),
        ).fetchone()
    return dict(row)


@app.get("/conversations")
def list_conversations(user: dict = Depends(auth.get_current_user)):
    with db.session() as conn:
        rows = conn.execute(
            """SELECT c.*, p.name AS patient_name FROM conversations c
               LEFT JOIN patients p ON p.id = c.patient_id
               WHERE c.user_id = %s ORDER BY c.created_at DESC""",
            (user["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


def _get_owned_conversation(conn, conversation_id: int, user: dict):
    row = conn.execute("SELECT * FROM conversations WHERE id = %s", (conversation_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if row["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="This conversation belongs to another user.")
    return row


@app.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, user: dict = Depends(auth.get_current_user)):
    with db.session() as conn:
        _get_owned_conversation(conn, conversation_id, user)
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = %s ORDER BY id ASC", (conversation_id,)
        ).fetchall()
    out = []
    for r in rows:
        m = dict(r)
        m["sources"] = json.loads(m.pop("sources_json") or "[]")
        out.append(m)
    return out


@app.post("/conversations/{conversation_id}/messages")
def post_message(conversation_id: int, req: MessageCreate, user: dict = Depends(auth.get_current_user)):
    """Streams the assistant's answer token-by-token over Server-Sent Events. The user's
    message is persisted immediately; the assistant's message + audit log entry are persisted
    once streaming completes, inside the same generator (not a separate request).

    Note on tenant isolation: retrieval itself doesn't need to know about organizations --
    conv["patient_id"] was already verified to belong to the caller's org back when the
    conversation was created (create_conversation), and shared platform documents (patient_id
    IS NULL) are intentionally available to every organization. So the only places that need
    an explicit organization_id check are wherever a patient_id is first accepted from the
    client (conversation creation, document upload/listing) -- not every downstream use of it.
    """
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query must not be empty.")

    with db.session() as conn:
        conv = _get_owned_conversation(conn, conversation_id, user)
        history_rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
        history = [{"role": r["role"], "content": r["content"]} for r in history_rows]

        conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, 'user', %s)",
            (conversation_id, req.query),
        )

    def event_stream():
        sources = []
        for event in rag.generate_answer_stream(req.query, user["role"], conv["patient_id"], history):
            if event["type"] == "sources":
                sources = event["sources"]
            elif event["type"] == "done":
                with db.session() as conn2:
                    conn2.execute(
                        """INSERT INTO messages (conversation_id, role, content, sources_json, confidence)
                           VALUES (%s, 'assistant', %s, %s, %s)""",
                        (conversation_id, event["answer"], json.dumps(sources), event["confidence"]),
                    )
                    retrieved_doc_ids = [s["document_id"] for s in sources]
                    conn2.execute(
                        """INSERT INTO audit_logs (user_id, username, role, patient_id, query, retrieved_doc_ids, confidence)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (
                            user["id"], user["username"], user["role"], conv["patient_id"],
                            req.query, json.dumps(retrieved_doc_ids), event["confidence"],
                        ),
                    )
                event = {**event, "sources": sources}
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------- Admin ----------
# Every query below is scoped to the caller's organization_id -- an admin at one hospital
# must only ever see their own hospital's users, patients, and query history.

@app.get("/admin/analytics")
def analytics(user: dict = Depends(auth.require_role("admin"))):
    org_id = user["organization_id"]
    with db.session() as conn:
        totals = conn.execute(
            """SELECT
                 (SELECT count(*) FROM audit_logs a JOIN users u ON u.id = a.user_id WHERE u.organization_id = %s) AS total_queries,
                 (SELECT count(*) FROM patients WHERE organization_id = %s) AS total_patients,
                 (SELECT count(*) FROM documents d LEFT JOIN patients p ON p.id = d.patient_id
                    WHERE p.organization_id = %s OR d.patient_id IS NULL) AS total_documents,
                 (SELECT count(DISTINCT a.user_id) FROM audit_logs a JOIN users u ON u.id = a.user_id WHERE u.organization_id = %s) AS active_users""",
            (org_id, org_id, org_id, org_id),
        ).fetchone()

        queries_per_day = conn.execute(
            """SELECT date_trunc('day', a.created_at)::date AS day, count(*) AS n
               FROM audit_logs a JOIN users u ON u.id = a.user_id
               WHERE u.organization_id = %s AND a.created_at >= now() - interval '14 days'
               GROUP BY 1 ORDER BY 1""",
            (org_id,),
        ).fetchall()

        confidence_breakdown = conn.execute(
            """SELECT coalesce(a.confidence, 'unknown') AS confidence, count(*) AS n
               FROM audit_logs a JOIN users u ON u.id = a.user_id
               WHERE u.organization_id = %s GROUP BY 1""",
            (org_id,),
        ).fetchall()

        role_breakdown = conn.execute(
            """SELECT a.role, count(*) AS n FROM audit_logs a JOIN users u ON u.id = a.user_id
               WHERE u.organization_id = %s GROUP BY 1 ORDER BY 2 DESC""",
            (org_id,),
        ).fetchall()

        top_patients = conn.execute(
            """SELECT p.name, count(*) AS n FROM audit_logs a
               JOIN users u ON u.id = a.user_id
               JOIN patients p ON p.id = a.patient_id
               WHERE u.organization_id = %s
               GROUP BY p.name ORDER BY n DESC LIMIT 6""",
            (org_id,),
        ).fetchall()

        doc_type_breakdown = conn.execute(
            """SELECT d.doc_type, count(*) AS n FROM documents d LEFT JOIN patients p ON p.id = d.patient_id
               WHERE p.organization_id = %s OR d.patient_id IS NULL
               GROUP BY 1 ORDER BY 2 DESC""",
            (org_id,),
        ).fetchall()

    return {
        "totals": dict(totals),
        "queries_per_day": [{"day": str(r["day"]), "n": r["n"]} for r in queries_per_day],
        "confidence_breakdown": [dict(r) for r in confidence_breakdown],
        "role_breakdown": [dict(r) for r in role_breakdown],
        "top_patients": [dict(r) for r in top_patients],
        "doc_type_breakdown": [dict(r) for r in doc_type_breakdown],
    }


@app.get("/admin/audit-logs")
def audit_logs(user: dict = Depends(auth.require_role("admin")), limit: int = 100, offset: int = 0):
    with db.session() as conn:
        rows = conn.execute(
            """SELECT a.*, p.name AS patient_name FROM audit_logs a
               JOIN users u ON u.id = a.user_id
               LEFT JOIN patients p ON p.id = a.patient_id
               WHERE u.organization_id = %s
               ORDER BY a.created_at DESC LIMIT %s OFFSET %s""",
            (user["organization_id"], limit, offset),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["retrieved_doc_ids"] = json.loads(d["retrieved_doc_ids"] or "[]")
        out.append(d)
    return out


@app.get("/admin/users")
def list_users(user: dict = Depends(auth.require_role("admin"))):
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id, username, role, full_name, created_at FROM users WHERE organization_id = %s ORDER BY created_at DESC",
            (user["organization_id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/admin/users")
def create_user(req: UserCreate, user: dict = Depends(auth.require_role("admin"))):
    if req.role not in config.VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of {sorted(config.VALID_ROLES)}.")
    if not req.username.strip() or not req.full_name.strip():
        raise HTTPException(status_code=400, detail="Username and full name are required.")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    password_hash, salt = auth.hash_password(req.password)
    with db.session() as conn:
        try:
            row = conn.execute(
                """INSERT INTO users (username, password_hash, salt, role, full_name, organization_id)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING id, username, role, full_name, created_at""",
                (req.username.strip(), password_hash, salt, req.role, req.full_name.strip(), user["organization_id"]),
            ).fetchone()
        except Exception:
            raise HTTPException(status_code=400, detail="A user with that username already exists.")
    return dict(row)


app.mount("/", NoCacheStaticFiles(directory=str(config.ROOT_DIR / "frontend"), html=True), name="frontend")
