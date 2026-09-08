"""Build the public command catalog without importing the Discord bot."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

COMMAND_DECORATORS = {"command", "group", "hybrid_command", "hybrid_group"}
CATEGORY_LABELS = {
    "core": "Core",
    "fun": "Fun & Games",
    "misc": "Community",
    "moderation": "Moderation",
    "utility": "Utility",
}


def _decorator_name(decorator: ast.expr) -> str:
    call = decorator if isinstance(decorator, ast.Call) else None
    target = call.func if call else decorator
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def _literal_keyword(call: ast.Call, name: str, default: Any = None) -> Any:
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    return default


def _parent_group(call: ast.Call) -> str | None:
    target = call.func
    if not isinstance(target, ast.Attribute) or target.attr != "command":
        return None
    if isinstance(target.value, ast.Name) and target.value.id != "commands":
        return target.value.id.lstrip("_").replace("_", "-")
    return None


def commands_from_file(path: Path) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return []

    commands: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        if any(_decorator_name(decorator) == "is_owner" for decorator in node.decorator_list):
            continue

        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if _decorator_name(decorator) not in COMMAND_DECORATORS:
                continue
            if _literal_keyword(decorator, "enabled", True) is False:
                continue
            if _literal_keyword(decorator, "hidden", False) is True:
                continue

            name = _literal_keyword(decorator, "name", node.name.lstrip("_"))
            if decorator.args and isinstance(decorator.args[0], ast.Constant):
                if isinstance(decorator.args[0].value, str):
                    name = decorator.args[0].value
            name = str(name).replace("_", "-")
            parent = _parent_group(decorator)
            qualified_name = f"{parent} {name}" if parent and parent != name else name
            description = _literal_keyword(decorator, "description", "")
            if not description:
                description = (ast.get_docstring(node) or "").split("\n", 1)[0]
            if not description:
                description = f"Use Fate's {qualified_name} command."

            commands.append(
                {
                    "name": qualified_name,
                    "description": str(description).strip(),
                    "category": CATEGORY_LABELS.get(path.parent.name, path.parent.name.title()),
                    "source": path.stem,
                }
            )
            break
    return commands


def build_catalog(cogs_root: Path) -> list[dict[str, Any]]:
    """Return a stable, de-duplicated list of public commands."""
    catalog: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(cogs_root.rglob("*.py")):
        relative_parts = path.relative_to(cogs_root).parts
        if "dev" in relative_parts[:-1]:
            continue
        for command in commands_from_file(path):
            key = (command["category"], command["name"])
            if key in seen:
                continue
            seen.add(key)
            catalog.append(command)
    return sorted(catalog, key=lambda item: (item["category"], item["name"]))
