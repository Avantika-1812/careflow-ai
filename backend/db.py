from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS organizations (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        slug TEXT UNIQUE NOT NULL,
        plan TEXT NOT NULL DEFAULT 'trial',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        role TEXT NOT NULL,
        full_name TEXT NOT NULL,
        organization_id INTEGER REFERENCES organizations(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS patients (
        id SERIAL PRIMARY KEY,
        mrn TEXT NOT NULL,
        name TEXT NOT NULL,
        dob TEXT,
        sex TEXT,
        organization_id INTEGER REFERENCES organizations(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS documents (
        id SERIAL PRIMARY KEY,
        patient_id INTEGER REFERENCES patients(id),
        title TEXT NOT NULL,
        doc_type TEXT NOT NULL,
        allowed_roles TEXT NOT NULL,
        source_filename TEXT,
        uploaded_by TEXT,
        chunk_count INTEGER NOT NULL DEFAULT 0,
        full_text TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS conversations (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        patient_id INTEGER REFERENCES patients(id),
        title TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        sources_json TEXT,
        confidence TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS audit_logs (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        username TEXT NOT NULL,
        role TEXT NOT NULL,
        patient_id INTEGER,
        query TEXT NOT NULL,
        retrieved_doc_ids TEXT,
        confidence TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # --- Migration statements (idempotent -- safe to re-run against an existing database
    # created before organizations/multi-tenancy existed) ---
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS organization_id INTEGER REFERENCES organizations(id)",
    "ALTER TABLE patients ADD COLUMN IF NOT EXISTS organization_id INTEGER REFERENCES organizations(id)",
    # MRN used to be globally unique; it must be unique per-organization instead, since two
    # different hospitals legitimately reuse the same MRN numbering scheme.
    "ALTER TABLE patients DROP CONSTRAINT IF EXISTS patients_mrn_key",
    """DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'patients_org_mrn_key'
        ) THEN
            ALTER TABLE patients ADD CONSTRAINT patients_org_mrn_key UNIQUE (organization_id, mrn);
        END IF;
    END $$""",
]

_pool = ConnectionPool(
    conninfo=config.DATABASE_URL,
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row},
    open=False,
)
_pool.open(wait=True, timeout=30)


@contextmanager
def session():
    with _pool.connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db():
    with session() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)


def ensure_default_organization() -> int:
    """Returns the id of a default organization, creating "Demo General Hospital" if none
    exists yet, and backfills organization_id onto any pre-existing users/patients that predate
    multi-tenancy (e.g. this session's already-imported Synthea/MTSamples/Kaggle data)."""
    with session() as conn:
        row = conn.execute("SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()
        if row:
            org_id = row["id"]
        else:
            row = conn.execute(
                "INSERT INTO organizations (name, slug, plan) VALUES (%s, %s, %s) RETURNING id",
                ("Demo General Hospital", "demo-general-hospital", "trial"),
            ).fetchone()
            org_id = row["id"]
        conn.execute("UPDATE users SET organization_id = %s WHERE organization_id IS NULL", (org_id,))
        conn.execute("UPDATE patients SET organization_id = %s WHERE organization_id IS NULL", (org_id,))
    return org_id
