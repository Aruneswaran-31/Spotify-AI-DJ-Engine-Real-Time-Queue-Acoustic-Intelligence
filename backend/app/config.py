"""Runtime configuration, sourced from environment variables (never hardcoded)."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
PROJECT_ROOT = BASE_DIR.parent


def _load_dotenv() -> None:
    """Minimal .env loader so the project needs no extra dependency.

    Real environment variables always win; .env is only a local convenience and
    is git-ignored.
    """
    for candidate in (PROJECT_ROOT / ".env", BASE_DIR / ".env"):
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


class Settings:
    APP_NAME = "AI DJ Studio"
    API_PREFIX = "/api/v1"

    DATABASE_URL: str = os.getenv("DATABASE_URL", f"sqlite:///{(BASE_DIR / 'app.db').as_posix()}")
    DB_ECHO: bool = os.getenv("DB_ECHO", "0") == "1"

    # A generated fallback keeps local dev working; deployments must set JWT_SECRET.
    JWT_SECRET: str = os.getenv("JWT_SECRET") or secrets.token_urlsafe(48)
    JWT_SECRET_IS_EPHEMERAL: bool = not bool(os.getenv("JWT_SECRET"))
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_MINUTES: int = int(os.getenv("ACCESS_TOKEN_MINUTES", "60"))
    REFRESH_TOKEN_DAYS: int = int(os.getenv("REFRESH_TOKEN_DAYS", "7"))

    CORS_ORIGINS: list[str] = [
        o.strip()
        for o in os.getenv(
            "CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000",
        ).split(",")
        if o.strip()
    ]

    AUDIO_DIR: Path = Path(os.getenv("AUDIO_DIR", str(BASE_DIR / "static" / "audio")))
    FRONTEND_DIST: Path = Path(os.getenv("FRONTEND_DIST", str(PROJECT_ROOT / "frontend" / "dist")))

    # Sentiment rolling window used by the aggregation loop.
    SENTIMENT_WINDOW_SECONDS: int = int(os.getenv("SENTIMENT_WINDOW_SECONDS", "120"))
    SENTIMENT_MIN_SAMPLES: int = int(os.getenv("SENTIMENT_MIN_SAMPLES", "3"))

    # Optional hosted, OpenAI-compatible LLM for richer DJ banter. The engine has a
    # deterministic grounded generator as a fallback, so no key is required to run.
    LLM_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    LLM_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://freebuff.com/api/v1/paid")
    LLM_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    LLM_TIMEOUT_SECONDS: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "7"))
    LLM_ENABLED: bool = os.getenv("AI_DJ_USE_LLM", "1") == "1" and bool(os.getenv("OPENAI_API_KEY"))

    # Design targets from the PRD (used by the load-test report, not as limits).
    MAX_PARTICIPANTS_DEFAULT: int = int(os.getenv("MAX_PARTICIPANTS_DEFAULT", "50"))
    DEFAULT_BPM_TOLERANCE_PCT: float = float(os.getenv("DEFAULT_BPM_TOLERANCE_PCT", "8"))


settings = Settings()
