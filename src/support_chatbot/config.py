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
    auth_session_hours: int = int(os.getenv("SUPPORT_CHATBOT_AUTH_SESSION_HOURS", "24"))
    expose_reset_token: bool = _flag("SUPPORT_CHATBOT_EXPOSE_RESET_TOKEN")
    public_base_url: str = os.getenv("SUPPORT_CHATBOT_PUBLIC_BASE_URL", "").rstrip("/")
    qstash_token: str = os.getenv("QSTASH_TOKEN", "")
    qstash_current_signing_key: str = os.getenv("QSTASH_CURRENT_SIGNING_KEY", "")
    qstash_next_signing_key: str = os.getenv("QSTASH_NEXT_SIGNING_KEY", "")
    workflow_retries: int = int(os.getenv("SUPPORT_CHATBOT_WORKFLOW_RETRIES", "3"))
    workflow_lease_seconds: int = int(os.getenv("SUPPORT_CHATBOT_WORKFLOW_LEASE_SECONDS", "300"))
    absence_demo_mode: bool = _flag("SUPPORT_CHATBOT_ABSENCE_DEMO_MODE")
    approval_reminder_seconds: int = int(os.getenv("SUPPORT_CHATBOT_APPROVAL_REMINDER_SECONDS", "3600"))
    approval_escalation_seconds: int = int(os.getenv("SUPPORT_CHATBOT_APPROVAL_ESCALATION_SECONDS", "7200"))
    approval_expiry_seconds: int = int(os.getenv("SUPPORT_CHATBOT_APPROVAL_EXPIRY_SECONDS", "86400"))
    absence_scan_seconds: int = int(os.getenv("SUPPORT_CHATBOT_ABSENCE_SCAN_SECONDS", "15"))

    @property
    def approval_deadlines(self) -> tuple[int, int, int]:
        if self.absence_demo_mode:
            return 60, 120, 300
        deadlines = (
            self.approval_reminder_seconds,
            self.approval_escalation_seconds,
            self.approval_expiry_seconds,
        )
        if not (0 < deadlines[0] < deadlines[1] < deadlines[2]):
            raise ValueError("Approval deadlines must be positive and ordered reminder < escalation < expiry")
        return deadlines

    @property
    def workflow_enabled(self) -> bool:
        return bool(
            self.public_base_url
            and self.qstash_token
            and self.qstash_current_signing_key
            and self.qstash_next_signing_key
        )

    @property
    def workflow_url(self) -> str:
        return f"{self.public_base_url}/workflow/requests"


settings = Settings()
