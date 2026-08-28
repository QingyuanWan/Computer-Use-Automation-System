"""Agent configuration + fail-fast API-key loading (ADR-008 boundary: agent is the LLM-owning module)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

try:  # optional dependency; a manual .env parse is the fallback
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

DEFAULT_MODEL = "claude-sonnet-4-6"


class AgentConfigError(RuntimeError):
    pass


def _manual_env_key(env_path: Path) -> Optional[str]:
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("ANTHROPIC_API_KEY="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            if len(val) > 10:
                return val
    return None


def load_api_key(explicit: Optional[str] = None, env_path: Optional[Path] = None) -> str:
    """Return the Anthropic API key, or raise AgentConfigError (fail fast at agent init)."""
    if explicit:
        return explicit
    if load_dotenv is not None:
        load_dotenv()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        key = _manual_env_key(env_path or Path(".env"))
    if not key:
        raise AgentConfigError(
            "ANTHROPIC_API_KEY not found (checked process env and .env). Set it before creating a DiscoveryAgent.")
    return key
