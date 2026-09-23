"""Validated HTTP request and error schemas."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def validate_bcrypt_password(value: str) -> str:
    if len(value.encode("utf-8")) > 72:
        raise ValueError("must not exceed 72 UTF-8 bytes")
    return value


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SignupRequest(StrictRequest):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        value = value.strip().lower()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("must be a valid email address")
        return value

    _validate_password = field_validator("password")(validate_bcrypt_password)


class LoginRequest(StrictRequest):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)

    _validate_password = field_validator("password")(validate_bcrypt_password)


class PasswordResetRequest(StrictRequest):
    email: str = Field(min_length=3, max_length=320)


class PasswordResetConfirmRequest(StrictRequest):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=12, max_length=128)

    _validate_password = field_validator("new_password")(validate_bcrypt_password)


class ChatRequest(StrictRequest):
    message: str = Field(default="", max_length=4000)
    planner: Literal["react", "plan"] = "react"


class ApprovalDecisionRequest(StrictRequest):
    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=2_000)


class ResetRequest(StrictRequest):
    pass


class FeedbackRequest(StrictRequest):
    golden_row_id: str | int | None = None
    original_response: str = Field(max_length=20_000)
    corrected_response: str = Field(max_length=20_000)
    reason: str = Field(default="", max_length=4_000)

    @field_validator("original_response", "corrected_response")
    @classmethod
    def required_text(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("reason")
    @classmethod
    def trim_reason(cls, value):
        return value.strip()
