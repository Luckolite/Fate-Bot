"""
cogs.core.menus
~~~~~~~~~~~~~~~~

An interactive command guide built with Discord views.

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import inspect
import math
from contextlib import suppress
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

import discord
from discord import Embed, Interaction, Message, SelectOption, app_commands, ui
from discord.ext import commands
from discord.ext.commands import Command, Context

from botutils import AuthorView, Cooldown, colors
from fate import Fate

# Use strings or lists to denote what module goes on what page.
structure = {
    "Core": [
        "Core", "Statistics", "CustomCommands", "Config", "Menus",
        "Toggles", "User", "Tasks", "Settings", "Messages", "Music",
    ],
    "Moderation": {
        "Mod Cmds": ["Moderation", "Lock"],
        "Modmail": ["ModMail", "CaseManager"],
        "Logger": "Logger",
        "AntiSpam": "AntiSpam",
        "AntiRaid": "AntiRaid",
        "Chatfilter": "ChatFilter",
        "Channel Limits": ["ChatLock", "Limiter", "Mod"],
        "Verification": "Verification",
    },
    "Utility": {
        "Welcome Messages": "Welcome",
        "Leave Messages": "Leave",
        "AutoRole": "AutoRole",
        "Self-Roles": "SelfRoles",
        "Restore-Roles": "RestoreRoles",
        "Emojis": "Emojis",
        "Vc-Log": "VcLog",
        "Chat Bridges": "ChatBridges",
        "Suggestions": "Suggestions",
        "ServerStatistics": "ServerStatistics",
        "Giveaways": "Giveaways",
        "Misc": ["Polls", "Audit", "Notepad", "Utility"],
    },
    "Misc": ["GlobalChat", "NSFW", "Reddit"],
    "Ranking": "Ranking",
    "Fun": {
        "Board Games": [],
        "Factions": "Factions",
        "Cookies": "Cookies",
        "Actions": "Actions",
        "Reactions": "Reactions",
        "Responses": "Responses",
        "Misc": "Fun",
    },
}


def is_developer_command(command: Command) -> bool:
    """Return whether a command is internal or restricted to bot owners."""
    module = getattr(command.cog, "__module__", "")
    if module.startswith("cogs.dev."):
        return True
    current = command
    while current:
        if any(
            "is_owner.<locals>.predicate" in getattr(check, "__qualname__", "")
            for check in current.checks
        ):
            return True
        current = current.parent
    return False


def is_public_help_command(command: Command) -> bool:
    """Apply the same public visibility rule across every help surface."""
    current = command
    while current:
        if not current.enabled or current.hidden:
            return False
        current = current.parent
    return not is_developer_command(command)


class Menus(commands.Cog):
    """Handles the interactive help menu."""

    structure: Dict[str, Any] = {}

    def __init__(self, bot: Fate) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if self.bot.is_ready():
            await self.restructure()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.restructure()

    async def construct(self, item: Union[str, List[str]]) -> List[Command]:
        """Convert one or more cog names into their commands."""
        items = []
        cog_names = [item] if isinstance(item, str) else item
        for cog_name in cog_names:
            if cog := self.bot.get_cog(cog_name):
                items.extend(
                    command
                    for command in cog.walk_commands()
                    if is_public_help_command(command)
                )

        for command in items:
            usage_attr = command.name + "_usage"
            if not hasattr(command.cog, usage_attr):
                continue
            usage = getattr(command.cog, usage_attr)
            # Decorated cog methods are Command objects and are callable only
            # with a Context. They are legacy usage aliases, not factories.
            if callable(usage) and not isinstance(usage, Command):
                usage = usage()
            if inspect.isawaitable(usage):
                usage = await usage
            if isinstance(usage, (str, Embed)):
                command.usage = usage
        return items

    async def restructure(self) -> None:
        """Rebuild the help index from the currently loaded cogs."""
        rebuilt = {}
        for category, value in structure.items():
            if isinstance(value, dict):
                subcategories = {}
                for subcategory, cogs in value.items():
                    commands_found = await self.construct(cogs)
                    if commands_found:
                        subcategories[subcategory] = commands_found
                if subcategories:
                    rebuilt[category] = subcategories
            else:
                commands_found = await self.construct(value)
                if commands_found:
                    rebuilt[category] = commands_found
        self.structure = rebuilt

    @commands.hybrid_command(
        name="help",
        description="Opens the interactive command guide",
    )
    @app_commands.describe(query="Command or keyword to open")
    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.bot_has_permissions(embed_links=True)
    async def help(self, ctx: Context, *, query: Optional[str] = None) -> None:
        if not self.structure:
            await self.restructure()
        await HelpMenu(self.structure, ctx, query=query)

    @help.autocomplete("query")
    async def help_autocomplete(
        self,
        interaction: Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        current = current.casefold()
        matches = []
        for command in self.bot.walk_commands():
            if not is_public_help_command(command):
                continue
            description = command.description or command.short_doc or "Command help"
            searchable = f"{command.qualified_name} {' '.join(command.aliases)} {description}".casefold()
            if current and current not in searchable:
                continue
            matches.append(app_commands.Choice(
                name=f"{command.qualified_name} — {description}"[:100],
                value=command.qualified_name[:100],
            ))
        return matches[:25]


async def setup(bot: Fate) -> None:
    await bot.add_cog(Menus(bot))


class HelpMenu(AuthorView):
    """A searchable and paginated command guide."""

    page_size = 10
    category_emojis = {
        "Core": "⚙️",
        "Moderation": "🛡️",
        "Utility": "🧰",
        "Misc": "🧩",
        "Ranking": "🏆",
        "Fun": "🎮",
        "Dev": "🧪",
    }

    def __init__(
        self,
        structure: Dict[str, Any],
        ctx: Context,
        *,
        query: Optional[str] = None,
    ) -> None:
        self.ctx = ctx
        self.bot: Fate = ctx.bot
        self.cd = Cooldown(4, 5)
        self.message: Optional[Message] = None
        self.structure = {
            key: value
            for key, value in structure.items()
            if key != "Dev"
        }
        self.state: Union[Dict[str, Any], List[Command]] = self.structure
        self.breadcrumbs: List[str] = []
        self.history: List[Tuple[Any, List[str], int, Optional[Command]]] = []
        self.page = 0
        self.selected_command: Optional[Command] = None
        self.query_miss: Optional[str] = None

        super().__init__(timeout=300)
        if query and not self.open_query(query):
            self.query_miss = query
        self.refresh()

    def __await__(self) -> Generator[None, None, "HelpMenu"]:
        return self._send().__await__()

    async def _send(self) -> "HelpMenu":
        self.message = await self.ctx.send("https://phasic.dev/dashboard", embed=self.embed, view=self)
        await self.wait()
        return self

    async def on_timeout(self) -> None:
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(view=None)

    def visible(self, command: Command) -> bool:
        return is_public_help_command(command)

    def command_list(self, value: Any = None) -> List[Command]:
        value = self.structure if value is None else value
        if isinstance(value, list):
            return [command for command in value if self.visible(command)]
        commands_found = []
        for child in value.values():
            commands_found.extend(self.command_list(child))
        return commands_found

    def count_commands(self, value: Any) -> int:
        return len({command.qualified_name for command in self.command_list(value)})

    @staticmethod
    def get_description(command: Command) -> str:
        if description := command.description or command.short_doc:
            return description
        name = command.name.replace("-", " ").lstrip(".").title()
        return f"Runs the {name} command"

    def get_usage(self, command: Command) -> str:
        if isinstance(command.usage, str):
            return command.usage
        invocation = f"{self.ctx.prefix}{command.qualified_name}"
        if command.signature:
            invocation += f" {command.signature}"
        return f"`{invocation}`"

    def command_aliases(self, command: Command) -> str:
        if not command.aliases:
            return "None"
        parent = f"{command.parent.qualified_name} " if command.parent else ""
        return ", ".join(
            f"`{self.ctx.prefix}{parent}{alias}`" for alias in command.aliases
        )

    def snapshot(self) -> Tuple[Any, List[str], int, Optional[Command]]:
        return self.state, list(self.breadcrumbs), self.page, self.selected_command

    def restore(self, snapshot: Tuple[Any, List[str], int, Optional[Command]]) -> None:
        self.state, self.breadcrumbs, self.page, self.selected_command = snapshot

    def push_history(self) -> None:
        self.history.append(self.snapshot())

    def go_home(self) -> None:
        self.state = self.structure
        self.breadcrumbs = []
        self.page = 0
        self.selected_command = None

    def all_commands(self) -> List[Command]:
        unique = {}
        for command in self.command_list():
            unique[command.qualified_name] = command
        return list(unique.values())

    def search(self, query: str) -> List[Command]:
        query = query.strip().casefold()
        prefix = str(self.ctx.prefix).casefold()
        if query.startswith(prefix):
            query = query[len(prefix):].strip()
        if not query:
            return []

        scored = []
        for command in self.all_commands():
            name = command.qualified_name.casefold()
            aliases = [alias.casefold() for alias in command.aliases]
            description = self.get_description(command).casefold()
            if query == name or query in aliases:
                score = 0
            elif name.startswith(query):
                score = 1
            elif query in name:
                score = 2
            elif any(query in alias for alias in aliases):
                score = 3
            elif query in description:
                score = 4
            else:
                continue
            scored.append((score, name, command))
        return [command for _score, _name, command in sorted(scored)]

    def open_query(self, query: str) -> bool:
        results = self.search(query)
        if not results:
            return False
        normalized = query.strip().casefold()
        prefix = str(self.ctx.prefix).casefold()
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].strip()
        exact = next(
            (
                command for command in results
                if command.qualified_name.casefold() == normalized
                or normalized in (alias.casefold() for alias in command.aliases)
            ),
            None,
        )
        if exact:
            self.selected_command = exact
            self.breadcrumbs = ["Search", exact.qualified_name]
        else:
            self.state = results
            self.breadcrumbs = [f"Search: {query.strip()[:40]}"]
            self.selected_command = None
        self.page = 0
        self.query_miss = None
        return True

    @property
    def page_count(self) -> int:
        if self.selected_command or not isinstance(self.state, list):
            return 1
        return max(1, math.ceil(len(self.command_list(self.state)) / self.page_size))

    def current_page(self) -> List[Command]:
        if not isinstance(self.state, list):
            return []
        start = self.page * self.page_size
        return self.command_list(self.state)[start:start + self.page_size]

    def build_home_embed(self) -> Embed:
        embed = Embed(
            title="Fate Command Guide",
            color=colors.fate,
            description=(
                f"Browse **{self.count_commands(self.structure)} commands** or use "
                "the search button to jump straight to one.\n\n"
                f"[Support]({self.bot.config['support_server']}) • "
                f"[Invite Fate]({self.bot.invite_url}) • "
                "[Privacy](https://gist.github.com/FrequencyX4/a31d065b66d9ce2448f3dae3ac96bfd1)"
            ),
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        for category, value in self.structure.items():
            emoji = self.category_emojis.get(category, "📁")
            embed.add_field(
                name=f"{emoji} {category}",
                value=f"{self.count_commands(value)} commands",
                inline=True,
            )
        embed.add_field(
            name="Quick start",
            value=(
                f"Use `{self.ctx.prefix}help ban` to open a command directly.\n"
                "Hybrid commands can also be opened with `/help`."
            ),
            inline=False,
        )
        if self.query_miss:
            embed.add_field(
                name="No exact match",
                value=f"Nothing matched `{self.query_miss[:80]}`. Try the Search button.",
                inline=False,
            )
        embed.set_footer(text="Select a category below • This menu is private to your interactions")
        return embed

    def build_section_embed(self) -> Embed:
        title = " › ".join(self.breadcrumbs) or "Command Guide"
        embed = Embed(title=title, color=colors.fate)
        if isinstance(self.state, dict):
            embed.description = "Choose a section to narrow the command list."
            for name, value in self.state.items():
                embed.add_field(
                    name=f"📁 {name}",
                    value=f"{self.count_commands(value)} commands",
                    inline=True,
                )
        else:
            commands_on_page = self.current_page()
            if commands_on_page:
                embed.description = "\n".join(
                    f"`{self.ctx.prefix}{command.qualified_name}`\n"
                    f"{self.get_description(command)}"
                    for command in commands_on_page
                )
            else:
                embed.description = "No loaded commands are available in this section."
            embed.set_footer(
                text=f"Page {self.page + 1}/{self.page_count} • Select a command for details"
            )
        return embed

    def build_command_embed(self, command: Command) -> Embed:
        if isinstance(command.usage, Embed):
            embed = command.usage.copy()
            if not embed.title:
                embed.title = f"{self.ctx.prefix}{command.qualified_name}"
            return embed

        embed = Embed(
            title=f"{self.ctx.prefix}{command.qualified_name}",
            description=self.get_description(command),
            color=colors.fate,
        )
        embed.add_field(name="Usage", value=self.get_usage(command)[:1024], inline=False)
        embed.add_field(name="Aliases", value=self.command_aliases(command)[:1024], inline=False)
        embed.add_field(
            name="Available as",
            value="Prefix command • Slash command"
            if isinstance(command, commands.HybridCommand)
            else "Prefix command",
            inline=True,
        )
        embed.add_field(
            name="Module",
            value=command.cog.qualified_name if command.cog else "No module",
            inline=True,
        )
        embed.set_footer(text="Use Back to return to the command list")
        return embed

    def build_embed(self) -> Embed:
        if self.selected_command:
            return self.build_command_embed(self.selected_command)
        if self.state is self.structure:
            return self.build_home_embed()
        return self.build_section_embed()

    def select_options(self) -> List[SelectOption]:
        if self.selected_command:
            return []
        if isinstance(self.state, dict):
            return [
                SelectOption(
                    emoji="📁",
                    label=name[:100],
                    description=f"{self.count_commands(value)} commands",
                    value=f"nav:{name}",
                )
                for name, value in self.state.items()
            ][:25]
        return [
            SelectOption(
                emoji="⚡" if isinstance(command, commands.HybridCommand) else "⌨️",
                label=f"{self.ctx.prefix}{command.qualified_name}"[:100],
                description=self.get_description(command)[:100],
                value=f"cmd:{command.qualified_name}",
            )
            for command in self.current_page()
        ]

    def refresh(self) -> None:
        self.embed = self.build_embed()
        self.clear_items()

        if options := self.select_options():
            placeholder = "Choose a command" if isinstance(self.state, list) else "Choose a section"
            self.add_item(HelpSelect(self, options=options, placeholder=placeholder))

        self.add_item(HelpButton(self, "back", label="Back", emoji="↩️", row=1, disabled=not self.history))
        self.add_item(HelpButton(self, "home", label="Home", emoji="🏠", row=1, disabled=self.state is self.structure and not self.selected_command))
        self.add_item(HelpButton(self, "previous", label="Previous", emoji="◀️", row=1, disabled=self.page <= 0 or self.selected_command is not None))
        self.add_item(HelpButton(self, "next", label="Next", emoji="▶️", row=1, disabled=self.page >= self.page_count - 1 or self.selected_command is not None))
        self.add_item(HelpButton(self, "search", label="Search", emoji="🔎", style=discord.ButtonStyle.primary, row=2))
        self.add_item(HelpButton(self, "close", label="Close", emoji="✖️", style=discord.ButtonStyle.danger, row=2))

    async def edit(self, interaction: Interaction) -> None:
        self.refresh()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def navigate(self, value: str, interaction: Interaction) -> None:
        if value.startswith("nav:") and isinstance(self.state, dict):
            key = value[4:]
            if key not in self.state:
                return await interaction.response.send_message("That section is no longer available.", ephemeral=True)
            self.push_history()
            self.state = self.state[key]
            self.breadcrumbs.append(key)
            self.page = 0
            self.selected_command = None
            return await self.edit(interaction)

        if value.startswith("cmd:"):
            command = self.bot.get_command(value[4:])
            if not command or not self.visible(command):
                return await interaction.response.send_message("That command is no longer available.", ephemeral=True)
            self.push_history()
            self.selected_command = command
            self.breadcrumbs.append(command.name)
            return await self.edit(interaction)

    async def handle_action(self, action: str, interaction: Interaction) -> None:
        if action == "close":
            self.stop()
            return await interaction.response.edit_message(embed=self.embed, view=None)
        if action == "search":
            return await interaction.response.send_modal(HelpSearchModal(self))
        if action == "back" and self.history:
            self.restore(self.history.pop())
        elif action == "home":
            self.push_history()
            self.go_home()
        elif action == "previous" and self.page > 0:
            self.page -= 1
        elif action == "next" and self.page < self.page_count - 1:
            self.page += 1
        return await self.edit(interaction)


class HelpSelect(ui.Select):
    def __init__(
        self,
        menu: HelpMenu,
        *,
        options: List[SelectOption],
        placeholder: str,
    ) -> None:
        self.menu = menu
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: Interaction) -> None:
        await self.menu.navigate(self.values[0], interaction)


class HelpButton(ui.Button):
    def __init__(
        self,
        menu: HelpMenu,
        action: str,
        *,
        label: Optional[str] = None,
        emoji: Optional[str] = None,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        row: int,
        disabled: bool = False,
    ) -> None:
        self.menu = menu
        self.action = action
        super().__init__(
            label=label,
            emoji=emoji,
            style=style,
            row=row,
            disabled=disabled,
        )

    async def callback(self, interaction: Interaction) -> None:
        await self.menu.handle_action(self.action, interaction)


class HelpSearchModal(ui.Modal, title="Search commands"):
    query = ui.TextInput(
        label="Command or keyword",
        placeholder="Try: ban, roles, music...",
        min_length=1,
        max_length=100,
    )

    def __init__(self, menu: HelpMenu) -> None:
        self.menu = menu
        super().__init__()

    async def on_submit(self, interaction: Interaction) -> None:
        query = self.query.value
        results = self.menu.search(query)
        if not results:
            return await interaction.response.send_message(
                f"No commands matched `{query}`.",
                ephemeral=True,
            )

        self.menu.push_history()
        self.menu.open_query(query)
        await self.menu.edit(interaction)
