"""The single place where we talk to the model.

Reads OPENAI_API_KEY and OPENAI_BASE_URL from .env. The base URL points at
the class LLM proxy, which speaks the OpenAI chat-completions API.
"""

import os
import time

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError

from support_chatbot import observe
from support_chatbot import pricing
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


RATE_LIMIT_TRIES = 6


def _call(kwargs):
    """One API call, waiting out the proxy when we ask too fast.

    The class proxy allows 60 requests a minute. That is generous for a
    person typing and nowhere near enough for an eval suite firing turns
    back to back, which is how a 429 first showed up: as fifteen agent
    "failures" that were nothing of the sort. A rate limit is not an error
    to report, it is a queue to join.
    """
    for attempt in range(RATE_LIMIT_TRIES):
        try:
            return client().chat.completions.create(**kwargs)
        except RateLimitError as e:
            # Two different things arrive as a 429, and only one is worth
            # waiting out. "Slow down" clears in under a minute. "This
            # key's budget is used up" never clears, and retrying it buys
            # a minute of silence before the identical failure — which is
            # exactly how a finished eval run looks like a hung one.
            if "insufficient_quota" in str(e) or "budget" in str(e):
                raise
            if attempt == RATE_LIMIT_TRIES - 1:
                raise
            wait = 2 ** (attempt + 1)      # 2, 4, 8, 16, 32 — a minute in all,
                                           # which is the window being enforced
            observe.log("llm", model=kwargs.get("model"), ms=0,
                        error=f"rate limited, waiting {wait}s")
            time.sleep(wait)


def complete(messages, tools=None, model=MODEL, temperature=0.3):
    """One model call, timed and logged. Everything goes through here."""
    kwargs = {"model": model, "messages": messages, "temperature": temperature}
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
