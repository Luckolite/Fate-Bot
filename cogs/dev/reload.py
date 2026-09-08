"""Owner-only extension lifecycle commands."""

import asyncio
import io
import traceback
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from time import monotonic
from typing import Iterable, Optional

import discord
from discord.ext import commands

from botutils import colors

COGS_ROOT = Path(__file__).resolve().parents[1]
COMMAND_SYNC_COOLDOWN_SECONDS = 5 * 60


@dataclass(slots=True)
class ExtensionResult:
    requested: str
    extension: Optional[str] = None
    action: Optional[str] = None
    elapsed_ms: int = 0
    error: Optional[str] = None
    traceback: Optional[str] = None

    @property
    def name(self) -> str:
        return (self.extension or self.requested).removesuffix(".py").rsplit(".", 1)[-1]

    @property
    def category(self) -> str:
        parts = (
            (self.extension or self.requested)
            .removeprefix("cogs.")
            .removesuffix(".py")
            .split(".")
        )
        return parts[-2] if len(parts) > 1 else "other"

    @property
    def succeeded(self) -> bool:
        return self.error is None


class Reload(commands.Cog):
    """Load, reload, unload, and publish extensions without restarting Fate."""

    def __init__(self, bot):
        self.bot = bot
        self._operation_lock = asyncio.Lock()
        self._extensions = self._build_extension_index()

    @commands.command(
        name="logout",
        aliases=["shutdown"],
        description="Shuts Fate down gracefully",
        hidden=True,
    )
    @commands.is_owner()
    async def logout(self, ctx):
        """Disconnect Fate and release its shared resources."""
        await ctx.send("Logging out and shutting down.")
        await self.bot.close()

    @staticmethod
    def _build_extension_index() -> dict[str, tuple[str, ...]]:
        """Index canonical extension paths and convenient user-facing aliases."""
        aliases: dict[str, set[str]] = {}
        for path in COGS_ROOT.rglob("*.py"):
            if path.name == "__init__.py" or "__pycache__" in path.parts:
                continue

            relative = path.relative_to(COGS_ROOT).with_suffix("")
            extension = f"cogs.{'.'.join(relative.parts)}"
            for alias in (extension, extension.removeprefix("cogs."), path.stem):
                aliases.setdefault(alias.casefold(), set()).add(extension)

        return {alias: tuple(sorted(matches)) for alias, matches in aliases.items()}

    def _configured_extensions(self) -> tuple[str, ...]:
        configured = getattr(self.bot, "configured_extensions", None)
        if configured is not None:
            return tuple(configured())
        return tuple(
            f"{category}.{cog}"
            for category, cogs in self.bot.config.get("extensions", {}).items()
            for cog in cogs
        )

    def _sync_cooldown_remaining(self) -> int:
        last_sync = getattr(self.bot, "_application_command_sync_at", 0.0)
        return max(
            0,
            ceil(COMMAND_SYNC_COOLDOWN_SECONDS - (monotonic() - last_sync)),
        )

    @staticmethod
    def _format_cooldown(seconds: int) -> str:
        minutes, seconds = divmod(max(0, seconds), 60)
        return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

    @staticmethod
    def _full_reload_requested(modules: tuple[str, ...]) -> bool:
        return not modules or (
            len(modules) == 1 and modules[0].strip().casefold() == "all"
        )

    def _refresh_website(self) -> str:
        app = getattr(self.bot, "app", None)
        dashboard = app.get("dashboard") if app is not None else None
        refresh = getattr(dashboard, "refresh_runtime", None)
        if not callable(refresh):
            raise RuntimeError("The web dashboard is not mounted.")
        return str(refresh())

    def _resolve(self, module: str) -> str:
        normalized = (
            module.strip()
            .replace("/", ".")
            .replace("\\", ".")
            .strip(".")
            .casefold()
            .removesuffix(".py")
        )
        matches = self._extensions.get(normalized, ())
        if not matches:
            # A developer may have added a new cog since this cog was loaded.
            self._extensions = self._build_extension_index()
            matches = self._extensions.get(normalized, ())
        if len(matches) > 1:
            choices = ", ".join(match.removeprefix("cogs.") for match in matches)
            raise commands.BadArgument(f"`{module}` is ambiguous: {choices}")
        if not matches:
            raise commands.BadArgument(f"Couldn't find a cog matching `{module}`.")
        return matches[0]

    @staticmethod
    def _failure(
        requested: str, error: Exception, extension: str = None
    ) -> ExtensionResult:
        summary = "".join(traceback.format_exception_only(type(error), error)).strip()
        return ExtensionResult(
            requested=requested,
            extension=extension,
            error=summary,
            traceback=traceback.format_exc(),
        )

    async def _reload_extensions(
        self, requested: Iterable[str]
    ) -> list[ExtensionResult]:
        results = []
        seen = set()

        for module in requested:
            try:
                extension = self._resolve(module)
            except commands.BadArgument as error:
                results.append(self._failure(module, error))
                continue

            if extension in seen:
                continue
            seen.add(extension)
            started = monotonic()
            action = "reloaded"
            try:
                try:
                    await self.bot.reload_extension(extension)
                except commands.ExtensionNotLoaded:
                    action = "loaded"
                    await self.bot.load_extension(extension)
            except commands.ExtensionError as error:
                results.append(self._failure(module, error, extension))
            else:
                results.append(
                    ExtensionResult(
                        requested=module,
                        extension=extension,
                        action=action,
                        elapsed_ms=round((monotonic() - started) * 1000),
                    )
                )

        return results

    @staticmethod
    def _preview(lines: list[str], *, limit: int = 1024) -> str:
        """Fit a readable list inside one Discord embed field."""
        visible = []
        for index, line in enumerate(lines):
            if len("\n".join((*visible, line))) > limit:
                omitted = len(lines) - index
                marker = f"…and {omitted} more"
                while visible and len("\n".join((*visible, marker))) > limit:
                    visible.pop()
                    omitted += 1
                    marker = f"…and {omitted} more"
                visible.append(marker)
                return "\n".join(visible)
            visible.append(line)
        return "\n".join(visible) or "None"

    @staticmethod
    def _error_file(failures: list[ExtensionResult]) -> discord.File:
        sections = [
            "\n".join(
                (
                    f"Requested: {result.requested}",
                    f"Resolved: {result.extension or 'not found'}",
                    result.traceback or result.error or "Unknown error",
                )
            )
            for result in failures
        ]
        payload = "\n\n".join(sections).encode("utf-8", errors="replace")
        return discord.File(io.BytesIO(payload), filename="reload-errors.txt")

    @staticmethod
    def _add_full_reload_fields(
        embed: discord.Embed, results: list[ExtensionResult]
    ) -> None:
        """Summarize a full reload compactly, grouped by cog category."""
        grouped: dict[str, list[ExtensionResult]] = {}
        for result in results:
            grouped.setdefault(result.category, []).append(result)

        labels = {
            "reloaded": "Reloaded",
            "loaded": "Loaded",
            "failed": "Failed",
        }
        for category, category_results in grouped.items():
            lines = []
            for action in ("reloaded", "loaded", "failed"):
                matching = [
                    result
                    for result in category_results
                    if (result.action if result.succeeded else "failed") == action
                ]
                if matching:
                    names = ", ".join(f"`{result.name}`" for result in matching)
                    lines.append(f"**{labels[action]}:** {names}")
            embed.add_field(
                name=category.replace("_", " ").title(),
                value=Reload._preview(lines),
                inline=False,
            )

    @staticmethod
    def _result_embed(
        ctx,
        results: list[ExtensionResult],
        synced,
        *,
        sync_attempted: bool,
        full_reload: bool = False,
        sync_cooldown: int = 0,
        website_status: Optional[str] = None,
    ) -> discord.Embed:
        successes = [result for result in results if result.succeeded]
        failures = [result for result in results if not result.succeeded]
        website_failed = bool(website_status and website_status.startswith("⚠️"))
        color = (
            colors.red
            if failures and not successes
            else colors.orange
            if failures or website_failed
            else colors.green
        )
        embed = discord.Embed(title="Extension reload", color=color)
        embed.set_author(
            name=ctx.author.display_name,
            icon_url=ctx.author.display_avatar.url,
        )

        if not results:
            embed.description = "No extensions are configured."
            if website_status:
                embed.add_field(name="Website", value=website_status, inline=False)
            return embed

        embed.description = (
            f"**{len(successes)} succeeded** · **{len(failures)} failed**"
        )
        if full_reload:
            Reload._add_full_reload_fields(embed, results)
        else:
            reloaded = [
                f"✅ `{result.name}` — {result.elapsed_ms}ms"
                for result in successes
                if result.action == "reloaded"
            ]
            loaded = [
                f"➕ `{result.name}` — {result.elapsed_ms}ms"
                for result in successes
                if result.action == "loaded"
            ]
            failed = [
                f"❌ `{result.name}` — {discord.utils.escape_markdown(result.error or 'Unknown error')}"
                for result in failures
            ]

            if reloaded:
                embed.add_field(
                    name="Reloaded", value=Reload._preview(reloaded), inline=False
                )
            if loaded:
                embed.add_field(
                    name="Loaded", value=Reload._preview(loaded), inline=False
                )
            if failed:
                embed.add_field(
                    name="Failed", value=Reload._preview(failed), inline=False
                )

        if sync_cooldown:
            embed.add_field(
                name="Application commands",
                value=(
                    "Publishing skipped to avoid repeated Discord updates. "
                    f"Available again in {Reload._format_cooldown(sync_cooldown)}."
                ),
                inline=False,
            )
        elif sync_attempted and synced is None:
            embed.add_field(
                name="Application commands",
                value="⚠️ Sync failed; check the bot console.",
                inline=False,
            )
        elif sync_attempted:
            embed.add_field(
                name="Application commands",
                value=f"Published {len(synced)} global commands.",
                inline=False,
            )
        if website_status:
            embed.add_field(name="Website", value=website_status, inline=False)
        if failures:
            embed.set_footer(text="Full failure details are attached.")
        return embed

    @commands.command(
        name="reload",
        description="Reloads named cogs, or every cog and the website",
        hidden=True,
    )
    @commands.is_owner()
    async def reload(self, ctx, *modules: str):
        full_reload = self._full_reload_requested(modules)
        requested = self._configured_extensions() if full_reload else modules

        async with ctx.typing(), self._operation_lock:
            results = await self._reload_extensions(requested)
            successes = [result for result in results if result.succeeded]
            failures = [result for result in results if not result.succeeded]
            sync_cooldown = (
                self._sync_cooldown_remaining()
                if full_reload and successes
                else 0
            )
            sync_attempted = full_reload and bool(successes) and not sync_cooldown
            synced = None
            if sync_attempted:
                # Keep the cooldown on the bot so it survives reloading this cog.
                self.bot._application_command_sync_at = monotonic()
                synced = await self.bot.sync_application_commands(force=True)
            website_status = None
            if full_reload:
                try:
                    self._refresh_website()
                except Exception as error:
                    summary = "".join(
                        traceback.format_exception_only(type(error), error)
                    ).strip()
                    website_status = (
                        "⚠️ Automatic page refresh failed — "
                        f"{discord.utils.escape_markdown(summary)}"
                    )
                else:
                    website_status = (
                        "✅ Open website pages were notified and will refresh "
                        "automatically."
                    )

        embed = self._result_embed(
            ctx,
            results,
            synced,
            sync_attempted=sync_attempted,
            full_reload=full_reload,
            sync_cooldown=sync_cooldown,
            website_status=website_status,
        )
        file = self._error_file(failures) if failures else None
        await ctx.send(embed=embed, file=file)

    @commands.command(
        name="sync-commands",
        aliases=["sync-slash"],
        description="Publishes application commands to Discord",
        hidden=True,
    )
    @commands.is_owner()
    async def sync_commands(self, ctx):
        async with ctx.typing(), self._operation_lock:
            synced = await self.bot.sync_application_commands(force=True)
        if synced is None:
            return await ctx.send("Slash command sync failed; check the bot console.")
        await ctx.send(f"Published **{len(synced)}** global application commands.")

    @commands.command(
        name="unload",
        description="Unloads a cog and republishes application commands",
        hidden=True,
    )
    @commands.is_owner()
    async def unload(self, ctx, *, module: str):
        extension = self._resolve(module)
        async with ctx.typing(), self._operation_lock:
            try:
                await self.bot.unload_extension(extension)
            except commands.ExtensionNotLoaded:
                return await ctx.send(
                    f"`{extension.removeprefix('cogs.')}` isn't loaded."
                )
            synced = await self.bot.sync_application_commands(force=True)

        sync_status = (
            "Application-command sync failed; check the bot console."
            if synced is None
            else f"Published {len(synced)} global application commands."
        )
        embed = discord.Embed(
            title="Extension unloaded",
            description=f"Disabled `{extension.removeprefix('cogs.')}`.\n{sync_status}",
            color=colors.orange,
        )
        embed.set_author(
            name=ctx.author.display_name,
            icon_url=ctx.author.display_avatar.url,
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Reload(bot), override=True)
