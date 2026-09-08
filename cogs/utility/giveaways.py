"""
cogs.utility.giveaways
~~~~~~~~~~~~~~~~~~~~~~~

Restart-safe, button-powered giveaways with eligibility and winner management.

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import json
import random
import re
import secrets
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from os import path
from typing import Optional

import discord
from discord import Forbidden, NotFound, app_commands
from discord.ext import commands

from botutils import colors, extract_time

MIN_DURATION = 10
MAX_DURATION = 60 * 60 * 24 * 365
MAX_WINNERS = 50
MAX_BONUS_ENTRIES = 100
MAX_AGE_REQUIREMENT_HOURS = 24 * 365 * 20
MAX_PRIZE_LENGTH = 240
HISTORY_DAYS = 30
ENTRY_EMOJI = "🎉"
START_MENTIONS = discord.AllowedMentions(
    users=True,
    roles=True,
    everyone=False,
    replied_user=False,
)
RESULT_MENTIONS = discord.AllowedMentions(
    users=True,
    roles=False,
    everyone=False,
    replied_user=False,
)


def utcnow() -> datetime:
    return discord.utils.utcnow()


def parse_datetime(value: str) -> datetime:
    """Parse current ISO timestamps and the legacy giveaway timestamp format."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def snowflake_from(value: str) -> str:
    matches = re.findall(r"\d{5,}", str(value))
    return matches[-1] if matches else str(value).strip()


