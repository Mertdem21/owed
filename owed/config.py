"""Settings loaded from .env. Every sponsor integration is switched on only when its key is present."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RUNTIME = DATA / "runtime"

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover - dotenv is a core dependency
    pass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass
class Settings:
    # Sponsor keys
    senso_api_key: str = field(default_factory=lambda: _env("SENSO_API_KEY"))
    senso_base_url: str = field(default_factory=lambda: _env("SENSO_BASE_URL", "https://apiv2.senso.ai/api/v1"))
    sie_api_key: str = field(default_factory=lambda: _env("SIE_API_KEY"))
    sie_base_url: str = field(default_factory=lambda: _env("SIE_BASE_URL", "https://api.superlinked.com"))
    sie_extract_model: str = field(default_factory=lambda: _env("SIE_EXTRACT_MODEL", "urchade/gliner_multi-v2.1"))
    sie_rerank_model: str = field(default_factory=lambda: _env("SIE_RERANK_MODEL", "Qwen/Qwen3-Reranker-0.6B"))
    arcee_api_key: str = field(default_factory=lambda: _env("ARCEE_API_KEY"))
    arcee_base_url: str = field(default_factory=lambda: _env("ARCEE_BASE_URL", "https://api.arcee.ai/api/v1"))
    arcee_model_small: str = field(default_factory=lambda: _env("ARCEE_MODEL_SMALL"))
    arcee_model_large: str = field(default_factory=lambda: _env("ARCEE_MODEL_LARGE"))
    amplitude_api_key: str = field(default_factory=lambda: _env("AMPLITUDE_API_KEY"))
    statsig_secret: str = field(default_factory=lambda: _env("STATSIG_SERVER_SECRET"))
    singlestore_url: str = field(default_factory=lambda: _env("SINGLESTORE_URL"))
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))
    google_refresh_token: str = field(default_factory=lambda: _env("GOOGLE_REFRESH_TOKEN"))
    gmail_address: str = field(default_factory=lambda: _env("GMAIL_ADDRESS"))
    gmail_app_password: str = field(default_factory=lambda: _env("GMAIL_APP_PASSWORD"))
    gmail_query: str = field(default_factory=lambda: _env("GMAIL_QUERY", "category:purchases newer_than:60d"))
    cognee_api_key: str = field(default_factory=lambda: _env("COGNEE_API_KEY"))
    cognee_base_url: str = field(default_factory=lambda: _env("COGNEE_BASE_URL"))  # per-tenant URL from the API Keys page
    cognee_tenant_id: str = field(default_factory=lambda: _env("COGNEE_TENANT_ID"))
    cognee_dataset: str = field(default_factory=lambda: _env("COGNEE_DATASET", "owed_claim_memory"))

    # Agent behaviour
    user_id: str = field(default_factory=lambda: _env("OWED_USER_ID", "demo-user"))
    user_email: str = field(default_factory=lambda: _env("OWED_USER_EMAIL"))
    demo_today: str = field(default_factory=lambda: _env("DEMO_TODAY", "2026-11-20"))
    default_auto_limit_usd: float = field(default_factory=lambda: float(_env("AUTO_CLAIM_LIMIT_USD", "150")))
    inbox_source: str = field(default_factory=lambda: _env("INBOX_SOURCE", "auto"))  # auto | gmail | local
    merchant_base_url: str = field(default_factory=lambda: _env("MERCHANT_BASE_URL"))  # empty = in-process mock store
    parallel_cases: int = field(default_factory=lambda: int(_env("PARALLEL_CASES", "8")))

    def today(self) -> date:
        if self.demo_today:
            return date.fromisoformat(self.demo_today)
        return date.today()

    def enabled(self) -> dict[str, bool]:
        return {
            "senso": bool(self.senso_api_key),
            "superlinked_sie": bool(self.sie_api_key),
            "arcee": bool(self.arcee_api_key),
            "amplitude": bool(self.amplitude_api_key),
            "statsig": bool(self.statsig_secret),
            "singlestore": bool(self.singlestore_url),
            "cognee": bool(self.cognee_api_key and self.cognee_base_url),
            "airbyte_gmail": bool(self.google_client_id and self.google_client_secret and self.google_refresh_token),
            "gmail": bool(self.gmail_address and self.gmail_app_password)
            or bool(self.google_client_id and self.google_client_secret and self.google_refresh_token),
        }


settings = Settings()
RUNTIME.mkdir(parents=True, exist_ok=True)
