"""Canonical filesystem locations for Fate-owned runtime logs."""

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOGGING_DIRECTORY = REPOSITORY_ROOT / "data" / "logging"
DISCORD_LOG_PATH = LOGGING_DIRECTORY / "discord.log"
WHAT_DIED_LOG_PATH = LOGGING_DIRECTORY / "what_died.txt"
LAST_ERROR_LOG_PATH = LOGGING_DIRECTORY / "last_error.txt"


def ensure_logging_directory() -> Path:
    """Create and return Fate's runtime logging directory."""
    LOGGING_DIRECTORY.mkdir(parents=True, exist_ok=True)
    return LOGGING_DIRECTORY
