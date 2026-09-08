"""Adaptive raid containment, incident telemetry, and modern controls."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import time
from typing import Any, Deque, Iterable, Mapping, MutableMapping, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from botutils.antiraid import (
    PROTECTION_ORDER,
    account_is_suspicious,
    enabled_protections,
    normalize_config,
    recommended_config,
)
from fate import Fate

MAX_JOIN_RECORDS = 500
MAX_ACTION_RECORDS = 100
MAX_AUDIT_IDS = 5_000
DANGEROUS_ROLE_PERMISSIONS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
    "ban_members",
    "kick_members",
    "moderate_members",
)


@dataclass(slots=True)
class JoinRecord:
    member_id: int
    joined_at: float
    suspicious: bool


@dataclass(slots=True)
class ActionRecord:
    entry_id: int
    action: str
    occurred_at: float
    target_id: Optional[int]


class AntiRaid(commands.Cog):
    """Detect coordinated entry and destructive staff-account raids."""

    def __init__(self, bot: Fate) -> None:
        self.bot = bot
        # Preserve the legacy collection name while migrating its documents.
        self.config = bot.utils.cache("anti_raid")
        self.started_at = time()

        self.join_windows: dict[int, Deque[JoinRecord]] = defaultdict(
            lambda: deque(maxlen=MAX_JOIN_RECORDS)
        )
        self.action_windows: dict[tuple[int, int], Deque[ActionRecord]] = defaultdict(
            lambda: deque(maxlen=MAX_ACTION_RECORDS)
        )
        self.guild_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.processed_audit_entries: dict[int, float] = {}
        self.incident_cooldowns: dict[tuple[int, int, str], float] = {}
        self.recent_incidents: dict[int, Deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=25)
        )
        self.stats: dict[int, dict[str, Any]] = {}
        self.unlock_tasks: dict[int, asyncio.Task] = {}
        self.background_tasks: set[asyncio.Task] = set()

        changed = False
        for _guild_id, guild_config in list(self.config.items()):
            if isinstance(guild_config, MutableMapping):
                changed = normalize_config(guild_config) or changed
        if changed:
            self.track_task(self.bot.loop.create_task(self.config.flush()))

        self.runtime_cleanup.start()
        self.track_task(self.bot.loop.create_task(self._restore_lockdowns()))

    # ------------------------------------------------------------------
    # Lifecycle, configuration, and dashboard state
    # ------------------------------------------------------------------
    def track_task(self, task: asyncio.Task) -> asyncio.Task:
        self.background_tasks.add(task)
        task.add_done_callback(self._background_task_done)
        return task

    def _background_task_done(self, task: asyncio.Task) -> None:
        self.background_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error:
            self.bot.log.error(
                "AntiRaid background task failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def cog_unload(self) -> None:
        self.runtime_cleanup.cancel()
        for task in tuple(self.background_tasks):
            task.cancel()
        for task in tuple(self.unlock_tasks.values()):
            task.cancel()
        self.background_tasks.clear()
        self.unlock_tasks.clear()

    def get_config(
        self, guild_id: int, *, create: bool = False
    ) -> Optional[dict[str, Any]]:
        config = self.config.get(guild_id)
        if config is None and create:
            config = recommended_config(enabled=False)
            self.config[guild_id] = config
        if config is not None and normalize_config(config):
            self.track_task(self.bot.loop.create_task(self.config.flush()))
        return config

    async def replace_config(
        self, guild: discord.Guild, settings: Mapping[str, Any]
    ) -> dict[str, Any]:
        existing = self.get_config(guild.id, create=True)
        assert existing is not None
        runtime = {
            "lockdown_until": existing.get("lockdown_until", 0.0),
            "temporary_bans": list(existing.get("temporary_bans", [])),
        }
        replacement = dict(settings)
        replacement.update(runtime)
        normalize_config(replacement)
        self.config[guild.id] = replacement
        await self.config.flush()
        if not replacement["enabled"] or replacement["mode"] != "enforce":
            await self.unlock_guild(guild, notify=False)
        elif replacement["lockdown_until"] > time():
            self._schedule_unlock(guild.id, replacement["lockdown_until"])
        return replacement

    async def enable_for_guild(self, guild: discord.Guild) -> tuple[dict[str, Any], bool]:
        config = self.get_config(guild.id)
        created = config is None
        if config is None:
            config = recommended_config()
            self.config[guild.id] = config
        else:
            config["enabled"] = True
        await self.config.flush()
        if created:
            await self._module_log(guild, True)
        return config, created

    async def set_enabled(self, guild: discord.Guild, enabled: bool) -> dict[str, Any]:
        config = self.get_config(guild.id, create=True)
        assert config is not None
        changed = config["enabled"] != enabled
        if not enabled:
            await self.unlock_guild(guild, notify=False)
            config = self.get_config(guild.id, create=True)
            assert config is not None
        config["enabled"] = enabled
        await self.config.flush()
        if changed:
            await self._module_log(guild, enabled)
        return config

    async def set_protection(
        self, guild: discord.Guild, protection: str, enabled: bool
    ) -> dict[str, Any]:
        if protection not in PROTECTION_ORDER:
            raise ValueError("Unknown AntiRaid protection")
        config = self.get_config(guild.id, create=True)
        assert config is not None
        config["protections"][protection]["enabled"] = enabled
        if enabled:
            config["enabled"] = True
        await self.config.flush()
        return config

    async def _module_log(self, guild: discord.Guild, enabled: bool) -> None:
        with suppress(Exception):
            await self.bot.create_log(
                message=(
                    f"!{'on' if enabled else 'off'} **Anti Raid** - `{guild}`"
                ),
                channel="module_log",
                embedded=True,
                color="green" if enabled else "red",
            )

    def _stats(self, guild_id: int) -> dict[str, Any]:
        if guild_id not in self.stats:
            self.stats[guild_id] = {
                "started_at": self.started_at,
                "joins": 0,
                "suspicious_joins": 0,
                "destructive_actions": 0,
                "incidents": 0,
                "contained_members": 0,
                "staff_responses": 0,
                "failures": 0,
                "triggers": Counter(),
            }
        return self.stats[guild_id]

    def permission_warnings(
        self, guild: discord.Guild, config: Optional[Mapping[str, Any]] = None
    ) -> list[str]:
        config = config or self.get_config(guild.id) or recommended_config(enabled=False)
        if not config.get("enabled"):
            return []
        protections = config["protections"]
        response = config["response"]
        permissions = guild.me.guild_permissions if guild.me else None
        warnings: list[str] = []
        if not enabled_protections(config):
            warnings.append("Enable at least one AntiRaid defense before relying on protection.")

        def require(permission: str, label: str, purpose: str) -> None:
            if not permissions or not getattr(permissions, permission, False):
                warnings.append(f"Fate needs **{label}** to {purpose}.")

        if protections["destructive_actions"]["enabled"]:
            require("view_audit_log", "View Audit Log", "identify destructive actors")
        if config["mode"] == "enforce":
            if protections["join_burst"]["enabled"] or protections["suspicious_accounts"]["enabled"]:
                permission = "ban_members" if response["join_action"] == "temporary_ban" else "kick_members"
                label = "Ban Members" if permission == "ban_members" else "Kick Members"
                require(permission, label, "contain raid accounts")
            if protections["destructive_actions"]["enabled"]:
                required = {
                    "strip_roles": ("manage_roles", "Manage Roles"),
                    "kick": ("kick_members", "Kick Members"),
                    "ban": ("ban_members", "Ban Members"),
                }[response["staff_action"]]
                require(required[0], required[1], "stop a compromised staff account")

        channel_id = config.get("alert_channel_id")
        if channel_id:
            channel = guild.get_channel(int(channel_id))
            if not channel:
                warnings.append("The saved incident alert channel no longer exists.")
            elif guild.me:
                channel_permissions = channel.permissions_for(guild.me)
                if not channel_permissions.send_messages or not channel_permissions.embed_links:
                    warnings.append(
                        "Fate needs **Send Messages** and **Embed Links** in the incident alert channel."
                    )
        return warnings

    def runtime_snapshot(self, guild_id: int) -> dict[str, Any]:
        config = self.get_config(guild_id) or recommended_config(enabled=False)
        stats = self._stats(guild_id)
        now = time()
        lock_until = float(config.get("lockdown_until", 0.0))
        return {
            "started_at": datetime.fromtimestamp(
                stats["started_at"], tz=timezone.utc
            ).isoformat(),
            "joins": stats["joins"],
            "suspicious_joins": stats["suspicious_joins"],
            "destructive_actions": stats["destructive_actions"],
            "incidents": stats["incidents"],
            "contained_members": stats["contained_members"],
            "staff_responses": stats["staff_responses"],
            "failures": stats["failures"],
            "active_lockdown": lock_until > now,
            "lockdown_until": (
                datetime.fromtimestamp(lock_until, tz=timezone.utc).isoformat()
                if lock_until > now
                else None
            ),
            "temporary_bans": len(config.get("temporary_bans", [])),
            "top_trigger": (
                stats["triggers"].most_common(1)[0][0]
                if stats["triggers"]
                else None
            ),
            "recent": list(self.recent_incidents[guild_id]),
        }

    # ------------------------------------------------------------------
    # Lockdown persistence and join containment
    # ------------------------------------------------------------------
    async def _restore_lockdowns(self) -> None:
        await self.bot.wait_until_ready()
        now = time()
        for guild_id, config in list(self.config.items()):
            guild = self.bot.get_guild(guild_id)
            if not guild or not isinstance(config, MutableMapping):
                continue
            normalize_config(config)
            if config["lockdown_until"] > now:
                self._schedule_unlock(guild_id, config["lockdown_until"])
            elif config["temporary_bans"] or config["lockdown_until"]:
                await self.unlock_guild(guild, notify=False)

    def _schedule_unlock(self, guild_id: int, unlock_at: float) -> None:
        existing = self.unlock_tasks.pop(guild_id, None)
        if existing and existing is not asyncio.current_task():
            existing.cancel()

        async def runner() -> None:
            try:
                await asyncio.sleep(max(0.0, unlock_at - time()))
                guild = self.bot.get_guild(guild_id)
                if guild:
                    await self.unlock_guild(guild, notify=True)
            finally:
                if self.unlock_tasks.get(guild_id) is asyncio.current_task():
                    self.unlock_tasks.pop(guild_id, None)

        task = self.bot.loop.create_task(runner())
        self.unlock_tasks[guild_id] = task
        self.track_task(task)

    async def unlock_guild(self, guild: discord.Guild, *, notify: bool = True) -> int:
        config = self.get_config(guild.id)
        if not config:
            return 0
        task = self.unlock_tasks.pop(guild.id, None)
        if task and task is not asyncio.current_task():
            task.cancel()

        released = 0
        for user_id in list(config.get("temporary_bans", [])):
            try:
                await guild.unban(
                    discord.Object(id=int(user_id)),
                    reason="AntiRaid: temporary raid lockdown ended",
                )
                released += 1
            except discord.NotFound:
                # Already unbanned is the desired final state.
                pass
            except (discord.Forbidden, discord.HTTPException):
                self._stats(guild.id)["failures"] += 1

        was_locked = bool(config.get("lockdown_until") or config.get("temporary_bans"))
        config["lockdown_until"] = 0.0
        config["temporary_bans"] = []
        await self.config.flush()
        if notify and was_locked:
            incident = self._record_incident(
                guild.id,
                "lockdown_cleared",
                "Raid lockdown cleared",
                f"Released {released} temporary ban{'s' if released != 1 else ''}.",
                outcome="Server reopened",
            )
            await self._send_alert(guild, incident, color=discord.Color.green())
        return released

    def _is_trusted(self, member: discord.abc.User, config: Mapping[str, Any]) -> bool:
        guild = getattr(member, "guild", None)
        if guild and member.id == guild.owner_id:
            return True
        if self.bot.user and member.id == self.bot.user.id:
            return True
        if member.id in config.get("trusted_members", []):
            return True
        roles = getattr(member, "roles", ())
        trusted_roles = set(config.get("trusted_roles", []))
        return any(role.id in trusted_roles for role in roles)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        config = self.get_config(member.guild.id)
        if not config or not config["enabled"] or self._is_trusted(member, config):
            return

        async with self.guild_locks[member.guild.id]:
            config = self.get_config(member.guild.id)
            if not config or not config["enabled"]:
                return
            stats = self._stats(member.guild.id)
            stats["joins"] += 1
            protections = config["protections"]
            risky_settings = protections["suspicious_accounts"]
            suspicious = risky_settings["enabled"] and account_is_suspicious(
                member.created_at,
                max_age_hours=risky_settings["max_account_age_hours"],
                has_avatar=member.avatar is not None,
                require_no_avatar=risky_settings["require_no_avatar"],
            )
            if suspicious:
                stats["suspicious_joins"] += 1

            now = time()
            window = self.join_windows[member.guild.id]
            window.append(JoinRecord(member.id, now, suspicious))
            horizon = max(
                protections["join_burst"]["window"],
                risky_settings["window"],
            )
            while window and window[0].joined_at < now - horizon:
                window.popleft()

            lock_until = float(config.get("lockdown_until", 0.0))
            if lock_until > now:
                if config["mode"] == "enforce":
                    await self._contain_joiners(
                        member.guild,
                        config,
                        [member],
                        reason="Active AntiRaid lockdown",
                    )
                return
            if lock_until or config.get("temporary_bans"):
                await self.unlock_guild(member.guild, notify=False)
                config = self.get_config(member.guild.id)
                assert config is not None

            reason: Optional[str] = None
            candidates: list[discord.Member] = []
            join_settings = protections["join_burst"]
            if join_settings["enabled"]:
                recent = [
                    record
                    for record in window
                    if record.joined_at >= now - join_settings["window"]
                ]
                if len(recent) >= join_settings["threshold"]:
                    reason = (
                        f"{len(recent)} joins in {join_settings['window']} seconds"
                    )
                    candidates = [
                        found
                        for record in recent
                        if (found := member.guild.get_member(record.member_id)) is not None
                    ]

            if reason is None and risky_settings["enabled"]:
                risky = [
                    record
                    for record in window
                    if record.suspicious
                    and record.joined_at >= now - risky_settings["window"]
                ]
                if len(risky) >= risky_settings["threshold"]:
                    reason = (
                        f"{len(risky)} high-risk accounts joined in "
                        f"{risky_settings['window']} seconds"
                    )
                    candidates = [
                        found
                        for record in risky
                        if (found := member.guild.get_member(record.member_id)) is not None
                    ]

            if reason:
                await self._activate_lockdown(member.guild, config, reason, candidates)

    async def _activate_lockdown(
        self,
        guild: discord.Guild,
        config: MutableMapping[str, Any],
        reason: str,
        candidates: Iterable[discord.Member],
    ) -> None:
        stats = self._stats(guild.id)
        stats["incidents"] += 1
        trigger = "suspicious_accounts" if "high-risk" in reason else "join_burst"
        stats["triggers"][trigger] += 1

        outcome = "Observed only"
        contained = 0
        if config["mode"] == "enforce":
            unlock_at = time() + config["response"]["lock_minutes"] * 60
            config["lockdown_until"] = unlock_at
            await self.config.flush()
            self._schedule_unlock(guild.id, unlock_at)
            contained = await self._contain_joiners(
                guild,
                config,
                candidates,
                reason=f"AntiRaid: {reason}",
            )
            outcome = (
                f"Lockdown active for {config['response']['lock_minutes']} minutes; "
                f"contained {contained} account{'s' if contained != 1 else ''}"
            )

        incident = self._record_incident(
            guild.id,
            trigger,
            "Join raid detected",
            reason,
            outcome=outcome,
        )
        await self._send_alert(guild, incident, color=discord.Color.red())

    async def _contain_joiners(
        self,
        guild: discord.Guild,
        config: MutableMapping[str, Any],
        members: Iterable[discord.Member],
        *,
        reason: str,
    ) -> int:
        action = config["response"]["join_action"]
        unique = {member.id: member for member in members}
        contained = 0
        temporary_bans = list(config.get("temporary_bans", []))
        for member in unique.values():
            if self._is_trusted(member, config):
                continue
            if guild.me and member.top_role >= guild.me.top_role:
                self._stats(guild.id)["failures"] += 1
                continue
            with suppress(discord.HTTPException):
                await member.send(
                    f"**{guild.name}** entered a temporary AntiRaid lockdown. "
                    "You can rejoin after the lockdown ends."
                )
            try:
                if action == "temporary_ban":
                    await guild.ban(
                        member,
                        reason=reason,
                        delete_message_seconds=0,
                    )
                    if member.id not in temporary_bans:
                        temporary_bans.append(member.id)
                else:
                    await member.kick(reason=reason)
                contained += 1
                self._stats(guild.id)["contained_members"] += 1
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                self._stats(guild.id)["failures"] += 1
        if temporary_bans != config.get("temporary_bans", []):
            config["temporary_bans"] = temporary_bans[-1_000:]
            await self.config.flush()
        return contained

    # ------------------------------------------------------------------
    # Audit-log correlation and compromised staff containment
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if self.bot.user and member.id == self.bot.user.id:
            return
        await self._resolve_audit_event(
            member.guild,
            target_id=member.id,
            actions=(discord.AuditLogAction.ban, discord.AuditLogAction.kick),
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self._resolve_audit_event(
            channel.guild,
            target_id=channel.id,
            actions=(discord.AuditLogAction.channel_delete,),
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self._resolve_audit_event(
            role.guild,
            target_id=role.id,
            actions=(discord.AuditLogAction.role_delete,),
        )

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        action = getattr(discord.AuditLogAction, "webhook_delete", None)
        if action is not None:
            await self._resolve_audit_event(
                channel.guild,
                target_id=None,
                actions=(action,),
            )

    @staticmethod
    def _watch_key(action: discord.AuditLogAction) -> tuple[str, str]:
        mapping = {
            discord.AuditLogAction.ban: ("watch_bans", "ban"),
            discord.AuditLogAction.kick: ("watch_kicks", "kick"),
            discord.AuditLogAction.channel_delete: ("watch_channels", "channel delete"),
            discord.AuditLogAction.role_delete: ("watch_roles", "role delete"),
        }
        webhook_delete = getattr(discord.AuditLogAction, "webhook_delete", None)
        if webhook_delete is not None:
            mapping[webhook_delete] = ("watch_webhooks", "webhook delete")
        return mapping[action]

    async def _resolve_audit_event(
        self,
        guild: discord.Guild,
        *,
        target_id: Optional[int],
        actions: tuple[discord.AuditLogAction, ...],
    ) -> None:
        config = self.get_config(guild.id)
        if not config or not config["enabled"]:
            return
        settings = config["protections"]["destructive_actions"]
        if not settings["enabled"]:
            return
        actions = tuple(
            action for action in actions if settings[self._watch_key(action)[0]]
        )
        if not actions or not guild.me or not guild.me.guild_permissions.view_audit_log:
            return

        entry: Optional[discord.AuditLogEntry] = None
        for delay in (0.35, 0.8, 1.4):
            await asyncio.sleep(delay)
            after = datetime.now(timezone.utc) - timedelta(seconds=12)
            try:
                async for candidate in guild.audit_logs(limit=10, after=after):
                    if candidate.action not in actions:
                        continue
                    if candidate.id in self.processed_audit_entries:
                        continue
                    candidate_target_id = getattr(candidate.target, "id", None)
                    if target_id is not None and candidate_target_id != target_id:
                        continue
                    entry = candidate
                    break
            except (discord.Forbidden, discord.HTTPException, ValueError):
                return
            if entry:
                break
        if not entry or not entry.user:
            return

        self.processed_audit_entries[entry.id] = time()
        actor = guild.get_member(entry.user.id)
        if actor is None:
            with suppress(discord.Forbidden, discord.NotFound, discord.HTTPException):
                actor = await guild.fetch_member(entry.user.id)
        if actor is None or self._is_trusted(actor, config):
            return

        _watch, action_label = self._watch_key(entry.action)
        now = time()
        key = (guild.id, actor.id)
        window = self.action_windows[key]
        window.append(ActionRecord(entry.id, action_label, now, target_id))
        while window and window[0].occurred_at < now - settings["window"]:
            window.popleft()
        self._stats(guild.id)["destructive_actions"] += 1
        if len(window) < settings["threshold"]:
            return

        cooldown_key = (guild.id, actor.id, "destructive_actions")
        if now - self.incident_cooldowns.get(cooldown_key, 0.0) < settings["window"]:
            return
        self.incident_cooldowns[cooldown_key] = now
        await self._respond_to_staff_raid(guild, config, actor, list(window))

    async def _respond_to_staff_raid(
        self,
        guild: discord.Guild,
        config: Mapping[str, Any],
        actor: discord.Member,
        records: list[ActionRecord],
    ) -> None:
        stats = self._stats(guild.id)
        stats["incidents"] += 1
        stats["triggers"]["destructive_actions"] += 1
        summary = Counter(record.action for record in records)
        evidence = ", ".join(
            f"{count} {label}{'' if count == 1 else 's'}"
            for label, count in summary.items()
        )
        outcome = "Observed only"

        if config["mode"] == "enforce":
            outcome = await self._apply_staff_response(
                guild, actor, config["response"]["staff_action"]
            )

        incident = self._record_incident(
            guild.id,
            "destructive_actions",
            "Destructive action burst",
            f"{actor} performed {evidence} inside the configured window.",
            outcome=outcome,
            actor_id=actor.id,
        )
        await self._send_alert(guild, incident, color=discord.Color.dark_red())

    async def _apply_staff_response(
        self, guild: discord.Guild, actor: discord.Member, action: str
    ) -> str:
        if actor.id == guild.owner_id or not guild.me or actor.top_role >= guild.me.top_role:
            self._stats(guild.id)["failures"] += 1
            return "Could not contain actor because of Discord role hierarchy"
        try:
            if action == "strip_roles":
                dangerous = [
                    role
                    for role in actor.roles
                    if not role.is_default()
                    and not role.managed
                    and role < guild.me.top_role
                    and any(
                        getattr(role.permissions, permission, False)
                        for permission in DANGEROUS_ROLE_PERMISSIONS
                    )
                ]
                if not dangerous:
                    return "No removable dangerous roles were found"
                await actor.remove_roles(
                    *dangerous,
                    reason="AntiRaid: destructive staff-account activity",
                )
                result = f"Removed {len(dangerous)} dangerous role{'s' if len(dangerous) != 1 else ''}"
            elif action == "kick":
                await actor.kick(reason="AntiRaid: destructive staff-account activity")
                result = "Kicked the suspected compromised account"
            else:
                await guild.ban(
                    actor,
                    reason="AntiRaid: destructive staff-account activity",
                    delete_message_seconds=0,
                )
                result = "Banned the suspected compromised account"
            self._stats(guild.id)["staff_responses"] += 1
            return result
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            self._stats(guild.id)["failures"] += 1
            return "Containment failed; verify Fate's permissions and role position"

    # ------------------------------------------------------------------
    # Incidents, alerts, and bounded runtime cleanup
    # ------------------------------------------------------------------
    def _record_incident(
        self,
        guild_id: int,
        trigger: str,
        title: str,
        detail: str,
        *,
        outcome: str,
        actor_id: Optional[int] = None,
    ) -> dict[str, Any]:
        incident = {
            "trigger": trigger,
            "title": title,
            "detail": detail,
            "outcome": outcome,
            "actor_id": str(actor_id) if actor_id else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.recent_incidents[guild_id].appendleft(incident)
        return incident

    def _alert_channel(
        self, guild: discord.Guild, config: Mapping[str, Any]
    ) -> Optional[discord.abc.Messageable]:
        configured = config.get("alert_channel_id")
        candidates = [
            guild.get_channel(int(configured)) if configured else None,
            guild.system_channel,
            getattr(guild, "public_updates_channel", None),
            *guild.text_channels,
        ]
        for channel in candidates:
            if channel is None or not guild.me:
                continue
            permissions = channel.permissions_for(guild.me)
            if permissions.send_messages and permissions.embed_links:
                return channel
        return None

    async def _send_alert(
        self,
        guild: discord.Guild,
        incident: Mapping[str, Any],
        *,
        color: discord.Color,
    ) -> None:
        config = self.get_config(guild.id)
        if not config:
            return
        channel = self._alert_channel(guild, config)
        if not channel:
            return
        embed = discord.Embed(
            title=f"AntiRaid · {incident['title']}",
            description=incident["detail"],
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Response", value=incident["outcome"], inline=False)
        if actor_id := incident.get("actor_id"):
            embed.add_field(name="Actor", value=f"<@{actor_id}> · `{actor_id}`")
        embed.set_footer(text="Review the incident before reversing moderation actions")
        with suppress(discord.Forbidden, discord.HTTPException):
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @tasks.loop(minutes=5)
    async def runtime_cleanup(self) -> None:
        now = time()
        for guild_id, records in list(self.join_windows.items()):
            while records and records[0].joined_at < now - 600:
                records.popleft()
            if not records:
                self.join_windows.pop(guild_id, None)
        for key, records in list(self.action_windows.items()):
            while records and records[0].occurred_at < now - 300:
                records.popleft()
            if not records:
                self.action_windows.pop(key, None)
        self.processed_audit_entries = {
            entry_id: timestamp
            for entry_id, timestamp in self.processed_audit_entries.items()
            if timestamp >= now - 600
        }
        if len(self.processed_audit_entries) > MAX_AUDIT_IDS:
            newest = sorted(
                self.processed_audit_entries.items(), key=lambda item: item[1], reverse=True
            )[:MAX_AUDIT_IDS]
            self.processed_audit_entries = dict(newest)
        self.incident_cooldowns = {
            key: timestamp
            for key, timestamp in self.incident_cooldowns.items()
            if timestamp >= now - 600
        }

    @runtime_cleanup.before_loop
    async def before_runtime_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Commands and the Components V2 control center
    # ------------------------------------------------------------------
    @commands.hybrid_group(
        name="anti-raid",
        aliases=["antiraid", "anti_raid"],
        fallback="view",
        invoke_without_command=True,
        description="Opens the raid-protection control center",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def anti_raid(self, ctx: commands.Context) -> None:
        """Open the complete AntiRaid dashboard."""
        from cogs.moderation.antiraid_ui import AntiRaidDashboard

        await AntiRaidDashboard(self, ctx).start()

    @anti_raid.command(
        name="configure",
        aliases=["config"],
        description="Opens raid-protection settings",
    )
    @commands.has_permissions(manage_guild=True)
    async def configure(self, ctx: commands.Context) -> None:
        from cogs.moderation.antiraid_ui import AntiRaidDashboard

        dashboard = AntiRaidDashboard(self, ctx)
        dashboard.page = "join_defense"
        await dashboard.start()

    @anti_raid.command(name="enable", description="Enables Raid Protection")
    @commands.has_permissions(manage_guild=True)
    async def enable(self, ctx: commands.Context) -> None:
        config, created = await self.enable_for_guild(ctx.guild)
        warnings = self.permission_warnings(ctx.guild, config)
        message = (
            f"Raid Protection enabled with {len(enabled_protections(config))} active "
            f"defense{'s' if len(enabled_protections(config)) != 1 else ''}."
        )
        if warnings:
            message += f" Open the control center to resolve {len(warnings)} readiness warning{'s' if len(warnings) != 1 else ''}."
        await ctx.send(message, ephemeral=bool(ctx.interaction))

    @anti_raid.command(name="disable", description="Disables Raid Protection")
    @commands.has_permissions(manage_guild=True)
    async def disable(self, ctx: commands.Context) -> None:
        await self.set_enabled(ctx.guild, False)
        await ctx.send(
            "Raid Protection is disabled. Its tuned settings remain saved.",
            ephemeral=bool(ctx.interaction),
        )

    @anti_raid.command(name="unlock", description="Ends the current raid lockdown")
    @commands.has_permissions(manage_guild=True)
    async def unlock(self, ctx: commands.Context) -> None:
        config = self.get_config(ctx.guild.id)
        if not config or not config.get("lockdown_until"):
            return await ctx.send(
                "This server is not currently locked down.",
                ephemeral=bool(ctx.interaction),
            )
        released = await self.unlock_guild(ctx.guild, notify=True)
        await ctx.send(
            f"Lockdown cleared and {released} temporary ban{'s' if released != 1 else ''} released.",
            ephemeral=bool(ctx.interaction),
        )

    @anti_raid.command(name="status", description="Shows AntiRaid health and incidents")
    @commands.has_permissions(manage_guild=True)
    async def status(self, ctx: commands.Context) -> None:
        from cogs.moderation.antiraid_ui import AntiRaidDashboard

        dashboard = AntiRaidDashboard(self, ctx)
        dashboard.page = "activity"
        await dashboard.start()


async def setup(bot: Fate) -> None:
    await bot.add_cog(AntiRaid(bot), override=True)
