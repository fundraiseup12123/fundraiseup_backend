from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

_BACKEND_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _BACKEND_DIR.parent


def load_app_env() -> None:
    """Load env from project root first, then backend overrides."""
    for path in (
        _ROOT_DIR / ".env",
        _ROOT_DIR / ".env.local",
        _BACKEND_DIR / ".env",
    ):
        if path.is_file():
            load_dotenv(path, override=False)
