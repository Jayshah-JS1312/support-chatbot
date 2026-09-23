"""Authentication identity context and password/token primitives."""

from __future__ import annotations

import hashlib
import secrets
from contextvars import ContextVar
from dataclasses import dataclass

import bcrypt


@dataclass(frozen=True)
class Identity:
    user_id: str
    email: str
    display_name: str
    role: str
    session_id: str | None = None

    def public(self):
        return {
            "id": self.user_id,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role,
        }


_identity = ContextVar("support_chatbot_identity", default=None)


def current_identity():
    return _identity.get()


def set_identity(identity):
    return _identity.set(identity)


def reset_identity(token):
    _identity.reset(token)


def hash_password(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password, encoded):
    try:
        return bcrypt.checkpw(password.encode(), encoded.encode())
    except (TypeError, ValueError):
        return False


def new_token():
    return secrets.token_urlsafe(32)


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()