class GiveawayEntryView(discord.ui.View):
    def __init__(
        self,
        cog: "Giveaways",
        guild_id: str,
        giveaway_id: str,
        *,
        ended: bool = False,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = str(guild_id)
        self.giveaway_id = str(giveaway_id)
        data = cog.get_giveaway(self.guild_id, self.giveaway_id) or {}
        entries = len(set(data.get("entrants", [])))
        label = f"{entries:,} entr{'y' if entries == 1 else 'ies'}"
        if not ended:
            label = f"Enter • {entries:,}"
        self.entry_button = discord.ui.Button(
            label=label,
            emoji=ENTRY_EMOJI,
            style=discord.ButtonStyle.secondary if ended else discord.ButtonStyle.success,
            custom_id=f"fate:giveaway:entry:{self.guild_id}:{self.giveaway_id}",
            disabled=ended,
        )
        self.entry_button.callback = self.toggle_entry
        self.add_item(self.entry_button)

    async def toggle_entry(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not interaction.guild or not isinstance(member, discord.Member):
            return await interaction.response.send_message(
                "Giveaways can only be entered from their server.", ephemeral=True
            )

        lock = self.cog.entry_locks.setdefault(
            (self.guild_id, self.giveaway_id), asyncio.Lock()
        )
        async with lock:
            data = self.cog.get_giveaway(self.guild_id, self.giveaway_id)
            if not data or data.get("status") != "active":
                return await interaction.response.send_message(
                    "This giveaway has ended.", ephemeral=True
                )
            if utcnow() >= parse_datetime(data["end_at"]):
                self.cog.start_giveaway_task(self.guild_id, self.giveaway_id)
                return await interaction.response.send_message(
                    "This giveaway is ending now.", ephemeral=True
                )
            if error := self.cog.eligibility_error(member, data):
                return await interaction.response.send_message(error, ephemeral=True)

            entrants = {int(user_id) for user_id in data.get("entrants", [])}
            entered = member.id not in entrants
            if entered:
                entrants.add(member.id)
            else:
                entrants.discard(member.id)
            data["entrants"] = sorted(entrants)
            await self.cog.save_data()

            self.entry_button.label = f"Enter • {len(entrants):,}"
            await interaction.response.edit_message(
                embed=self.cog.make_embed(data),
                view=self,
            )
            await interaction.followup.send(
                "You're entered. Good luck!" if entered else "Your entry was removed.",
                ephemeral=True,
            )


class Giveaways(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.path = "./data/userdata/giveaways.json"
        self.data = {}
        if path.isfile(self.path):
            with open(self.path, "r", encoding="utf-8") as file:
                self.data = json.load(file)
        self.data, self._migration_pending = self.normalize_data(self.data)
        self.resume_fetch_lock = asyncio.Lock()
        self.entry_locks = {}
        self.finish_locks = {}
        self.registered_views = []
        self._views_loaded = False
        if "giveaways" not in self.bot.tasks:
            self.bot.tasks["giveaways"] = {}

    @staticmethod
    def normalize_data(payload: dict) -> tuple[dict, bool]:
        """Upgrade reaction-era records while retaining active giveaways."""
        normalized = {}
        changed = False
        cutoff = utcnow() - timedelta(days=HISTORY_DAYS)
        for raw_guild_id, records in (payload or {}).items():
            guild_id = str(raw_guild_id)
            if not isinstance(records, dict):
                changed = True
                continue
            migrated = {}
            for raw_giveaway_id, raw in records.items():
                if not isinstance(raw, dict):
                    changed = True
                    continue
                giveaway_id = str(raw_giveaway_id)
                legacy = "end_at" not in raw
                data = dict(raw)
                data["id"] = giveaway_id
                data["prize"] = str(data.get("prize") or data.get("giveaway") or "Giveaway")[:MAX_PRIZE_LENGTH]
                data["description"] = str(data.get("description") or "")[:1000]
                data["host_id"] = int(data.get("host_id") or data.get("user") or 0)
                data["channel_id"] = int(data.get("channel_id") or data.get("channel") or 0)
                data["message_id"] = int(data.get("message_id") or data.get("message") or 0)
                data["winner_count"] = max(1, min(MAX_WINNERS, int(
                    data.get("winner_count") or data.get("winners") or 1
                )))
                data["end_at"] = str(data.get("end_at") or data.get("end_time") or utcnow().isoformat())
                data["created_at"] = str(data.get("created_at") or utcnow().isoformat())
                data["status"] = data.get("status", "active")
                data["entry_mode"] = data.get("entry_mode", "reaction" if legacy else "button")
                data["entrants"] = sorted({int(user_id) for user_id in data.get("entrants", [])})
                data["winner_ids"] = [int(user_id) for user_id in data.get("winner_ids", [])]
                for key in (
                    "required_role_id",
                    "blocked_role_id",
                    "bonus_role_id",
                    "winner_role_id",
                    "ping_role_id",
                ):
                    data[key] = int(data.get(key) or 0) or None
                data["bonus_entries"] = max(0, min(
                    MAX_BONUS_ENTRIES, int(data.get("bonus_entries") or 0)
                ))
                data["minimum_account_age_hours"] = max(0, min(
                    MAX_AGE_REQUIREMENT_HOURS,
                    int(data.get("minimum_account_age_hours") or 0),
                ))
                data["minimum_server_age_hours"] = max(0, min(
                    MAX_AGE_REQUIREMENT_HOURS,
                    int(data.get("minimum_server_age_hours") or 0),
                ))
                data["image_url"] = data.get("image_url") or None
                data["dm_winners"] = bool(data.get("dm_winners", True))
                if data["status"] == "ended" and data.get("ended_at"):
                    with suppress(ValueError, TypeError):
                        if parse_datetime(data["ended_at"]) < cutoff:
                            changed = True
                            continue
                migrated[giveaway_id] = data
                changed = changed or legacy or data != raw
            if migrated:
                normalized[guild_id] = migrated
            elif records:
                changed = True
        return normalized, changed or normalized != payload

    async def cog_load(self) -> None:
        if self._migration_pending:
            await self.save_data()
            self._migration_pending = False
        self.register_persistent_views()
        if self.bot.is_ready():
            await self.resume_tasks()

    async def cog_unload(self) -> None:
        for view in self.registered_views:
            with suppress(Exception):
                self.bot.remove_view(view)
        self.registered_views.clear()
        tasks = []
        for task_id, task in list(self.bot.tasks.get("giveaways", {}).items()):
            if not task.done():
                task.cancel()
            tasks.append(task)
            self.bot.tasks["giveaways"].pop(task_id, None)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def register_persistent_views(self) -> None:
        if self._views_loaded:
            return
        for guild_id, records in self.data.items():
            for giveaway_id, data in records.items():
                if data.get("status") != "active" or data.get("entry_mode") != "button":
                    continue
                view = GiveawayEntryView(self, guild_id, giveaway_id)
                self.bot.add_view(view, message_id=int(data["message_id"]))
                self.registered_views.append(view)
        self._views_loaded = True

    def get_giveaway(self, guild_id, giveaway_id) -> Optional[dict]:
        return self.data.get(str(guild_id), {}).get(str(giveaway_id))

    def resolve_giveaway(self, guild_id, query: str) -> tuple[Optional[str], Optional[dict]]:
        lookup = snowflake_from(query)
        for giveaway_id, data in self.data.get(str(guild_id), {}).items():
            if giveaway_id == lookup or str(data.get("message_id")) == lookup:
                return giveaway_id, data
        return None, None

    async def save_data(self) -> None:
        async with self.bot.utils.open(self.path, "w+") as file:
            await file.write(json.dumps(self.data, separators=(",", ":")))

    async def remove_giveaway(self, guild_id, giveaway_id) -> None:
        records = self.data.get(str(guild_id))
        if records is None:
            return
        records.pop(str(giveaway_id), None)
        if not records:
            self.data.pop(str(guild_id), None)
        await self.save_data()

    def start_giveaway_task(self, guild_id, giveaway_id):
        guild_id = str(guild_id)
        giveaway_id = str(giveaway_id)
        data = self.get_giveaway(guild_id, giveaway_id)
        if not data or data.get("status") != "active":
            return None
        registry = self.bot.tasks.setdefault("giveaways", {})
        task_id = f"giveaway-{guild_id}-{giveaway_id}"
        current = registry.get(task_id)
        if current and not current.done():
            return current
        task = self.bot.loop.create_task(self.run_giveaway(guild_id, giveaway_id))
        registry[task_id] = task

        def cleanup(finished):
            if self.bot.tasks.get("giveaways", {}).get(task_id) is finished:
                self.bot.tasks["giveaways"].pop(task_id, None)
            if not finished.cancelled() and (error := finished.exception()) is not None:
                self.bot.loop.call_exception_handler({
                    "message": f"Giveaway task {task_id} failed",
                    "exception": error,
                    "task": finished,
                })

        task.add_done_callback(cleanup)
        return task

    async def fetch_message(self, data: dict) -> tuple[discord.abc.Messageable, discord.Message]:
        channel = self.bot.get_channel(int(data["channel_id"]))
        if channel is None:
            channel = await self.bot.fetch_channel(int(data["channel_id"]))
        message = await channel.fetch_message(int(data["message_id"]))
        return channel, message

    def eligibility_error(self, member: discord.Member, data: dict) -> Optional[str]:
        if member.bot:
            return "Bots cannot enter giveaways."
        role_ids = {role.id for role in member.roles}
        if required := data.get("required_role_id"):
            if required not in role_ids:
                return f"You need <@&{required}> to enter this giveaway."
        if blocked := data.get("blocked_role_id"):
            if blocked in role_ids:
                return "One of your roles is excluded from this giveaway."
        now = utcnow()
        account_hours = int(data.get("minimum_account_age_hours") or 0)
        if account_hours and member.created_at > now - timedelta(hours=account_hours):
            return f"Your Discord account must be at least {account_hours:,} hours old."
        server_hours = int(data.get("minimum_server_age_hours") or 0)
        if server_hours and (
            member.joined_at is None
            or member.joined_at > now - timedelta(hours=server_hours)
        ):
            return f"You must be in this server for at least {server_hours:,} hours."
        return None

    def entry_weight(self, member: discord.Member, data: dict) -> int:
        bonus_role_id = data.get("bonus_role_id")
        if bonus_role_id and any(role.id == bonus_role_id for role in member.roles):
            return 1 + int(data.get("bonus_entries") or 0)
        return 1

    def draw_winners(
        self,
        members: list[discord.Member],
        data: dict,
        count: int,
        *,
        exclude_ids=(),
    ) -> list[discord.Member]:
        excluded = {int(user_id) for user_id in exclude_ids}
        pool = [
            member for member in members
            if member.id not in excluded and not self.eligibility_error(member, data)
        ]
        if not pool and excluded:
            pool = [member for member in members if not self.eligibility_error(member, data)]
        winners = []
        rng = random.SystemRandom()
        while pool and len(winners) < count:
            weights = [self.entry_weight(member, data) for member in pool]
            winner = rng.choices(pool, weights=weights, k=1)[0]
            winners.append(winner)
            pool.remove(winner)
        return winners

    async def entrant_members(
        self,
        guild: discord.Guild,
        data: dict,
        message: Optional[discord.Message] = None,
    ) -> list[discord.Member]:
        entrant_ids = {int(user_id) for user_id in data.get("entrants", [])}
        if data.get("entry_mode") == "reaction" and message:
            for reaction in message.reactions:
                if str(reaction.emoji) == ENTRY_EMOJI:
                    entrant_ids = {
                        user.id async for user in reaction.users()
                        if not user.bot
                    }
                    data["entrants"] = sorted(entrant_ids)
                    data["entry_mode"] = "button"
                    break
        members = []
        for user_id in entrant_ids:
            member = guild.get_member(user_id)
            if member is None:
                with suppress(NotFound, Forbidden, discord.HTTPException):
                    member = await guild.fetch_member(user_id)
            if member:
                members.append(member)
        return members

    def make_embed(self, data: dict) -> discord.Embed:
        ended = data.get("status") == "ended"
        end_at = parse_datetime(data["end_at"])
        description = data.get("description") or (
            "This giveaway has ended." if ended
            else "Press the button below to enter. Press it again to withdraw."
        )
        embed = discord.Embed(
            title=f"{ENTRY_EMOJI} {data['prize']}",
            description=description,
            color=0x747F8D if ended else colors.fate,
            timestamp=end_at,
        )
        embed.add_field(
            name="Ended" if ended else "Ends",
            value=discord.utils.format_dt(end_at, style="R"),
        )
        embed.add_field(name="Winners", value=f"{int(data['winner_count']):,}")
        embed.add_field(name="Hosted by", value=f"<@{int(data['host_id'])}>")
        embed.add_field(
            name="Entries",
            value=f"{len(set(data.get('entrants', []))):,}",
        )

        rules = []
        if role_id := data.get("required_role_id"):
            rules.append(f"Requires <@&{role_id}>")
        if role_id := data.get("blocked_role_id"):
            rules.append(f"Excludes <@&{role_id}>")
        if hours := data.get("minimum_account_age_hours"):
            rules.append(f"Account age: {hours:,}h")
        if hours := data.get("minimum_server_age_hours"):
            rules.append(f"Server age: {hours:,}h")
        if rules:
            embed.add_field(name="Eligibility", value="\n".join(rules), inline=False)
        if role_id := data.get("bonus_role_id"):
            embed.add_field(
                name="Bonus entries",
                value=f"<@&{role_id}> gets +{int(data.get('bonus_entries') or 0):,}",
                inline=False,
            )
        if ended:
            winner_ids = data.get("winner_ids", [])
            visible_winners = winner_ids[:30]
            winner_text = " ".join(f"<@{user_id}>" for user_id in visible_winners)
            if len(winner_ids) > len(visible_winners):
                winner_text += f"\n…and {len(winner_ids) - len(visible_winners):,} more"
            embed.add_field(
                name="Selected winners",
                value=winner_text if winner_ids else "No eligible entries",
                inline=False,
            )
        if data.get("image_url"):
            embed.set_image(url=data["image_url"])
        embed.set_footer(text=f"Giveaway ID: {data['id']} • Fate Giveaways")
        return embed

    async def reward_winners(self, data: dict, winners: list[discord.Member]) -> None:
        winner_role_id = data.get("winner_role_id")
        role = winners[0].guild.get_role(winner_role_id) if winners and winner_role_id else None
        for winner in winners:
            if role:
                with suppress(Forbidden, discord.HTTPException):
                    await winner.add_roles(role, reason=f"Won giveaway {data['id']}")
            if data.get("dm_winners", True):
                with suppress(Forbidden, discord.HTTPException):
                    await winner.send(
                        f"You won **{data['prize']}** in **{winner.guild.name}**!"
                    )

    async def finish_giveaway(self, guild_id, giveaway_id) -> list[discord.Member]:
        guild_id = str(guild_id)
        giveaway_id = str(giveaway_id)
        lock = self.finish_locks.setdefault((guild_id, giveaway_id), asyncio.Lock())
        async with lock:
            data = self.get_giveaway(guild_id, giveaway_id)
            if not data or data.get("status") != "active":
                return []
            try:
                async with self.resume_fetch_lock:
                    channel, message = await self.fetch_message(data)
            except (NotFound, Forbidden):
                await self.remove_giveaway(guild_id, giveaway_id)
                return []

            guild = self.bot.get_guild(int(guild_id)) or getattr(channel, "guild", None)
            if guild is None:
                await self.remove_giveaway(guild_id, giveaway_id)
                return []
            members = await self.entrant_members(guild, data, message)
            winners = self.draw_winners(members, data, int(data["winner_count"]))
            data["status"] = "ended"
            data["ended_at"] = utcnow().isoformat()
            data["winner_ids"] = [winner.id for winner in winners]
            await self.save_data()

            ended_view = GiveawayEntryView(self, guild_id, giveaway_id, ended=True)
            with suppress(NotFound, Forbidden, discord.HTTPException):
                await message.edit(embed=self.make_embed(data), view=ended_view)
            if winners:
                mentions = " ".join(winner.mention for winner in winners)
                content = f"{ENTRY_EMOJI} Congratulations {mentions}! You won **{data['prize']}**."
            else:
                content = f"{ENTRY_EMOJI} **{data['prize']}** ended with no eligible entries."
            with suppress(Forbidden, discord.HTTPException):
                await channel.send(content, allowed_mentions=RESULT_MENTIONS)
            await self.reward_winners(data, winners)
            return winners

    async def run_giveaway(self, guild_id, giveaway_id) -> None:
        while data := self.get_giveaway(guild_id, giveaway_id):
            if data.get("status") != "active":
                return
            remaining = (parse_datetime(data["end_at"]) - utcnow()).total_seconds()
            if remaining <= 0:
                try:
                    await self.finish_giveaway(guild_id, giveaway_id)
                    return
                except discord.HTTPException:
                    # Discord.py already handles route rate limits. A remaining
                    # transient gateway/API failure should retry instead of
                    # leaving a persisted giveaway stranded until a restart.
                    await asyncio.sleep(60)
                    continue
            await asyncio.sleep(min(remaining, 3600))

    @commands.Cog.listener("on_ready")
    async def resume_tasks(self) -> None:
        self.register_persistent_views()
        for guild_id, records in list(self.data.items()):
            for giveaway_id, data in list(records.items()):
                if data.get("status") == "active":
                    self.start_giveaway_task(guild_id, giveaway_id)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        changed = False
        role_fields = (
            "required_role_id",
            "blocked_role_id",
            "bonus_role_id",
            "winner_role_id",
            "ping_role_id",
        )
        for data in self.data.get(str(role.guild.id), {}).values():
            for field in role_fields:
                if data.get(field) == role.id:
                    data[field] = None
                    changed = True
        if changed:
            await self.save_data()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        guild_id = str(channel.guild.id)
        records = self.data.get(guild_id, {})
        removed = [
            giveaway_id for giveaway_id, data in records.items()
            if data.get("channel_id") == channel.id
        ]
        for giveaway_id in removed:
            task_id = f"giveaway-{guild_id}-{giveaway_id}"
            task = self.bot.tasks.get("giveaways", {}).pop(task_id, None)
            if task and not task.done():
                task.cancel()
            records.pop(giveaway_id, None)
        if removed:
            if not records:
                self.data.pop(guild_id, None)
            await self.save_data()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        guild_id = str(guild.id)
        records = self.data.pop(guild_id, {})
        for giveaway_id in records:
            task_id = f"giveaway-{guild_id}-{giveaway_id}"
            task = self.bot.tasks.get("giveaways", {}).pop(task_id, None)
            if task and not task.done():
                task.cancel()
        if records:
            await self.save_data()

    @commands.hybrid_group(
        name="giveaway",
        aliases=["giveaways", "gw"],
        fallback="view",
        invoke_without_command=True,
        description="Create and manage button-powered giveaways",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def giveaway(self, ctx: commands.Context) -> None:
        await self._send_list(ctx)

    async def _send_list(self, ctx: commands.Context) -> None:
        records = list(self.data.get(str(ctx.guild.id), {}).values())
        records.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        active = [item for item in records if item.get("status") == "active"]
        ended = [item for item in records if item.get("status") == "ended"]
        embed = discord.Embed(
            title="Giveaways",
            description=(
                "Use `/giveaway create` or the prefix equivalent to start one. "
                "Entries use a persistent button and survive restarts."
            ),
            color=colors.fate,
        )
        for label, items in (("Active", active[:10]), ("Recently ended", ended[:5])):
            lines = []
            for data in items:
                channel = f"<#{data['channel_id']}>"
                jump = f"https://discord.com/channels/{ctx.guild.id}/{data['channel_id']}/{data['message_id']}"
                when = discord.utils.format_dt(parse_datetime(data["end_at"]), style="R")
                prize = data["prize"][:60]
                lines.append(f"[`{data['id']}`]({jump}) **{prize}** • {channel} • {when}")
            embed.add_field(
                name=f"{label} ({len(active) if label == 'Active' else len(ended)})",
                value="\n".join(lines) if lines else "None",
                inline=False,
            )
        await ctx.send(embed=embed, ephemeral=bool(ctx.interaction))

    @giveaway.command(name="create", description="Starts a giveaway with the essential options")
    @app_commands.describe(
        channel="Channel that will host the giveaway",
        winners="Number of unique winners (1-50)",
        duration="Length such as 30m, 12h, or 7d",
        prize="Giveaway title or prize",
    )
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(send_messages=True, embed_links=True)
    async def create_giveaway(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        winners: int,
        duration: str,
        *,
        prize: str,
    ) -> None:
        await self._create_giveaway(ctx, channel, winners, duration, prize=prize)

    @giveaway.command(
        name="advanced",
        description="Starts a giveaway with eligibility, bonus, image, and reward options",
    )
    @app_commands.describe(
        channel="Channel that will host the giveaway",
        winners="Number of unique winners (1-50)",
        duration="Length such as 30m, 12h, or 7d",
        required_role="Role members must have to enter",
        blocked_role="Role that cannot enter",
        bonus_role="Role that receives weighted bonus entries",
        bonus_entries="Extra entries granted by the bonus role",
        winner_role="Role awarded to winners",
        ping_role="Role mentioned when the giveaway starts",
        minimum_account_age_hours="Minimum Discord account age in hours",
        minimum_server_age_hours="Minimum time in this server in hours",
        image_url="Optional HTTPS image shown in the giveaway",
        description="Optional prize details or terms",
        dm_winners="Send each winner a direct message",
        prize="Giveaway title or prize",
    )
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(send_messages=True, embed_links=True)
    async def create_advanced_giveaway(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        winners: int,
        duration: str,
        required_role: Optional[discord.Role] = None,
        blocked_role: Optional[discord.Role] = None,
        bonus_role: Optional[discord.Role] = None,
        bonus_entries: int = 0,
        winner_role: Optional[discord.Role] = None,
        ping_role: Optional[discord.Role] = None,
        minimum_account_age_hours: int = 0,
        minimum_server_age_hours: int = 0,
        image_url: Optional[str] = None,
        description: Optional[str] = None,
        dm_winners: bool = True,
        *,
        prize: str,
    ) -> None:
        await self._create_giveaway(
            ctx,
            channel,
            winners,
            duration,
            required_role=required_role,
            blocked_role=blocked_role,
            bonus_role=bonus_role,
            bonus_entries=bonus_entries,
            winner_role=winner_role,
            ping_role=ping_role,
            minimum_account_age_hours=minimum_account_age_hours,
            minimum_server_age_hours=minimum_server_age_hours,
            image_url=image_url,
            description=description,
            dm_winners=dm_winners,
            prize=prize,
        )

    async def _create_giveaway(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        winners: int,
        duration: str,
        required_role: Optional[discord.Role] = None,
        blocked_role: Optional[discord.Role] = None,
        bonus_role: Optional[discord.Role] = None,
        bonus_entries: int = 0,
        winner_role: Optional[discord.Role] = None,
        ping_role: Optional[discord.Role] = None,
        minimum_account_age_hours: int = 0,
        minimum_server_age_hours: int = 0,
        image_url: Optional[str] = None,
        description: Optional[str] = None,
        dm_winners: bool = True,
        *,
        prize: str,
    ) -> None:
        await ctx.defer(ephemeral=bool(ctx.interaction))
        seconds = extract_time(duration)
        if not seconds or not MIN_DURATION <= seconds <= MAX_DURATION:
            return await ctx.send("Choose a duration from `10s` through `365d`.", ephemeral=bool(ctx.interaction))
        if not 1 <= winners <= MAX_WINNERS:
            return await ctx.send(f"Choose between 1 and {MAX_WINNERS} winners.", ephemeral=bool(ctx.interaction))
        prize = prize.strip()
        if not prize or len(prize) > MAX_PRIZE_LENGTH:
            return await ctx.send(
                f"The prize must be between 1 and {MAX_PRIZE_LENGTH} characters.",
                ephemeral=bool(ctx.interaction),
            )
        if not 0 <= bonus_entries <= MAX_BONUS_ENTRIES:
            return await ctx.send(f"Bonus entries must be between 0 and {MAX_BONUS_ENTRIES}.", ephemeral=bool(ctx.interaction))
        if bonus_entries and bonus_role is None:
            return await ctx.send("Choose a bonus role when adding bonus entries.", ephemeral=bool(ctx.interaction))
        if not (
            0 <= minimum_account_age_hours <= MAX_AGE_REQUIREMENT_HOURS
            and 0 <= minimum_server_age_hours <= MAX_AGE_REQUIREMENT_HOURS
        ):
            return await ctx.send(
                f"Age requirements must be between 0 and {MAX_AGE_REQUIREMENT_HOURS:,} hours.",
                ephemeral=bool(ctx.interaction),
            )
        if image_url and not image_url.casefold().startswith("https://"):
            return await ctx.send("The image must use an `https://` URL.", ephemeral=bool(ctx.interaction))
        description = (description or "").strip()
        if len(description) > 1000:
            return await ctx.send("Prize details cannot exceed 1,000 characters.", ephemeral=bool(ctx.interaction))
        for role, label in (
            (required_role, "required"),
            (blocked_role, "blocked"),
            (bonus_role, "bonus"),
            (winner_role, "winner"),
            (ping_role, "ping"),
        ):
            if role and role.is_default():
                return await ctx.send(f"Choose a specific {label} role, not @everyone.", ephemeral=bool(ctx.interaction))
        if required_role and blocked_role and required_role.id == blocked_role.id:
            return await ctx.send("The required and blocked roles cannot be the same.", ephemeral=bool(ctx.interaction))
        if winner_role and winner_role >= ctx.guild.me.top_role:
            return await ctx.send("Move Fate's role above the winner role first.", ephemeral=bool(ctx.interaction))
        if winner_role and not ctx.guild.me.guild_permissions.manage_roles:
            return await ctx.send("I need Manage Roles to award the winner role.", ephemeral=bool(ctx.interaction))
        if ping_role and not (
            ping_role.mentionable or ctx.guild.me.guild_permissions.manage_roles
        ):
            return await ctx.send("Make the ping role mentionable or give me Manage Roles.", ephemeral=bool(ctx.interaction))

        permissions = channel.permissions_for(ctx.guild.me)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
            and permissions.read_message_history
        ):
            return await ctx.send(
                f"I need View Channel, Send Messages, Embed Links, and Read Message History in {channel.mention}.",
                ephemeral=bool(ctx.interaction),
            )

        guild_id = str(ctx.guild.id)
        records = self.data.setdefault(guild_id, {})
        giveaway_id = secrets.token_hex(3)
        while giveaway_id in records:
            giveaway_id = secrets.token_hex(3)
        now = utcnow()
        data = {
            "id": giveaway_id,
            "status": "active",
            "entry_mode": "button",
            "prize": prize,
            "description": description,
            "host_id": ctx.author.id,
            "channel_id": channel.id,
            "message_id": 0,
            "winner_count": winners,
            "created_at": now.isoformat(),
            "end_at": (now + timedelta(seconds=seconds)).isoformat(),
            "ended_at": None,
            "entrants": [],
            "winner_ids": [],
            "required_role_id": required_role.id if required_role else None,
            "blocked_role_id": blocked_role.id if blocked_role else None,
            "bonus_role_id": bonus_role.id if bonus_role else None,
            "bonus_entries": bonus_entries,
            "winner_role_id": winner_role.id if winner_role else None,
            "ping_role_id": ping_role.id if ping_role else None,
            "minimum_account_age_hours": minimum_account_age_hours,
            "minimum_server_age_hours": minimum_server_age_hours,
            "image_url": image_url,
            "dm_winners": dm_winners,
        }
        records[giveaway_id] = data
        view = GiveawayEntryView(self, guild_id, giveaway_id)
        content = ping_role.mention if ping_role else None
        try:
            message = await channel.send(
                content=content,
                embed=self.make_embed(data),
                view=view,
                allowed_mentions=START_MENTIONS,
            )
        except (Forbidden, discord.HTTPException):
            records.pop(giveaway_id, None)
            if not records:
                self.data.pop(guild_id, None)
            raise
        data["message_id"] = message.id
        await self.save_data()
        self.registered_views.append(view)
        self.start_giveaway_task(guild_id, giveaway_id)
        await ctx.send(
            f"Started giveaway `{giveaway_id}` in {channel.mention}: {message.jump_url}",
            ephemeral=bool(ctx.interaction),
        )

    @giveaway.command(name="list", description="Lists active and recently ended giveaways")
    @commands.has_permissions(manage_guild=True)
    async def list_giveaways(self, ctx: commands.Context) -> None:
        await self._send_list(ctx)

    @giveaway.command(name="end", description="Ends a giveaway immediately and selects winners")
    @app_commands.describe(giveaway="Giveaway ID, message ID, or message link")
    @commands.has_permissions(manage_guild=True)
    async def end_giveaway(self, ctx: commands.Context, giveaway: str) -> None:
        await ctx.defer(ephemeral=bool(ctx.interaction))
        giveaway_id, data = self.resolve_giveaway(ctx.guild.id, giveaway)
        if not data:
            return await ctx.send("I couldn't find that giveaway.", ephemeral=bool(ctx.interaction))
        if data.get("status") != "active":
            return await ctx.send("That giveaway has already ended.", ephemeral=bool(ctx.interaction))
        winners = await self.finish_giveaway(ctx.guild.id, giveaway_id)
        await ctx.send(
            f"Ended `{giveaway_id}` with {len(winners):,} winner{'s' if len(winners) != 1 else ''}.",
            ephemeral=bool(ctx.interaction),
        )

    @giveaway.command(name="reroll", description="Selects replacement winners")
    @app_commands.describe(
        giveaway="Giveaway ID, message ID, or message link",
        winners="Number of replacement winners",
    )
    @commands.has_permissions(manage_guild=True)
    async def reroll_giveaway(
        self,
        ctx: commands.Context,
        giveaway: str,
        winners: int = 1,
    ) -> None:
        await ctx.defer(ephemeral=bool(ctx.interaction))
        if not 1 <= winners <= MAX_WINNERS:
            return await ctx.send(f"Choose between 1 and {MAX_WINNERS} winners.", ephemeral=bool(ctx.interaction))
        giveaway_id, data = self.resolve_giveaway(ctx.guild.id, giveaway)
        if not data:
            return await ctx.send("I couldn't find that giveaway.", ephemeral=bool(ctx.interaction))
        if data.get("status") != "ended":
            return await ctx.send("End that giveaway before rerolling it.", ephemeral=bool(ctx.interaction))
        try:
            channel, message = await self.fetch_message(data)
        except (NotFound, Forbidden):
            return await ctx.send("The giveaway message is no longer accessible.", ephemeral=bool(ctx.interaction))
        members = await self.entrant_members(ctx.guild, data, message)
        selected = self.draw_winners(
            members,
            data,
            winners,
            exclude_ids=data.get("winner_ids", []),
        )
        if not selected:
            return await ctx.send("There are no eligible entries to reroll.", ephemeral=bool(ctx.interaction))
        data["winner_ids"] = list(dict.fromkeys([
            *data.get("winner_ids", []),
            *(member.id for member in selected),
        ]))
        await self.save_data()
        await channel.send(
            f"{ENTRY_EMOJI} Rerolled **{data['prize']}**: "
            + " ".join(member.mention for member in selected),
            allowed_mentions=RESULT_MENTIONS,
        )
        await self.reward_winners(data, selected)
        with suppress(discord.HTTPException):
            await message.edit(embed=self.make_embed(data))
        await ctx.send(
            f"Selected {len(selected):,} replacement winner{'s' if len(selected) != 1 else ''}.",
            ephemeral=bool(ctx.interaction),
        )

    @giveaway.command(name="delete", description="Deletes a giveaway without selecting winners")
    @app_commands.describe(giveaway="Giveaway ID, message ID, or message link")
    @commands.has_permissions(manage_guild=True)
    async def delete_giveaway(self, ctx: commands.Context, giveaway: str) -> None:
        await ctx.defer(ephemeral=bool(ctx.interaction))
        giveaway_id, data = self.resolve_giveaway(ctx.guild.id, giveaway)
        if not data:
            return await ctx.send("I couldn't find that giveaway.", ephemeral=bool(ctx.interaction))
        task_id = f"giveaway-{ctx.guild.id}-{giveaway_id}"
        task = self.bot.tasks.get("giveaways", {}).pop(task_id, None)
        if task and not task.done():
            task.cancel()
        with suppress(NotFound, Forbidden, discord.HTTPException):
            _channel, message = await self.fetch_message(data)
            await message.delete()
        await self.remove_giveaway(ctx.guild.id, giveaway_id)
        await ctx.send(f"Deleted giveaway `{giveaway_id}`.", ephemeral=bool(ctx.interaction))

    @giveaway.command(name="info", description="Shows giveaway configuration and entry totals")
    @app_commands.describe(giveaway="Giveaway ID, message ID, or message link")
    @commands.has_permissions(manage_guild=True)
    async def giveaway_info(self, ctx: commands.Context, giveaway: str) -> None:
        giveaway_id, data = self.resolve_giveaway(ctx.guild.id, giveaway)
        if not data:
            return await ctx.send("I couldn't find that giveaway.", ephemeral=bool(ctx.interaction))
        embed = self.make_embed(data)
        embed.title = f"Giveaway details • {data['prize']}"
        embed.add_field(name="Status", value=data["status"].title())
        embed.add_field(name="Message", value=(
            f"[Open giveaway](https://discord.com/channels/{ctx.guild.id}/{data['channel_id']}/{data['message_id']})"
        ))
        await ctx.send(embed=embed, ephemeral=bool(ctx.interaction))


async def setup(bot):
    await bot.add_cog(Giveaways(bot), override=True)
