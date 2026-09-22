"""Validated HTTP request and error schemas."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatRequest(StrictRequest):
    message: str = Field(default="", max_length=4000)
    planner: Literal["react", "plan"] = "react"


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
