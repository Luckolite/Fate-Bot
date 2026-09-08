"""Discord Components V2 control center for AntiRaid."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any, Mapping, Optional

import discord
from discord import ui
from discord.ext import commands

from botutils import colors
from botutils.antiraid import (
    PROTECTION_INFO,
    PROTECTION_ORDER,
    enabled_protections,
    format_duration,
    recommended_config,
)

if TYPE_CHECKING:
    from cogs.moderation.anti_raid import AntiRaid


PAGES = {
    "overview": ("Overview", "🛡️", "See what AntiRaid can do for your server"),
    "join_defense": ("Join defense", "🌊", "Tune member-flood and new-account signals"),
    "staff_guard": ("Staff guard", "🔐", "Protect against compromised moderator accounts"),
    "response": ("Response", "⚡", "Choose containment, alerts, and trusted bypasses"),
    "activity": ("Activity", "📡", "Review telemetry and recent incidents"),
}

PAGE_ACCENTS = {
    "overview": colors.red,
    "join_defense": colors.fate,
    "staff_guard": colors.red,
    "response": colors.orange,
    "activity": colors.green,
}

ANTIRAID_ART_PATH = (
    Path(__file__).resolve().parents[2] / "assets" / "antiraid" / "guard.png"
)
ANTIRAID_ART_FILENAME = "fate-antiraid-guard.png"


def _parse_pair(
    raw: str,
    label: str,
    *,
    count_min: int,
    count_max: int,
    window_min: int,
    window_max: int,
) -> tuple[int, int]:
    separator = "/" if "/" in raw else ":" if ":" in raw else None
    if not separator:
        raise ValueError(f"{label} must use count/seconds, such as 10/20.")
    count_raw, window_raw = raw.split(separator, 1)
    try:
        count, window = int(count_raw.strip()), int(window_raw.strip())
    except ValueError as error:
        raise ValueError(f"{label} must contain whole numbers.") from error
    if not count_min <= count <= count_max:
        raise ValueError(f"{label} count must be {count_min}–{count_max}.")
    if not window_min <= window <= window_max:
        raise ValueError(f"{label} window must be {window_min}–{window_max} seconds.")
    return count, window


def _parse_int(raw: str, label: str, minimum: int, maximum: int) -> int:
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise ValueError(f"{label} must be a whole number.") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return value


class AntiRaidDashboard(ui.LayoutView):
    def __init__(self, cog: "AntiRaid", ctx: commands.Context):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.guild = ctx.guild
        self.user = ctx.author
        self.message: Optional[discord.Message] = None
        self.notice: Optional[str] = None
        self.page = "overview"
        self.thumbnail_media: Optional[str] = None
        if ANTIRAID_ART_PATH.is_file():
            self.thumbnail_media = f"attachment://{ANTIRAID_ART_FILENAME}"
        else:
            avatar = getattr(getattr(ctx.bot, "user", None), "display_avatar", None)
            if avatar:
                self.thumbnail_media = str(avatar.url)

    @property
    def config(self) -> dict[str, Any]:
        stored = self.cog.get_config(self.guild.id)
        return stored if stored is not None else recommended_config(enabled=False)

    async def start(self) -> None:
        self.rebuild()
        kwargs: dict[str, Any] = {
            "view": self,
            "ephemeral": bool(self.ctx.interaction),
        }
        if ANTIRAID_ART_PATH.is_file():
            kwargs["file"] = discord.File(
                ANTIRAID_ART_PATH,
                filename=ANTIRAID_ART_FILENAME,
                description="Fate AntiRaid containment emblem",
            )
        self.message = await self.ctx.send(**kwargs)

    def rebuild(self) -> None:
        self.clear_items()
        title, emoji, subtitle = PAGES[self.page]
        page_position = list(PAGES).index(self.page) + 1
        guild_name = discord.utils.escape_mentions(
            discord.utils.escape_markdown(self.guild.name)
        )
        if self.page == "overview":
            header = (
                "## 🛡️ Fate AntiRaid\n"
                "Stop a raid before it becomes a cleanup project.\n"
                "-# Join-flood defense · compromised-staff containment · automatic recovery"
            )
        else:
            header = (
                f"## {emoji} AntiRaid · {title}\n{subtitle}\n"
                f"-# {guild_name} · Panel {page_position}/{len(PAGES)} · "
                "Changes save instantly · 5 minute session"
            )
        header_item: ui.Item[Any]
        if self.thumbnail_media:
            header_item = ui.Section(
                ui.TextDisplay(header),
                accessory=ui.Thumbnail(
                    self.thumbnail_media,
                    description="Fate AntiRaid guard",
                ),
            )
        else:
            header_item = ui.TextDisplay(header)

        children: list[ui.Item[Any]] = [header_item]
        if self.notice:
            children.append(ui.TextDisplay(f"> ✨ **Updated** · {self.notice}"))
        children.extend(
            (
                ui.ActionRow(DashboardPageSelect(self)),
                ui.Separator(spacing=discord.SeparatorSpacing.small),
            )
        )
        config = self.config
        if self.page == "overview":
            children.extend(self._overview(config))
        elif self.page == "join_defense":
            children.extend(self._join_defense(config))
        elif self.page == "staff_guard":
            children.extend(self._staff_guard(config))
        elif self.page == "response":
            children.extend(self._response(config))
        else:
            children.extend(self._activity(config))
        self.add_item(
            ui.Container(
                *children,
                accent_colour=PAGE_ACCENTS.get(self.page, colors.red),
            )
        )

    def _overview(self, config: Mapping[str, Any]) -> list[ui.Item[Any]]:
        enabled = bool(config["enabled"])
        return [
            ui.TextDisplay(
                "## One switch between a raid and a ruined night\n"
                "AntiRaid recognizes coordinated attacks while they are unfolding, contains the accounts "
                "involved, and keeps a clear incident trail for your staff."
            ),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(
                "### Built for the attacks that do real damage\n"
                "🌊 **Shuts down join floods** before throwaway accounts take over your channels.\n"
                "🛰️ **Spots coordinated new-account clusters** without treating every newcomer like a raider.\n"
                "🔐 **Stops compromised staff accounts** that suddenly mass-ban members or delete roles, "
                "channels, and webhooks.\n"
                "⏳ **Recovers automatically** by reopening on schedule and releasing temporary raid bans."
            ),
            ui.TextDisplay(
                "### Start safely, stay in control\n"
                "Use **Observe** to see exactly what Fate would catch. Move to **Enforce** when you are ready. "
                "Every threshold, response, alert channel, and trusted bypass is yours to tune."
            ),
            ui.ActionRow(
                DashboardButton(
                    self,
                    "open_join" if enabled else "toggle_system",
                    "Tune defenses" if enabled else "Protect this server",
                    "🎛️" if enabled else "✨",
                    discord.ButtonStyle.primary if enabled else discord.ButtonStyle.success,
                ),
                DashboardButton(self, "open_status", "Current status", "📡"),
                DashboardButton(self, "open_join", "Explore defenses", "🧭"),
            ),
            ui.TextDisplay(
                f"-# {'Protection is already enabled. Fine-tune it or review live status.' if enabled else 'Your settings are saved instantly and can begin in no-action Observe mode.'}"
            ),
        ]

    def _join_defense(self, config: Mapping[str, Any]) -> list[ui.Item[Any]]:
        protections = config["protections"]
        burst = protections["join_burst"]
        risky = protections["suspicious_accounts"]
        lock_until = float(config.get("lockdown_until", 0.0))
        return [
            ui.TextDisplay(
                "## Layered join defense\n"
                "A normal burst watches all members. The high-risk layer reaches its own threshold "
                "only with young accounts, optionally requiring a blank profile."
            ),
            ui.TextDisplay(
                f"{'●' if burst['enabled'] else '○'} **Join burst shield** · "
                f"`{burst['threshold']} joins / {burst['window']}s`\n"
                f"{'●' if risky['enabled'] else '○'} **New-account cluster** · "
                f"`{risky['threshold']} accounts / {risky['window']}s` · "
                f"age ≤ `{risky['max_account_age_hours']}h` · "
                f"blank avatar `{'required' if risky['require_no_avatar'] else 'not required'}`\n"
                f"**Lock** · `{config['response']['lock_minutes']} minutes` · "
                f"**Join response** · `{config['response']['join_action'].replace('_', ' ').title()}`"
            ),
            ui.ActionRow(
                DashboardButton(
                    self,
                    "toggle_join",
                    "Disable burst" if burst["enabled"] else "Enable burst",
                    "🌊",
                ),
                DashboardButton(
                    self,
                    "toggle_risk",
                    "Disable account risk" if risky["enabled"] else "Enable account risk",
                    "🛰️",
                ),
                DashboardButton(
                    self, "edit_join", "Tune thresholds", "🎛️", discord.ButtonStyle.primary
                ),
            ),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(
                "### Current lockdown\n"
                + (
                    f"🔒 Active until <t:{int(lock_until)}:F> (<t:{int(lock_until)}:R>). "
                    f"`{len(config.get('temporary_bans', []))}` accounts await automatic release."
                    if lock_until > time()
                    else "○ Server is open. A triggered lockdown is persisted through cog reloads."
                )
            ),
            ui.ActionRow(
                DashboardButton(
                    self,
                    "unlock",
                    "End lockdown now",
                    "🔓",
                    discord.ButtonStyle.danger,
                    disabled=lock_until <= time(),
                ),
                DashboardButton(self, "refresh", "Refresh", "🔄"),
            ),
        ]

    def _staff_guard(self, config: Mapping[str, Any]) -> list[ui.Item[Any]]:
        settings = config["protections"]["destructive_actions"]
        watched = [
            label
            for key, label in (
                ("watch_bans", "bans"),
                ("watch_kicks", "kicks"),
                ("watch_channels", "channel deletes"),
                ("watch_roles", "role deletes"),
                ("watch_webhooks", "webhook deletes"),
            )
            if settings[key]
        ]
        return [
            ui.TextDisplay(
                "## Compromised staff-account guard\n"
                "Discord audit entries are correlated by actor. The server owner and Fate are always "
                "protected; other exemptions must be explicit so an administrator compromise is still detectable."
            ),
            ui.TextDisplay(
                f"**State** · `{'Enabled' if settings['enabled'] else 'Disabled'}`\n"
                f"**Trigger** · `{settings['threshold']} actions / {settings['window']}s` by one actor\n"
                f"**Watching** · {', '.join(watched) if watched else 'nothing selected'}\n"
                f"**Response** · `{config['response']['staff_action'].replace('_', ' ').title()}`"
            ),
            ui.ActionRow(
                DashboardButton(
                    self,
                    "toggle_staff",
                    "Disable staff guard" if settings["enabled"] else "Enable staff guard",
                    "🔐",
                ),
                DashboardButton(
                    self, "edit_staff", "Tune audit guard", "🎛️", discord.ButtonStyle.primary
                ),
            ),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(
                "### Containment hierarchy\n"
                "`Strip roles` removes only removable roles with dangerous permissions. "
                "`Kick` and `Ban` require confirmation when selected. Discord role hierarchy always wins."
            ),
            ui.ActionRow(StaffActionSelect(self)),
        ]

    def _response(self, config: Mapping[str, Any]) -> list[ui.Item[Any]]:
        alert_id = config.get("alert_channel_id")
        alert = self.guild.get_channel(int(alert_id)) if alert_id else None
        return [
            ui.TextDisplay(
                "## Response policy\n"
                "Start in Observe to inspect evidence without changing members. Enforce applies the selected "
                "join and staff responses when a detector reaches its threshold."
            ),
            ui.TextDisplay("### Rollout mode"),
            ui.ActionRow(ResponseModeSelect(self)),
            ui.TextDisplay("### Join containment"),
            ui.ActionRow(JoinActionSelect(self)),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(
                f"### Incident delivery\n**Alert channel** · "
                f"{alert.mention if alert else 'Automatic fallback'}\n"
                f"**Trusted roles** · `{len(config.get('trusted_roles', []))}` · "
                f"**Trusted members** · `{len(config.get('trusted_members', []))}`"
            ),
            ui.ActionRow(
                DashboardButton(self, "edit_alert", "Choose alert channel", "📣"),
                DashboardButton(self, "edit_trusted", "Edit trusted bypasses", "🎯"),
            ),
            ui.TextDisplay(
                "-# If no alert channel is selected, Fate tries the system channel and then the first "
                "text channel where it can send embeds. The server owner and Fate cannot be removed from trust."
            ),
        ]

    def _activity(self, config: Mapping[str, Any]) -> list[ui.Item[Any]]:
        runtime = self.cog.runtime_snapshot(self.guild.id)
        warnings = self.cog.permission_warnings(self.guild, config)
        active = enabled_protections(config)
        recent = runtime["recent"][:8]
        incident_text = "\n\n".join(
            f"**{incident['title']}** · <t:{int(discord.utils.parse_time(incident['created_at']).timestamp())}:R>\n"
            f"{incident['detail']}\n-# {incident['outcome']}"
            for incident in recent
        ) or "No AntiRaid incidents have been recorded since the bot restarted."
        elapsed = format_duration(max(0, time() - self.cog.started_at))
        return [
            ui.TextDisplay(
                f"## {'Protection online' if config['enabled'] else 'Protection offline'}\n"
                f"**Mode** · `{config['mode'].title()}` · **Defenses** · "
                f"`{len(active)}/{len(PROTECTION_ORDER)}` · **Lockdown** · "
                f"`{'Active' if runtime['active_lockdown'] else 'Open'}`\n"
                + (
                    "**Readiness** · Ready"
                    if not warnings
                    else f"**Readiness** · {len(warnings)} warning{'s' if len(warnings) != 1 else ''}\n"
                    + "\n".join(f"- {warning}" for warning in warnings[:5])
                )
            ),
            ui.ActionRow(
                DashboardButton(
                    self,
                    "toggle_system",
                    "Disable protection" if config["enabled"] else "Enable protection",
                    "📴" if config["enabled"] else "✨",
                    discord.ButtonStyle.danger if config["enabled"] else discord.ButtonStyle.success,
                ),
                DashboardButton(self, "open_join", "Tune defenses", "🎛️"),
            ),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(
                f"## Live telemetry · `{elapsed}`\n"
                f"`{runtime['joins']}` joins · `{runtime['suspicious_joins']}` high-risk · "
                f"`{runtime['destructive_actions']}` destructive actions\n"
                f"`{runtime['incidents']}` incidents · `{runtime['contained_members']}` contained · "
                f"`{runtime['staff_responses']}` staff responses · `{runtime['failures']}` failures"
            ),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(f"### Recent incidents\n{incident_text}"),
            ui.ActionRow(
                DashboardButton(self, "refresh", "Refresh", "🔄"),
                DashboardButton(self, "clear_activity", "Clear session activity", "🧹"),
                DashboardButton(self, "close", "Close", "✖️"),
            ),
        ]

    async def refresh_interaction(self, interaction: discord.Interaction) -> None:
        self.rebuild()
        self.message = await interaction.edit_original_response(view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "Only the moderator who opened this panel can use it.", ephemeral=True
            )
            return False
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server to change AntiRaid.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.walk_children():
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(view=self)


class DashboardPageSelect(ui.Select):
    def __init__(self, dashboard: AntiRaidDashboard):
        self.dashboard = dashboard
        super().__init__(
            placeholder="Navigate the AntiRaid control center",
            options=[
                discord.SelectOption(
                    label=label,
                    value=value,
                    emoji=emoji,
                    description=description,
                    default=dashboard.page == value,
                )
                for value, (label, emoji, description) in PAGES.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.dashboard.page = self.values[0]
        self.dashboard.notice = None
        await interaction.response.defer()
        await self.dashboard.refresh_interaction(interaction)


class ResponseModeSelect(ui.Select):
    MODES = {
        "observe": ("Observe", "Record evidence without changing members", "🔭"),
        "enforce": ("Enforce", "Apply lockdown and staff containment policies", "🛡️"),
    }

    def __init__(self, dashboard: AntiRaidDashboard):
        self.dashboard = dashboard
        current = dashboard.config["mode"]
        super().__init__(
            placeholder="Choose the rollout mode",
            options=[
                discord.SelectOption(
                    label=label,
                    value=value,
                    description=description,
                    emoji=emoji,
                    default=current == value,
                )
                for value, (label, description, emoji) in self.MODES.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = self.dashboard.cog.get_config(self.dashboard.guild.id, create=True)
        config["mode"] = self.values[0]
        await self.dashboard.cog.config.flush()
        if self.values[0] == "observe":
            await self.dashboard.cog.unlock_guild(self.dashboard.guild, notify=False)
        self.dashboard.notice = f"Rollout mode changed to {self.values[0].title()}."
        await self.dashboard.refresh_interaction(interaction)


class JoinActionSelect(ui.Select):
    ACTIONS = {
        "kick": ("Kick", "Remove raid accounts; they may rejoin immediately", "🚪"),
        "temporary_ban": (
            "Temporary ban",
            "Block re-entry and automatically unban when lockdown ends",
            "⏳",
        ),
    }

    def __init__(self, dashboard: AntiRaidDashboard):
        self.dashboard = dashboard
        current = dashboard.config["response"]["join_action"]
        super().__init__(
            placeholder="Choose the join-raid response",
            options=[
                discord.SelectOption(
                    label=label,
                    value=value,
                    description=description,
                    emoji=emoji,
                    default=current == value,
                )
                for value, (label, description, emoji) in self.ACTIONS.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = self.dashboard.cog.get_config(self.dashboard.guild.id, create=True)
        config["response"]["join_action"] = self.values[0]
        await self.dashboard.cog.config.flush()
        self.dashboard.notice = (
            f"Join containment changed to {self.values[0].replace('_', ' ').title()}."
        )
        await self.dashboard.refresh_interaction(interaction)


class StaffActionSelect(ui.Select):
    ACTIONS = {
        "strip_roles": (
            "Strip dangerous roles",
            "Remove manageable roles with moderation permissions",
            "🧯",
        ),
        "kick": ("Kick actor", "Remove the suspected compromised account", "🚪"),
        "ban": ("Ban actor", "Permanently ban the suspected compromised account", "🔨"),
    }

    def __init__(self, dashboard: AntiRaidDashboard):
        self.dashboard = dashboard
        current = dashboard.config["response"]["staff_action"]
        super().__init__(
            placeholder="Choose the compromised-account response",
            options=[
                discord.SelectOption(
                    label=label,
                    value=value,
                    description=description,
                    emoji=emoji,
                    default=current == value,
                )
                for value, (label, description, emoji) in self.ACTIONS.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        action = self.values[0]
        if action in {"kick", "ban"}:
            return await interaction.response.send_modal(
                ConfirmStaffActionModal(self.dashboard, action)
            )
        await interaction.response.defer()
        await _save_staff_action(self.dashboard, action)
        await self.dashboard.refresh_interaction(interaction)


async def _save_staff_action(dashboard: AntiRaidDashboard, action: str) -> None:
    config = dashboard.cog.get_config(dashboard.guild.id, create=True)
    config["response"]["staff_action"] = action
    await dashboard.cog.config.flush()
    dashboard.notice = f"Staff containment changed to {action.replace('_', ' ').title()}."


class DashboardButton(ui.Button):
    def __init__(
        self,
        dashboard: AntiRaidDashboard,
        action: str,
        label: str,
        emoji: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        *,
        disabled: bool = False,
    ):
        super().__init__(label=label, emoji=emoji, style=style, disabled=disabled)
        self.dashboard = dashboard
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.dashboard
        if self.action == "close":
            await interaction.response.defer()
            await interaction.delete_original_response()
            dashboard.stop()
            return
        if self.action == "edit_join":
            return await interaction.response.send_modal(JoinSettingsModal(dashboard))
        if self.action == "edit_staff":
            return await interaction.response.send_modal(StaffSettingsModal(dashboard))
        if self.action == "edit_alert":
            return await interaction.response.send_modal(AlertChannelModal(dashboard))
        if self.action == "edit_trusted":
            return await interaction.response.send_modal(TrustedBypassesModal(dashboard))
        if self.action in {"open_join", "open_status"}:
            await interaction.response.defer()
            dashboard.page = "join_defense" if self.action == "open_join" else "activity"
            dashboard.notice = None
            await dashboard.refresh_interaction(interaction)
            return

        await interaction.response.defer()
        config = dashboard.cog.get_config(dashboard.guild.id, create=True)
        if self.action == "toggle_system":
            was_enabled = bool(config["enabled"])
            await dashboard.cog.set_enabled(dashboard.guild, not was_enabled)
            dashboard.notice = (
                "Raid Protection enabled with saved settings."
                if not was_enabled
                else "Raid Protection disabled; settings were preserved."
            )
        elif self.action in {"toggle_join", "toggle_risk", "toggle_staff"}:
            protection = {
                "toggle_join": "join_burst",
                "toggle_risk": "suspicious_accounts",
                "toggle_staff": "destructive_actions",
            }[self.action]
            enabled = not config["protections"][protection]["enabled"]
            await dashboard.cog.set_protection(dashboard.guild, protection, enabled)
            dashboard.notice = (
                f"{PROTECTION_INFO[protection]['label']} {'enabled' if enabled else 'disabled'}."
            )
        elif self.action == "unlock":
            released = await dashboard.cog.unlock_guild(dashboard.guild, notify=True)
            dashboard.notice = f"Lockdown ended; released {released} temporary bans."
        elif self.action == "clear_activity":
            dashboard.cog.stats.pop(dashboard.guild.id, None)
            dashboard.cog.recent_incidents.pop(dashboard.guild.id, None)
            dashboard.notice = "In-memory AntiRaid telemetry cleared; saved settings were untouched."
        else:
            dashboard.notice = "Status refreshed from the current saved configuration."
        await dashboard.refresh_interaction(interaction)


class JoinSettingsModal(ui.Modal):
    def __init__(self, dashboard: AntiRaidDashboard):
        super().__init__(title="Tune AntiRaid join defense")
        self.dashboard = dashboard
        config = dashboard.config
        burst = config["protections"]["join_burst"]
        risk = config["protections"]["suspicious_accounts"]
        self.burst = ui.TextInput(
            default=f"{burst['threshold']}/{burst['window']}",
            placeholder="10/20",
            max_length=20,
        )
        self.risk = ui.TextInput(
            default=f"{risk['threshold']}/{risk['window']}",
            placeholder="4/30",
            max_length=20,
        )
        self.age = ui.TextInput(
            default=str(risk["max_account_age_hours"]),
            placeholder="24",
            max_length=5,
        )
        self.lock = ui.TextInput(
            default=str(config["response"]["lock_minutes"]),
            placeholder="10",
            max_length=4,
        )
        self.no_avatar = ui.Checkbox(default=bool(risk["require_no_avatar"]))
        for text, description, component in (
            ("Join burst · count/seconds", "3–100 joins inside 3–300 seconds.", self.burst),
            ("Risk cluster · count/seconds", "2–50 accounts inside 3–600 seconds.", self.risk),
            ("Maximum account age · hours", "Accounts this age or younger add risk.", self.age),
            ("Lockdown duration · minutes", "1 minute to 24 hours.", self.lock),
            (
                "Require a blank avatar",
                "When on, a young account needs no custom avatar to add risk.",
                self.no_avatar,
            ),
        ):
            self.add_item(ui.Label(text=text, description=description, component=component))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            burst_count, burst_window = _parse_pair(
                self.burst.value,
                "Join burst",
                count_min=3,
                count_max=100,
                window_min=3,
                window_max=300,
            )
            risk_count, risk_window = _parse_pair(
                self.risk.value,
                "Risk cluster",
                count_min=2,
                count_max=50,
                window_min=3,
                window_max=600,
            )
            age = _parse_int(self.age.value, "Account age", 1, 8_760)
            lock = _parse_int(self.lock.value, "Lockdown duration", 1, 1_440)
        except ValueError as error:
            return await interaction.response.send_message(
                f"Nothing saved: {error}", ephemeral=True
            )
        await interaction.response.defer()
        config = self.dashboard.cog.get_config(self.dashboard.guild.id, create=True)
        config["protections"]["join_burst"].update(
            threshold=burst_count, window=burst_window
        )
        config["protections"]["suspicious_accounts"].update(
            threshold=risk_count,
            window=risk_window,
            max_account_age_hours=age,
            require_no_avatar=bool(self.no_avatar.value),
        )
        config["response"]["lock_minutes"] = lock
        await self.dashboard.cog.config.flush()
        self.dashboard.notice = "Join thresholds and lockdown duration saved."
        await self.dashboard.refresh_interaction(interaction)


class StaffSettingsModal(ui.Modal):
    WATCHES = {
        "bans": "watch_bans",
        "kicks": "watch_kicks",
        "channels": "watch_channels",
        "roles": "watch_roles",
        "webhooks": "watch_webhooks",
    }

    def __init__(self, dashboard: AntiRaidDashboard):
        super().__init__(title="Tune compromised-account guard")
        self.dashboard = dashboard
        settings = dashboard.config["protections"]["destructive_actions"]
        self.threshold = ui.TextInput(
            default=f"{settings['threshold']}/{settings['window']}",
            placeholder="3/15",
            max_length=20,
        )
        selected = [label for label, key in self.WATCHES.items() if settings[key]]
        self.watches = ui.TextInput(
            default=", ".join(selected),
            placeholder="bans, kicks, channels, roles, webhooks",
            max_length=100,
        )
        self.add_item(
            ui.Label(
                text="Actor threshold · count/seconds",
                description="2–25 actions inside 3–300 seconds.",
                component=self.threshold,
            )
        )
        self.add_item(
            ui.Label(
                text="Watched actions",
                description="Comma-separated: bans, kicks, channels, roles, webhooks.",
                component=self.watches,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            count, window = _parse_pair(
                self.threshold.value,
                "Staff guard",
                count_min=2,
                count_max=25,
                window_min=3,
                window_max=300,
            )
            selected = {
                value.strip().lower()
                for value in self.watches.value.replace("\n", ",").split(",")
                if value.strip()
            }
            unknown = selected - self.WATCHES.keys()
            if unknown:
                raise ValueError(f"Unknown watched action: {sorted(unknown)[0]}.")
            if not selected:
                raise ValueError("Choose at least one watched action.")
        except ValueError as error:
            return await interaction.response.send_message(
                f"Nothing saved: {error}", ephemeral=True
            )
        await interaction.response.defer()
        settings = self.dashboard.cog.get_config(
            self.dashboard.guild.id, create=True
        )["protections"]["destructive_actions"]
        settings.update(threshold=count, window=window)
        for label, key in self.WATCHES.items():
            settings[key] = label in selected
        await self.dashboard.cog.config.flush()
        self.dashboard.notice = "Staff-guard threshold and watched actions saved."
        await self.dashboard.refresh_interaction(interaction)


class AlertChannelModal(ui.Modal):
    def __init__(self, dashboard: AntiRaidDashboard):
        super().__init__(title="AntiRaid incident delivery")
        self.dashboard = dashboard
        channel_id = dashboard.config.get("alert_channel_id")
        default = dashboard.guild.get_channel(int(channel_id)) if channel_id else None
        self.channel = ui.ChannelSelect(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder="Use automatic fallback or choose one channel",
            min_values=0,
            max_values=1,
            required=False,
            default_values=[default] if default else [],
        )
        self.add_item(
            ui.Label(
                text="Incident alert channel",
                description="Leave empty to use Fate's safe automatic fallback.",
                component=self.channel,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = self.dashboard.cog.get_config(self.dashboard.guild.id, create=True)
        config["alert_channel_id"] = self.channel.values[0].id if self.channel.values else None
        await self.dashboard.cog.config.flush()
        self.dashboard.notice = (
            f"Incident alerts will post in {self.channel.values[0].mention}."
            if self.channel.values
            else "Incident alerts will use the automatic fallback channel."
        )
        await self.dashboard.refresh_interaction(interaction)


class TrustedBypassesModal(ui.Modal):
    def __init__(self, dashboard: AntiRaidDashboard):
        super().__init__(title="AntiRaid trusted bypasses")
        self.dashboard = dashboard
        config = dashboard.config
        role_defaults = [
            role
            for role_id in config.get("trusted_roles", [])
            if (role := dashboard.guild.get_role(role_id))
        ][:25]
        member_defaults = [
            member
            for member_id in config.get("trusted_members", [])
            if (member := dashboard.guild.get_member(member_id))
        ][:25]
        self.preserved_roles = [
            role_id
            for role_id in config.get("trusted_roles", [])
            if role_id not in {role.id for role in role_defaults}
        ]
        self.preserved_members = [
            member_id
            for member_id in config.get("trusted_members", [])
            if member_id not in {member.id for member in member_defaults}
        ]
        self.roles = ui.RoleSelect(
            placeholder="Roles that fully bypass AntiRaid",
            min_values=0,
            max_values=25,
            required=False,
            default_values=role_defaults,
        )
        self.members = ui.UserSelect(
            placeholder="Members that fully bypass AntiRaid",
            min_values=0,
            max_values=25,
            required=False,
            default_values=member_defaults,
        )
        self.add_item(
            ui.Label(
                text="Trusted roles",
                description="Use sparingly; every member with the role bypasses all defenses.",
                component=self.roles,
            )
        )
        self.add_item(
            ui.Label(
                text="Trusted members",
                description="Explicit individual bypasses; uncached saved IDs are preserved.",
                component=self.members,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if any(role.id == self.dashboard.guild.id for role in self.roles.values):
            return await interaction.response.send_message(
                "Nothing saved: @everyone cannot bypass AntiRaid.", ephemeral=True
            )
        await interaction.response.defer()
        config = self.dashboard.cog.get_config(self.dashboard.guild.id, create=True)
        config["trusted_roles"] = list(
            dict.fromkeys(
                [*(role.id for role in self.roles.values), *self.preserved_roles]
            )
        )
        config["trusted_members"] = list(
            dict.fromkeys(
                [*(member.id for member in self.members.values), *self.preserved_members]
            )
        )
        await self.dashboard.cog.config.flush()
        self.dashboard.notice = (
            f"Saved {len(config['trusted_roles'])} trusted roles and "
            f"{len(config['trusted_members'])} trusted members."
        )
        await self.dashboard.refresh_interaction(interaction)


class ConfirmStaffActionModal(ui.Modal):
    def __init__(self, dashboard: AntiRaidDashboard, action: str):
        super().__init__(title=f"Confirm AntiRaid {action}")
        self.dashboard = dashboard
        self.action = action
        self.confirmation = ui.TextInput(
            placeholder=action.upper(),
            max_length=4,
        )
        self.add_item(
            ui.Label(
                text=f"Type {action.upper()} to confirm",
                description=(
                    "This response removes a suspected compromised staff account when "
                    "the destructive-action threshold is reached."
                ),
                component=self.confirmation,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.confirmation.value.strip().upper() != self.action.upper():
            return await interaction.response.send_message(
                "Nothing saved: the confirmation did not match.", ephemeral=True
            )
        await interaction.response.defer()
        await _save_staff_action(self.dashboard, self.action)
        await self.dashboard.refresh_interaction(interaction)
