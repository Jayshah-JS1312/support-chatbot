"""Environment-backed runtime configuration."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("SUPPORT_CHATBOT_HOST", "127.0.0.1")
    port: int = int(os.getenv("SUPPORT_CHATBOT_PORT", "8000"))
    max_request_bytes: int = int(os.getenv("SUPPORT_CHATBOT_MAX_REQUEST_BYTES", "65536"))
    secure_cookies: bool = _flag("SUPPORT_CHATBOT_SECURE_COOKIES")
    expose_internal_ui: bool = _flag("SUPPORT_CHATBOT_EXPOSE_INTERNAL_UI")
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://support_chatbot:support_chatbot@127.0.0.1:5432/support_chatbot",
    )
    database_pool_min: int = int(os.getenv("SUPPORT_CHATBOT_DB_POOL_MIN", "1"))
    database_pool_max: int = int(os.getenv("SUPPORT_CHATBOT_DB_POOL_MAX", "5"))


settings = Settings()
