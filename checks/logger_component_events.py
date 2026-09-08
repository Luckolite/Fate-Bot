"""Regression checks for removed logger component-update events."""

from pathlib import Path

from apps.Dashboard.dashboard.module_validation import LOGGER_GROUPS
from cogs.moderation.logger import (
    Logger,
    default_config,
    is_component_only_message_update,
)

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    assert "component_update" not in default_config["disabled"]
    assert "component_update" not in Logger.log_types
    assert "Button Update" not in Logger.categories
    assert "Button Update" not in LOGGER_GROUPS
    assert tuple(Logger.categories) == LOGGER_GROUPS

    assert is_component_only_message_update({"components": []})
    assert not is_component_only_message_update(
        {"components": [], "content": "edited"}
    )

    logger = Logger.__new__(Logger)
    logger.config = {
        "guild": {
            "disabled": ["message_edit", "component_update"],
            "channels": {"component_update": 123},
        }
    }
    assert logger._normalize_config()
    assert logger.config["guild"]["disabled"] == ["message_edit"]
    assert "component_update" not in logger.config["guild"]["channels"]

    dashboard = (ROOT / "apps" / "Dashboard" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    assert "Button Update" not in dashboard

    print("Logger component-update events are removed")


if __name__ == "__main__":
    main()
