import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _bool(v, default=False):
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    SECRET_KEY: str = ""
    PORT: int = 0
    BASE_URL: str = ""
    JWT_EXPIRES_HOURS: int = 12
    CORS_ORIGINS: list = field(default_factory=list)
    DB_HOST: str = ""
    DB_PORT: int = 0
    DB_USER: str = ""
    DB_PASSWORD: str = ""
    DB_NAME: str = ""
    ARANGO_URL: str = ""
    ARANGO_USER: str = ""
    ARANGO_PASSWORD: str = ""
    ARANGO_DB: str = ""
    UPLOAD_DIR: str = "./uploads"
    GRAPH_DIR: str = "./graphs"
    MAX_UPLOAD_MB: int = 25
    PROCESSING_MODE: str = "async"
    LLM_PROVIDER: str = "none"
    LLM_MODEL: str = ""
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    LLM_TIMEOUT_SECONDS: int = 180
    TESTING: bool = False

    @classmethod
    def from_env(cls, **overrides):
        load_dotenv()
        e = os.environ
        cfg = cls(
            SECRET_KEY=e.get("SECRET_KEY", ""),
            PORT=int(e.get("PORT", 0)),
            BASE_URL=e.get("BASE_URL", "").rstrip("/"),
            JWT_EXPIRES_HOURS=int(e.get("JWT_EXPIRES_HOURS", 12)),
            CORS_ORIGINS=[o.strip() for o in e.get("CORS_ORIGINS", "").split(",") if o.strip()],
            DB_HOST=e.get("DB_HOST", ""),
            DB_PORT=int(e.get("DB_PORT", 0)),
            DB_USER=e.get("DB_USER", ""),
            DB_PASSWORD=e.get("DB_PASSWORD", ""),
            DB_NAME=e.get("DB_NAME", ""),
            ARANGO_URL=e.get("ARANGO_URL", "").rstrip("/"),
            ARANGO_USER=e.get("ARANGO_USER", ""),
            ARANGO_PASSWORD=e.get("ARANGO_PASSWORD", ""),
            ARANGO_DB=e.get("ARANGO_DB", ""),
            UPLOAD_DIR=e.get("UPLOAD_DIR", "./uploads"),
            GRAPH_DIR=e.get("GRAPH_DIR", "./graphs"),
            MAX_UPLOAD_MB=int(e.get("MAX_UPLOAD_MB", 25)),
            PROCESSING_MODE=e.get("PROCESSING_MODE", "async"),
            LLM_PROVIDER=e.get("LLM_PROVIDER", "none").lower(),
            LLM_MODEL=e.get("LLM_MODEL", ""),
            LLM_API_KEY=e.get("LLM_API_KEY", ""),
            LLM_BASE_URL=e.get("LLM_BASE_URL", ""),
            LLM_TIMEOUT_SECONDS=int(e.get("LLM_TIMEOUT_SECONDS", 180)),
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        Path(cfg.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
        Path(cfg.GRAPH_DIR).mkdir(parents=True, exist_ok=True)
        return cfg
