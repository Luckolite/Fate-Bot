"""Load Fate's real cogs and database clients without connecting to Discord."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["FATE_CONFIG_PATH"] = str(ROOT / "data" / "config.test.json")
os.environ["FATE_AUTH_PATH"] = str(ROOT / "data" / "auth.test.json")

from fate import bot, load_auth_config  # noqa: E402


async def main() -> None:
    bot.auth = load_auth_config(os.environ["FATE_AUTH_PATH"])
    if not bot.debug_mode or bot.instance_role != "secondary" or bot.vote_url is not None:
        raise RuntimeError("The isolated test profile was not configured as secondary")
    bot.mongo.client.admin.command("ping")
    await bot._async_setup_hook()
    await bot.setup_hook()
    await bot.create_pool()
    if not bot.pool:
        raise RuntimeError("Fate did not establish its MySQL connection pool")

    async with bot.pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT DATABASE(), COUNT(*) FROM msg")
            database, _ = await cursor.fetchone()
    if database != "fate_test":
        raise RuntimeError(f"Fate connected to unexpected database: {database}")

    configured = set(bot.configured_extensions())
    loaded = {name.removeprefix("cogs.") for name in bot.extensions}
    missing = configured - loaded
    if missing:
        raise RuntimeError(f"Configured extensions failed to load: {sorted(missing)}")

    bot.prepare_application_command_tree()
    info_command = bot.tree.get_command("info")
    if info_command is None:
        raise RuntimeError("The /info application-command group was not registered")
    info_subcommands = {
        option["name"] for option in info_command.to_dict(bot.tree).get("options", [])
    }
    expected_info_subcommands = {"user", "channel", "server", "role", "invite", "bot"}
    if info_subcommands != expected_info_subcommands:
        raise RuntimeError(
            f"Unexpected /info subcommands: {sorted(info_subcommands)}"
        )

    grouped_legacy_commands = {
        "actions", "antiraid", "bot", "cases", "cc", "chatfilter",
        "chatlock", "cookies", "emoji", "factions", "fun", "giveaway",
        "leave", "limits", "logger", "messages", "modmail",
        "modules", "notes", "ranking", "reactions", "restore-roles",
        "rules", "security", "selfroles", "server-stats", "suggestions",
        "utility", "vc-log", "welcome",
    }
    registered_names = {command.name for command in bot.tree.get_commands()}
    missing_groups = grouped_legacy_commands - registered_names
    if missing_groups:
        raise RuntimeError(
            f"Legacy slash-command groups were not registered: {sorted(missing_groups)}"
        )
    if "rename" not in registered_names:
        raise RuntimeError("The top-level /rename moderation command was not registered")
    if len(registered_names) > 100:
        raise RuntimeError(
            f"Discord's 100 top-level command limit was exceeded: {len(registered_names)}"
        )

    faction_group = bot.tree.get_command("factions")
    faction_sections = {
        option["name"] for option in faction_group.to_dict(bot.tree).get("options", [])
    }
    expected_faction_entries = {"overview", "create", "info", "economy", "conflict"}
    if not expected_faction_entries.issubset(faction_sections):
        raise RuntimeError(f"Unexpected /factions layout: {sorted(faction_sections)}")

    bot.configure_application_command_scopes()
    installable = bot.tree.get_command("invite").to_dict(bot.tree)
    if installable.get("integration_types") != [0, 1]:
        raise RuntimeError("/invite was not enabled for guild and user installs")
    if installable.get("contexts") != [0, 1, 2]:
        raise RuntimeError("/invite was not enabled in guilds, DMs, and group DMs")
    guild_only = info_command.to_dict(bot.tree)
    if guild_only.get("integration_types") != [0] or guild_only.get("contexts") != [0]:
        raise RuntimeError("Guild-only commands leaked into user installations")

    from cogs.core.menus import is_public_help_command

    hidden_commands = [command for command in bot.walk_commands() if command.hidden]
    developer_commands = [
        command
        for command in bot.walk_commands()
        if getattr(command.cog, "__module__", "").startswith("cogs.dev.")
        or any(
            "is_owner.<locals>.predicate" in getattr(check, "__qualname__", "")
            for check in command.checks
        )
    ]
    leaked = [
        command.qualified_name
        for command in [*hidden_commands, *developer_commands]
        if is_public_help_command(command)
    ]
    if leaked:
        raise RuntimeError(f"Private commands leaked into help: {sorted(set(leaked))}")

    print(
        f"Offline Fate probe passed: {len(loaded)} extensions, MySQL pool, "
        f"MongoDB, {len(bot.tree.get_commands())} application commands, "
        "and private-command filtering."
    )
    await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
