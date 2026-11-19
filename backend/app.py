import io
import json

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
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
    seed.seed_if_empty()


# ---------- Health ----------

@app.get("/health")
def health():
    return {"status": "ok", "llm_configured": config.GROQ_API_KEY is not None}


# ---------- Auth ----------

@app.post("/auth/login")
@limiter.limit(config.LOGIN_RATE_LIMIT)
def login(request: Request, req: LoginRequest):
    user = auth.authenticate(req.username, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    token = auth.create_access_token(user["id"], user["username"], user["role"], user["full_name"])
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "full_name": user["full_name"],
        },
    }


@app.get("/auth/me")
def me(user: dict = Depends(auth.get_current_user)):
    return user


# ---------- Patients ----------

@app.get("/patients")
def list_patients(user: dict = Depends(auth.require_role("clinician", "admin")), q: str | None = None):
    with db.session() as conn:
        if q:
            rows = conn.execute(
                "SELECT * FROM patients WHERE name ILIKE %s OR mrn ILIKE %s ORDER BY name",
                (f"%{q}%", f"%{q}%"),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM patients ORDER BY name").fetchall()
    return [dict(r) for r in rows]


@app.post("/patients")
def create_patient(req: PatientCreate, user: dict = Depends(auth.require_role("admin"))):
    with db.session() as conn:
        try:
            row = conn.execute(
                "INSERT INTO patients (mrn, name, dob, sex) VALUES (%s, %s, %s, %s) RETURNING *",
                (req.mrn, req.name, req.dob, req.sex),
            ).fetchone()
        except Exception:
            raise HTTPException(status_code=400, detail="A patient with that MRN already exists.")
    return dict(row)


# ---------- Documents ----------

@app.get("/documents")
def list_documents(user: dict = Depends(auth.get_current_user), patient_id: int | None = None):
    with db.session() as conn:
        if patient_id is not None:
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
            exists = conn.execute("SELECT id FROM patients WHERE id = %s", (patient_id,)).fetchone()
            if exists is None:
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
    rag.delete_document(document_id)
    return {"deleted": True}


# ---------- Conversations ----------

@app.post("/conversations")
def create_conversation(req: ConversationCreate, user: dict = Depends(auth.get_current_user)):
    if req.patient_id is not None and user["role"] not in ("clinician", "admin"):
        raise HTTPException(status_code=403, detail="Your role cannot access patient-specific conversations.")
    with db.session() as conn:
        if req.patient_id is not None:
            exists = conn.execute("SELECT id, name FROM patients WHERE id = %s", (req.patient_id,)).fetchone()
            if exists is None:
                raise HTTPException(status_code=400, detail="Patient not found.")
            title = req.title or f"Chat - {exists['name']}"
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

        result = rag.generate_answer(req.query, user["role"], conv["patient_id"], history)

        conn.execute(
            """INSERT INTO messages (conversation_id, role, content, sources_json, confidence)
               VALUES (%s, 'assistant', %s, %s, %s)""",
            (conversation_id, result["answer"], json.dumps(result["sources"]), result["confidence"]),
        )

        retrieved_doc_ids = [s["document_id"] for s in result["sources"]]
        conn.execute(
            """INSERT INTO audit_logs (user_id, username, role, patient_id, query, retrieved_doc_ids, confidence)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                user["id"], user["username"], user["role"], conv["patient_id"],
                req.query, json.dumps(retrieved_doc_ids), result["confidence"],
            ),
        )

    return result


# ---------- Admin ----------

@app.get("/admin/audit-logs")
def audit_logs(user: dict = Depends(auth.require_role("admin")), limit: int = 100, offset: int = 0):
    with db.session() as conn:
        rows = conn.execute(
            """SELECT a.*, p.name AS patient_name FROM audit_logs a
               LEFT JOIN patients p ON p.id = a.patient_id
               ORDER BY a.created_at DESC LIMIT %s OFFSET %s""",
            (limit, offset),
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
            "SELECT id, username, role, full_name, created_at FROM users ORDER BY created_at DESC"
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
                """INSERT INTO users (username, password_hash, salt, role, full_name)
                   VALUES (%s, %s, %s, %s, %s)
                   RETURNING id, username, role, full_name, created_at""",
                (req.username.strip(), password_hash, salt, req.role, req.full_name.strip()),
            ).fetchone()
        except Exception:
            raise HTTPException(status_code=400, detail="A user with that username already exists.")
    return dict(row)


app.mount("/", NoCacheStaticFiles(directory=str(config.ROOT_DIR / "frontend"), html=True), name="frontend")
