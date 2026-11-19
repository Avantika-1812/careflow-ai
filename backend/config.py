import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent
DOCUMENTS_DIR = ROOT_DIR / "documents"
UPLOADS_DIR = ROOT_DIR / "data" / "uploads"
CHROMA_DIR = ROOT_DIR / "data" / "chroma"

ENV = os.environ.get("CAREFLOW_ENV", "development")  # "development" | "production"

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://careflow:careflow@localhost:5433/careflow")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
MODEL_NAME = os.environ.get("CAREFLOW_MODEL", "openai/gpt-oss-20b")

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
TOP_K = 6
HISTORY_TURNS = 4  # prior user/assistant turn pairs included as conversation context

VALID_ROLES = {"clinician", "admin", "staff"}

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB

# CORS: comma-separated list of allowed origins. Empty by default -- the frontend is
# served from the same origin as the API, so cross-origin requests aren't needed unless
# you split frontend/backend into separate deployments.
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]

LOGIN_RATE_LIMIT = os.environ.get("LOGIN_RATE_LIMIT", "10/minute")
API_RATE_LIMIT = os.environ.get("API_RATE_LIMIT", "120/minute")


def _load_or_create_jwt_secret() -> str:
    env_secret = os.environ.get("JWT_SECRET")
    if env_secret:
        return env_secret
    if ENV == "production":
        raise RuntimeError(
            "JWT_SECRET must be set via environment in production (CAREFLOW_ENV=production). "
            "Refusing to auto-generate a session secret for a production deployment."
        )
    secret_file = ROOT_DIR / "data" / ".jwt_secret"
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    if secret_file.exists():
        return secret_file.read_text().strip()
    secret = secrets.token_hex(32)
    secret_file.write_text(secret)
    return secret


# Demo-only secret: persisted to data/.jwt_secret (gitignored) so sessions survive
# restarts in development. Production refuses to boot without JWT_SECRET set explicitly.
JWT_SECRET = _load_or_create_jwt_secret()
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 12

# Deterministic grounding heuristic (not model-based) used to flag answers whose wording
# isn't well supported by the retrieved context. See rag.py:score_grounding.
GROUNDING_HIGH_THRESHOLD = 0.6
GROUNDING_MEDIUM_THRESHOLD = 0.35
