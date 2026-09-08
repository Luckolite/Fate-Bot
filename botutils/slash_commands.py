"""Publish legacy prefix commands through an organized slash-command tree.

Fate has more than Discord's 100 top-level application-command limit.  The
prefix command tree is intentionally left untouched while legacy commands are
adapted through discord.py's hybrid invocation path and placed into namespaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from discord import app_commands
from discord.ext import commands
from discord.ext.commands.hybrid import HybridAppCommand
from discord.ext.commands.view import StringView


@dataclass(frozen=True)
class Namespace:
    name: str
    description: str


class LegacyHybridCommand(commands.Command):
    """A prefix command that accepts transformed slash-command arguments."""

    app_command: HybridAppCommand

    async def _parse_arguments(self, ctx: commands.Context) -> None:
        if ctx.interaction is None:
            await super()._parse_arguments(ctx)
            return
        ctx.kwargs = await self.app_command._transform_arguments(
            ctx.interaction,
            ctx.interaction.namespace,
        )


class LegacyHybridGroup(commands.Group):
    """A prefix group whose slash overview accepts transformed arguments."""

    app_command: HybridAppCommand

    async def _parse_arguments(self, ctx: commands.Context) -> None:
        if ctx.interaction is None:
            await super()._parse_arguments(ctx)
            return
        ctx.kwargs = await self.app_command._transform_arguments(
            ctx.interaction,
            ctx.interaction.namespace,
        )


NAMESPACES: Dict[str, Namespace] = {
    "cogs.core.cc": Namespace("cc", "Create and manage custom commands"),
    "cogs.core.core": Namespace("bot", "General Fate and server commands"),
    "cogs.core.messages": Namespace("messages", "Configure automatic server messages"),
    "cogs.core.ranking": Namespace("ranking", "Levels, XP, profiles, and leaderboards"),
    "cogs.core.toggles": Namespace("modules", "Enable or disable Fate modules"),
    "cogs.core.user": Namespace("users", "Manage user access and status"),
    "cogs.fun.actions": Namespace("actions", "Roleplay actions for server members"),
    "cogs.fun.cookies": Namespace("cookies", "Give cookies and view cookie totals"),
    "cogs.fun.factions": Namespace("factions", "Create, manage, and compete with factions"),
    "cogs.fun.factions_rewrite": Namespace("factions", "Create, manage, and compete with factions"),
    "cogs.fun.fun": Namespace("fun", "Games, generators, and novelty commands"),
    "cogs.fun.reactions": Namespace("reactions", "Send reaction GIFs"),
    "cogs.moderation.anti_raid": Namespace("antiraid", "Configure anti-raid protection"),
    "cogs.moderation.case_manager": Namespace("cases", "Search and manage moderation cases"),
    "cogs.moderation.chat_filter": Namespace("chatfilter", "Configure the server chat filter"),
    "cogs.moderation.chat_lock": Namespace("chatlock", "Configure automatic chat locking"),
    "cogs.moderation.limiter": Namespace("limits", "Limit content allowed in channels"),
    "cogs.moderation.lock": Namespace("security", "Lock channels or restrict new members"),
    "cogs.moderation.logger": Namespace("logger", "Configure server event logging"),
    "cogs.moderation.mod_mail": Namespace("modmail", "Configure and use moderation mail"),
    "cogs.moderation.rules": Namespace("rules", "Configure the server rules message"),
    "cogs.utility.emojis": Namespace("emoji", "View and manage server emoji and stickers"),
    "cogs.utility.farewell": Namespace("leave", "Configure member leave messages"),
    "cogs.utility.giveaways": Namespace("giveaway", "Create and manage giveaways"),
    "cogs.utility.notes": Namespace("notes", "Save and review personal notes"),
    "cogs.utility.restore_roles": Namespace("restore-roles", "Restore roles when members rejoin"),
    "cogs.utility.self_roles": Namespace("selfroles", "Create and manage self-role menus"),
    "cogs.utility.server_stats": Namespace("server-stats", "Configure server statistic channels"),
    "cogs.utility.suggestions": Namespace("suggestions", "Create and configure suggestions"),
    "cogs.utility.utility": Namespace("utility", "General server and user utilities"),
    "cogs.utility.vc_log": Namespace("vc-log", "Configure voice-channel logging"),
    "cogs.utility.welcome": Namespace("welcome", "Configure member welcome messages"),
}


SLASH_RENAMES = {
    ("cogs.fun.cookies", "cookie"): "give",
    ("cogs.fun.cookies", "cookies"): "count",
    ("cogs.core.messages", "messages"): "config",
    ("cogs.moderation.limiter", "limit"): "set",
    ("cogs.utility.emojis", "emoji"): "show",
    ("cogs.utility.emojis", "emojis"): "count",
    ("cogs.utility.emojis", "add-emoji"): "add",
    ("cogs.utility.emojis", "sticker"): "show-sticker",
    ("cogs.utility.emojis", "delemoji"): "delete",
    ("cogs.utility.emojis", "rename-emoji"): "rename",
    ("cogs.utility.giveaways", "giveaway"): "start",
    ("cogs.utility.notes", "note"): "add",
    ("cogs.utility.notes", "quicknote"): "quick",
    ("cogs.utility.notes", "notes"): "list",
}


FACTION_SECTION_DESCRIPTIONS = {
    "management": "Create, join, and manage factions and their members",
    "economy": "Faction income, claims, balances, and games",
    "conflict": "Faction battles, raids, and alliances",
}


FACTION_SECTIONS = {
    "create": "management",
    "disband": "management",
    "transfer": "management",
    "join": "management",
    "invite": "management",
    "leave": "management",
    "kick": "management",
    "promote": "management",
    "demote": "management",
    "privacy": "management",
    "set-bio": "management",
    "set-icon": "management",
    "set-banner": "management",
    "rename": "management",
    "toggle-notifs": "management",
    "annex": "conflict",
    "battle": "conflict",
    "raid": "conflict",
    "ally": "conflict",
    "income": "economy",
    "daily": "economy",
    "trade": "economy",
    "industry": "economy",
    "invest": "economy",
    "collect": "economy",
    "boosts": "economy",
    "claim": "economy",
    "unclaim": "economy",
    "claims": "economy",
    "work": "economy",
    "scrabble": "economy",
    "coinflip": "economy",
    "blackjack": "economy",
    "vote": "economy",
    "forage": "economy",
    "balance": "economy",
    "pay": "economy",
    "top": "economy",
    "shop": "economy",
    "glb": "economy",
}


ALREADY_COVERED = {
    ("cogs.fun.fun", "magik"),
    ("cogs.utility.utility", "info"),
    ("cogs.utility.utility", "serverinfo"),
}


TOP_LEVEL_MODULES = {"cogs.moderation.mod"}
MAX_GROUP_COMMANDS = 25


def _add_group_command(
    group: app_commands.Group,
    command: app_commands.Command | app_commands.Group,
) -> None:
    if len(group.commands) >= MAX_GROUP_COMMANDS:
        path = getattr(group, "qualified_name", group.name)
        raise RuntimeError(
            f"Application command group /{path} exceeds Discord's "
            f"{MAX_GROUP_COMMANDS}-child limit while adding {command.name!r}"
        )
    group.add_command(command)


def _has_owner_check(command: commands.Command) -> bool:
    current: Optional[commands.Command] = command
    while current is not None:
        if any(
            "is_owner.<locals>.predicate" in getattr(check, "__qualname__", "")
            for check in current.checks
        ):
            return True
        current = current.parent
    return False


def _is_public(command: commands.Command) -> bool:
    return (
        command.enabled
        and not command.hidden
        and not command.module.startswith("cogs.dev.")
        and not _has_owner_check(command)
    )


def _slash_name(command: commands.Command) -> str:
    return SLASH_RENAMES.get((command.module, command.name), command.name)


def _needs_adapter(command: commands.Command) -> bool:
    return (
        _is_public(command)
        and not isinstance(command, (commands.HybridCommand, commands.HybridGroup))
        and (command.module, command.name) not in ALREADY_COVERED
    )


def _text_fallback(command: commands.Command, name: str) -> HybridAppCommand:
    """Adapt signatures Discord cannot model to one parser-compatible field."""

    async def invoke(ctx: commands.Context, *, arguments: str = ""):
        old_view = ctx.view
        old_content = getattr(ctx.message, "content", "")
        ctx.view = StringView(arguments or "")
        with_command = f"{ctx.prefix or '/'}{command.qualified_name}"
        ctx.message.content = f"{with_command} {arguments}".rstrip()
        try:
            await command.invoke(ctx)
        finally:
            ctx.view = old_view
            ctx.message.content = old_content

    invoke.__name__ = f"slash_{command.callback.__name__}"
    invoke.__module__ = command.module
    invoke = app_commands.describe(
        arguments="Arguments exactly as they would follow the prefix command"
    )(invoke)
    adapter = commands.HybridCommand(
        invoke,
        name=name,
        description=command.description or command.short_doc or "Run this command",
    )
    return adapter.app_command


def _application_command(command: commands.Command) -> HybridAppCommand:
    name = _slash_name(command)
    legacy_command = command.copy()
    legacy_command.__class__ = (
        LegacyHybridGroup if isinstance(command, commands.Group) else LegacyHybridCommand
    )
    legacy_command.cog = command.cog
    legacy_command._locale_name = None
    legacy_command._locale_description = None
    try:
        app_command = HybridAppCommand(legacy_command, name=name)
        legacy_command.app_command = app_command
        return app_command
    except (TypeError, ValueError):
        return _text_fallback(command, name)


def _overview_command(group: commands.Group) -> Optional[HybridAppCommand]:
    if not _is_public(group):
        return None
    return _application_command(group)


def _subgroup(command: commands.Group) -> app_commands.Group:
    group = app_commands.Group(
        name=command.name,
        description=command.description or command.short_doc or f"{command.name} commands",
    )
    overview = _overview_command(command)
    if overview is not None:
        overview.name = "overview"
        _add_group_command(group, overview)
    for child in command.commands:
        if not _is_public(child):
            continue
        if isinstance(child, commands.Group):
            _add_group_command(group, _subgroup(child))
        else:
            _add_group_command(group, _application_command(child))
    return group


def _add_faction_commands(target: app_commands.Group, source: commands.Group) -> None:
    sections = {
        name: app_commands.Group(name=name, description=description)
        for name, description in FACTION_SECTION_DESCRIPTIONS.items()
    }
    overview = _overview_command(source)
    if overview is not None:
        overview.name = "overview"
        _add_group_command(target, overview)
    for child in source.commands:
        if not _is_public(child):
            continue
        section = FACTION_SECTIONS.get(child.name)
        if section is None:
            _add_group_command(target, _application_command(child))
        else:
            _add_group_command(sections[section], _application_command(child))
    for section in sections.values():
        if section.commands:
            _add_group_command(target, section)


def _namespace_commands(
    namespace: Namespace,
    prefix_commands: Iterable[commands.Command],
) -> app_commands.Group:
    group = app_commands.Group(name=namespace.name, description=namespace.description)
    app_commands.guild_only(group)

    for command in prefix_commands:
        if not _needs_adapter(command):
            continue
        if isinstance(command, commands.Group):
            if command.name == namespace.name:
                if namespace.name == "factions":
                    _add_faction_commands(group, command)
                    continue
                overview = _overview_command(command)
                if overview is not None:
                    overview.name = "overview"
                    _add_group_command(group, overview)
                for child in command.commands:
                    if not _is_public(child):
                        continue
                    if isinstance(child, commands.Group):
                        _add_group_command(group, _subgroup(child))
                    else:
                        _add_group_command(group, _application_command(child))
            else:
                _add_group_command(group, _subgroup(command))
        else:
            _add_group_command(group, _application_command(command))
    return group


def install_legacy_slash_commands(bot: commands.Bot) -> int:
    """Rebuild grouped slash adapters for every loaded public prefix command."""

    for name in getattr(bot, "_legacy_slash_groups", set()):
        bot.tree.remove_command(name)
    for name in getattr(bot, "_legacy_slash_commands", set()):
        bot.tree.remove_command(name)

    by_namespace: Dict[Namespace, list[commands.Command]] = {}
    generated_commands = set()
    for command in bot.commands:
        if command.module in TOP_LEVEL_MODULES and _needs_adapter(command):
            app_command = _application_command(command)
            app_commands.guild_only(app_command)
            bot.tree.add_command(app_command)
            generated_commands.add(app_command.name)
            continue
        namespace = NAMESPACES.get(command.module)
        if namespace is not None:
            by_namespace.setdefault(namespace, []).append(command)

    generated = set()
    for namespace, prefix_commands in by_namespace.items():
        group = _namespace_commands(namespace, prefix_commands)
        if not group.commands:
            continue
        bot.tree.add_command(group)
        generated.add(group.name)

    bot._legacy_slash_groups = generated
    bot._legacy_slash_commands = generated_commands
    total = len(bot.tree.get_commands())
    if total > 100:
        raise RuntimeError(
            f"Application command tree contains {total} top-level commands; Discord allows 100"
        )
    return total
