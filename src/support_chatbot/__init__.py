"""Support Chatbot application package."""

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent

# Runtime data is kept outside the importable package. Deployments should mount
# these paths on persistent storage; tests override the module-level paths.
STATE_DIR = Path(os.getenv("SUPPORT_CHATBOT_STATE_DIR", PROJECT_ROOT / "state"))
CACHE_DIR = Path(os.getenv("SUPPORT_CHATBOT_CACHE_DIR", PROJECT_ROOT / ".cache"))
KNOWLEDGE_DIR = PACKAGE_ROOT / "knowledge"
UI_DIR = PACKAGE_ROOT / "ui"

__all__ = [
    "CACHE_DIR",
    "KNOWLEDGE_DIR",
    "PACKAGE_ROOT",
    "PROJECT_ROOT",
    "STATE_DIR",
    "UI_DIR",
]
