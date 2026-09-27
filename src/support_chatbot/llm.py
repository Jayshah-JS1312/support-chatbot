"""The single place where we talk to the model.

Reads OPENAI_API_KEY and OPENAI_BASE_URL from .env. The base URL points at
the class LLM proxy, which speaks the OpenAI chat-completions API.
"""

import hashlib
import os
import random
import threading
import time

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from support_chatbot import observe
from support_chatbot import pricing
from support_chatbot.auth import current_identity
from support_chatbot.config import settings

load_dotenv()

MODEL = os.getenv("MODEL", "gpt-4o-mini")

_client = None


def client():
    """Create the provider client lazily so offline commands can import safely."""
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL")
        if not api_key or not base_url:
            raise RuntimeError(
                "OPENAI_API_KEY and OPENAI_BASE_URL must be configured before "
                "making a model request. Copy .env.example to .env."
            )
        _client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=settings.model_timeout_seconds,
            max_retries=0,
        )
    return _client


class ModelCapacityError(RuntimeError):
    """The process is at its configured model-call concurrency limit."""


_model_slots = threading.BoundedSemaphore(max(1, settings.model_max_concurrency))
_cache_capability_lock = threading.Lock()
_prompt_cache_supported = True


def _retry_after(error):
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _cache_key():
    """Keep provider cache routing stable without disclosing a customer ID."""
    identity = current_identity()
    subject = identity.user_id if identity else "anonymous"
    digest = hashlib.sha256(f"ami-prompt-v1:{subject}".encode()).hexdigest()[:32]
    return f"ami-v1-{digest}"


def _call(kwargs):
    """Call the provider with bounded concurrency and a bounded retry budget."""
    acquired = _model_slots.acquire(timeout=settings.model_acquire_timeout_seconds)
    if not acquired:
        observe.log("model_capacity", outcome="rejected")
        raise ModelCapacityError("model_concurrency_limit_reached")
    started = time.monotonic()
    transient = (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)
    try:
        global _prompt_cache_supported
        for attempt in range(max(1, settings.model_retry_attempts)):
            try:
                call_kwargs = dict(kwargs)
                with _cache_capability_lock:
                    cache_supported = _prompt_cache_supported
                if not cache_supported:
                    call_kwargs.pop("prompt_cache_key", None)
                    call_kwargs.pop("safety_identifier", None)
                try:
                    return client().chat.completions.create(**call_kwargs)
                except BadRequestError as error:
                    message = str(error).lower()
                    optional = ("prompt_cache_key", "safety_identifier")
                    if not any(field in message for field in optional):
                        raise
                    # OpenAI-compatible proxies can lag the official schema.
                    # Disable optional cache routing for this process and retry
                    # the same call without weakening any safety behavior.
                    with _cache_capability_lock:
                        _prompt_cache_supported = False
                    observe.log("prompt_cache", outcome="unsupported_by_provider")
                    for field in optional:
                        call_kwargs.pop(field, None)
                    return client().chat.completions.create(**call_kwargs)
            except transient as error:
                # Quota exhaustion is not transient. Retrying it only delays a
                # truthful failure and adds load during an outage.
                if isinstance(error, RateLimitError) and (
                    "insufficient_quota" in str(error) or "budget" in str(error).lower()
                ):
                    raise
                if attempt == max(1, settings.model_retry_attempts) - 1:
                    raise
                server_delay = _retry_after(error)
                exponential = min(
                    settings.model_retry_max_seconds,
                    settings.model_retry_base_seconds * (2 ** attempt),
                )
                wait = server_delay if server_delay is not None else exponential
                wait = min(settings.model_retry_max_seconds, wait + random.uniform(0, wait * 0.25))
                if time.monotonic() - started + wait > settings.model_retry_budget_seconds:
                    raise
                observe.log(
                    "llm_retry",
                    model=kwargs.get("model"),
                    attempt=attempt + 1,
                    delay_ms=round(wait * 1000),
                    error=type(error).__name__,
                )
                time.sleep(wait)
    finally:
        _model_slots.release()


def complete(messages, tools=None, model=MODEL, temperature=0.3):
    """One model call, timed and logged. Everything goes through here."""
    kwargs = {"model": model, "messages": messages, "temperature": temperature}
    if settings.prompt_cache_enabled:
        kwargs["prompt_cache_key"] = _cache_key()
        kwargs["safety_identifier"] = _cache_key()
    if tools:
        kwargs["tools"] = tools

    t = observe.timer().__enter__()
    try:
        response = _call(kwargs)
    except Exception as e:
        t.__exit__()
        observe.log("llm", model=model, ms=t.ms, error=f"{type(e).__name__}: {e}")
        raise
    t.__exit__()

    message = response.choices[0].message
    usage = response.usage

    # Input and output are billed at different rates, so record them apart.
    # total_tokens alone cannot be priced.
    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0

    observe.log("llm",
                model=model,
                ms=t.ms,
                messages=len(messages),
                tokens=getattr(usage, "total_tokens", None),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cached=cached,
                cost=pricing.cost(response.model, tokens_in, tokens_out, cached),
                served_by=response.model,
                finish=response.choices[0].finish_reason,
                tool_calls=len(message.tool_calls or []))
    return response


def chat(messages, model=MODEL, temperature=0.3):
    """Send a list of {"role", "content"} messages, get back the reply text."""
    return complete(messages, model=model,
                    temperature=temperature).choices[0].message.content
