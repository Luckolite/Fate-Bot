"""Discord Components V2 control center for AntiSpam."""

from __future__ import annotations

import io
import json
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional

import discord
from discord import ui
from discord.ext import commands

from botutils import colors, extract_time
from botutils.antispam import (
    DETECTION_PARENT,
    MODULE_DEFAULTS,
    MODULE_INFO,
    MODULE_ORDER,
    SCHEMA_VERSION,
    effective_action,
    evasion_signals,
    extract_urls,
    format_action,
    format_duration,
    format_thresholds,
    module_has_detectors,
    normalize_config,
    normalize_content,
    recommended_config,
    repeated_segment_score,
)

if TYPE_CHECKING:
    from cogs.moderation.anti_spam import AntiSpam


PAGES = {
    "overview": ("Overview", "🛡️", "See what AntiSpam can do"),
    "protections": (
        "Protections",
        "🧠",
        "Tune the signals Fate watches",
    ),
    "enforcement": (
        "Enforcement",
        "⚖️",
        "Choose what happens after a match",
    ),
    "exclusions": (
        "Exclusions",
        "🎯",
        "Decide where and who Fate skips",
    ),
    "diagnostics": (
        "Diagnostics",
        "🩺",
        "Readiness, incidents, and runtime",
    ),
    "guide": (
        "Guide",
        "📚",
        "Plain-language setup and tuning",
    ),
}

GUIDE_TOPICS = {
    "rollout": (
        "Safer rollout",
        "🧭",
        "Start quietly, inspect evidence, then increase response strength",
    ),
    "incident": (
        "How incidents work",
        "🔎",
        "From normalized message to a single explainable response",
    ),
    "tuning": (
        "False-positive tuning",
        "🎚️",
        "Adjust the narrowest rule before disabling broad protection",
    ),
    "privacy": (
        "Privacy and containment",
        "🔐",
        "What Fate remembers, deletes, and deliberately avoids",
    ),
    "commands": (
        "Command shortcuts",
        "⌨️",
        "Open the right panel directly from chat",
    ),
}

ANTISPAM_ART_PATH = (
    Path(__file__).resolve().parents[2] / "assets" / "antispam" / "guard.png"
)
ANTISPAM_ART_FILENAME = "fate-antispam-guard.png"

POLICY_TARGETS = {
    "default": ("Default response", "Fallback for message rules without an override"),
    "rate_limit": ("Flood control", "Rolling message-rate incidents"),
    "mass_pings": ("Mention bursts", "Mentions spread across messages"),
    "mass_pings_per_msg": ("Mentions per message", "One message with heavy mention weight"),
    "ghost_pings": ("Ghost pings", "Mentions removed by edit or deletion"),
    "duplicates": ("Repeated text", "Normalized repeated messages"),
    "duplicate_segments": ("Repeated segments", "Repeated adjacent phrases or lines"),
    "same_link": ("Repeated destinations", "Canonical URL repeats"),
    "same_image": ("Repeated images", "Matching short-lived image fingerprints"),
    "sticker": ("Sticker bursts", "Several sticker messages in a short window"),
    "same_sticker": ("Repeated stickers", "The same sticker sent repeatedly"),
    "inhuman": ("Message integrity", "Malformed or character-wall messages"),
    "copy_paste": ("Large paste risk", "A weak signal that requires supporting evidence"),
    "anti_macro": ("Automation cadence", "Long, unnaturally regular timing"),
    "evasion": ("Evasion resistance", "Invisible Unicode, edits, and delete/resend"),
    "edit_churn": ("Rapid edits", "Repeated content changes on one message"),
    "delete_resend": ("Delete and resend", "A deleted payload posted again quickly"),
    "coordinated": ("Coordinated campaigns", "One payload across several members"),
}

PAGE_ACCENTS = {
    "overview": colors.fate,
    "protections": colors.fate,
    "enforcement": colors.orange,
    "exclusions": colors.green,
    "diagnostics": colors.orange,
    "guide": colors.fate,
}


def _configured_module(config: Mapping[str, Any], module: str) -> bool:
    return module in config and module not in config.get("disabled_modules", [])


def _safe_mention(target: Any, fallback: str) -> str:
    return target.mention if target else fallback


def _rules_from_text(raw: str) -> list[dict[str, int]]:
    if raw.strip().lower() in {"off", "none", "disabled", "0"}:
        return []
    rules = []
    for item in raw.replace("\n", ",").split(","):
        item = item.strip().lower().replace("messages", "").replace("seconds", "")
        if not item:
            continue
        separator = "/" if "/" in item else ":" if ":" in item else None
        if not separator:
            raise ValueError("Use count/seconds pairs, such as 4/3, 6/10.")
        count_raw, seconds_raw = item.split(separator, 1)
        count = int(count_raw.strip())
        seconds = int(seconds_raw.strip().rstrip("s"))
        if not 2 <= count <= 100 or not 1 <= seconds <= 3600:
            raise ValueError("Counts must be 2–100 and windows 1–3600 seconds.")
        rule = {"threshold": count, "timespan": seconds}
        if rule not in rules:
            rules.append(rule)
    if not rules:
        raise ValueError("Enter at least one rolling-window rule.")
    if len(rules) > 25:
        raise ValueError("Use at most 25 rolling-window rules per module.")
    return sorted(rules, key=lambda rule: (rule["timespan"], rule["threshold"]))


def _parse_int(
    raw: str,
    label: str,
    minimum: int,
    maximum: int,
    *,
    allow_zero: bool = False,
) -> int:
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise ValueError(f"{label} must be a whole number.") from error
    floor = 0 if allow_zero else minimum
    if not floor <= value <= maximum:
        raise ValueError(f"{label} must be between {floor} and {maximum}.")
    return value


class AntiSpamDashboard(ui.LayoutView):
    def __init__(self, cog: "AntiSpam", ctx: commands.Context):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.guild = ctx.guild
        self.user = ctx.author
        self.message: Optional[discord.Message] = None
        self.notice: Optional[str] = None
        self.page = "overview"
        self.selected_module = "rate_limit"
        self.selected_policy = "default"
        self.selected_guide = "rollout"
        self.thumbnail_media: Optional[str] = None
        if ANTISPAM_ART_PATH.is_file():
            self.thumbnail_media = f"attachment://{ANTISPAM_ART_FILENAME}"
        else:
            bot_user = getattr(getattr(ctx, "bot", None), "user", None)
            avatar = getattr(bot_user, "display_avatar", None)
            if avatar:
                self.thumbnail_media = str(avatar.url)

    @property
    def config(self) -> dict[str, Any]:
        stored = self.cog.get_config(self.guild.id)
        if stored is not None:
            return stored
        preview = recommended_config()
        preview["enabled"] = False
        return preview

    async def start(self) -> None:
        self.rebuild()
        kwargs: dict[str, Any] = {
            "view": self,
            "ephemeral": bool(self.ctx.interaction),
        }
        if ANTISPAM_ART_PATH.is_file():
            kwargs["file"] = discord.File(
                ANTISPAM_ART_PATH,
                filename=ANTISPAM_ART_FILENAME,
                description="Fate AntiSpam protection emblem",
            )
        self.message = await self.ctx.send(**kwargs)

    def rebuild(self) -> None:
        self.clear_items()
        config = self.config
        title, emoji, subtitle = PAGES[self.page]
        if self.page == "overview":
            header = f"## {emoji} AntiSpam · {title}"
        else:
            guild_name = discord.utils.escape_mentions(
                discord.utils.escape_markdown(self.guild.name)
            )
            page_position = list(PAGES).index(self.page) + 1
            header = (
                f"## {emoji} AntiSpam · {title}\n"
                f"{subtitle}\n"
                f"-# {guild_name} · Panel {page_position}/{len(PAGES)} · "
                "Changes save instantly · 5 minute session"
            )
        header_item: ui.Item[Any]
        if self.thumbnail_media:
            header_item = ui.Section(
                ui.TextDisplay(header),
                accessory=ui.Thumbnail(
                    self.thumbnail_media,
                    description="Fate AntiSpam guard",
                ),
            )
        else:
            header_item = ui.TextDisplay(header)

        children: list[ui.Item[Any]] = [header_item]
        if self.notice:
            children.append(ui.TextDisplay(f"> ✨ **Updated** · {self.notice}"))
        children.extend(
            (
                self._navigation(),
                ui.Separator(spacing=discord.SeparatorSpacing.small),
            )
        )
        if self.page == "overview":
            children.extend(self._overview(config))
        elif self.page == "protections":
            children.extend(self._protections(config))
        elif self.page == "enforcement":
            children.extend(self._enforcement(config))
        elif self.page == "exclusions":
            children.extend(self._exclusions(config))
        elif self.page == "diagnostics":
            children.extend(self._diagnostics(config))
        else:
            children.extend(self._guide(config))
        self.add_item(
            ui.Container(
                *children,
                accent_colour=PAGE_ACCENTS.get(self.page, colors.fate),
            )
        )

    def _navigation(self) -> ui.ActionRow:
        return ui.ActionRow(DashboardPageSelect(self))

    def _overview(self, config: Mapping[str, Any]) -> list[ui.Item[Any]]:
        enabled = bool(config.get("enabled"))
        toggle_label = (
            "Disable AntiSpam"
            if enabled
            else "Enable saved settings"
            if self.cog.get_config(self.guild.id) is not None
            else "Enable recommended"
        )
        return [
            ui.TextDisplay(
                "## Protection that keeps up\n"
                "AntiSpam connects message patterns, timing, edits, and member behavior "
                "into one clear incident—so disruption is stopped without punishing "
                "normal conversation."
            ),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(
                "### 🌊 Stops the obvious\n"
                "**Floods & ping raids** · Catches rapid messages, mass mentions, and ghost pings.\n"
                "**Repeat spam** · Recognizes repeated text, links, images, stickers, and thread spam.\n"
                "**Message abuse** · Filters character walls, empty-line floods, and suspicious pastes."
            ),
            ui.TextDisplay(
                "### 🧠 Catches the clever\n"
                "**Automation** · Spots unnaturally regular, macro-like timing.\n"
                "**Evasion** · Sees through invisible Unicode, edit churn, and delete-and-resend tricks.\n"
                "**Coordinated raids** · Connects the same payload across multiple members."
            ),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(
                "### 🎛️ Built around your server\n"
                "**Preview safely** · Test suspicious messages without changing anything.\n"
                "**Respond your way** · Observe, delete, warn, timeout, kick, or ban.\n"
                "**Stay precise** · Exempt trusted channels, roles, and members.\n"
                "**Keep it private** · Message bodies and uploaded files are never persisted."
            ),
            ui.Section(
                ui.TextDisplay("### Make it yours"),
                ui.TextDisplay(
                    "Use the menu to tune detection, choose responses, set exclusions, "
                    "and inspect incidents."
                ),
                accessory=DashboardButton(
                    self,
                    "toggle_system",
                    toggle_label,
                    "📴" if enabled else "✨",
                    discord.ButtonStyle.danger
                    if enabled
                    else discord.ButtonStyle.success,
                ),
            ),
            ui.ActionRow(
                DashboardButton(self, "analyze", "Test message", "🧪"),
                DashboardButton(self, "refresh", "Refresh", "🔄"),
                DashboardButton(self, "close", "Close", "✖️"),
            ),
        ]

    def _module_overview_value(
        self, config: Mapping[str, Any], module: str
    ) -> str:
        value = config.get(module, MODULE_DEFAULTS[module])
        if module == "rate_limit":
            return f"Limits `{format_thresholds(value)}`"
        if module == "mass_pings":
            ghost = "On" if value["ghost_pings"] else "Off"
            return (
                f"Per message `{value['per_message']} weight` · Rolling "
                f"`{format_thresholds(value['thresholds'])}`\n"
                f"> Ghost-ping detection `{ghost}`"
            )
        if module == "duplicates":
            return (
                f"Text `{format_thresholds(value['thresholds'])}` · "
                f"Links `{format_duration(value['same_link'])}` · "
                f"Images `{format_duration(value['same_image'])}`\n"
                f"> Open-thread limit `{value['max_open_threads']} per member`"
            )
        if module == "inhuman":
            on = sum(bool(setting) for setting in value.values())
            return f"`{on} of {len(value)}` integrity checks enabled"
        if module == "anti_macro":
            return (
                f"`{value['samples']} samples` · `±{value['tolerance']}% jitter` · "
                f"`{value['min_interval']}–{value['max_interval']}s intervals`"
            )
        if module == "evasion":
            return (
                f"`{value['zero_width']} invisibles` · "
                f"`{value['combining_marks']} combining marks`\n"
                f"> `{value['max_edits']} edits / {value['edit_window']}s` · "
                f"resend window `{value.get('resend_window', 30)}s`"
            )
        return (
            f"`{value['unique_users']} members / {value['window']}s` · "
            f"minimum `{value['min_length']} normalized characters`"
        )

    def _module_value(self, config: Mapping[str, Any], module: str) -> str:
        value = config.get(module, MODULE_DEFAULTS[module])
        if module == "rate_limit":
            return format_thresholds(value)
        if module == "mass_pings":
            return (
                f"{value['per_message']} mention weight/message · "
                f"{format_thresholds(value['thresholds'])} · "
                f"ghost pings {'on' if value['ghost_pings'] else 'off'}"
            )
        if module == "duplicates":
            return (
                f"text {format_thresholds(value['thresholds'])} · "
                f"link {format_duration(value['same_link'])} · "
                f"image {format_duration(value['same_image'])} · "
                f"{value['max_open_threads']} open thread/user"
            )
        if module == "inhuman":
            on = sum(bool(setting) for setting in value.values())
            return f"{on}/{len(value)} integrity checks"
        if module == "anti_macro":
            return (
                f"{value['samples']} samples · ±{value['tolerance']}% jitter · "
                f"{value['min_interval']}–{value['max_interval']}s intervals"
            )
        if module == "evasion":
            return (
                f"{value['zero_width']} invisibles · {value['combining_marks']} marks · "
                f"{value['max_edits']} edits/{value['edit_window']}s · "
                f"resend {value.get('resend_window', 30)}s"
            )
        return (
            f"{value['unique_users']} members / {value['window']}s · "
            f"minimum {value['min_length']} normalized characters"
        )

    def _protections(self, config: Mapping[str, Any]) -> list[ui.Item[Any]]:
        module = self.selected_module
        configured = _configured_module(config, module)
        has_detectors = module_has_detectors(config, module)
        module_state = "Enabled" if configured else "Disabled"
        system_state = (
            "Enabled"
            if config.get("enabled")
            else "Disabled — no messages are being processed"
        )
        system_icon = "🟢" if config.get("enabled") else "🔴"
        checks_state = "Active" if configured and has_detectors else "No active checks"
        checks_icon = "" if configured and has_detectors else "⚠️ "
        action = format_action(
            config["punishments"].get(
                module, config["punishments"].get("default")
            )
        )
        body = (
            f"**Module** · `{module_state}`\n"
            f"**AntiSpam system** · {system_icon} `{system_state}`\n"
            f"**Checks** · {checks_icon}`{checks_state}` · "
            f"**Response** · `{action}`"
        )
        current_values = (
            "### Current setup\n"
            f"{self._module_overview_value(config, module)}"
        )
        return [
            ui.ActionRow(ModuleSelect(self)),
            ui.Section(
                ui.TextDisplay(
                    f"## {MODULE_INFO[module]['emoji']} "
                    f"{MODULE_INFO[module]['label']}"
                ),
                ui.TextDisplay(f"-# {MODULE_INFO[module]['summary']}"),
                ui.TextDisplay(body),
                accessory=DashboardButton(
                    self,
                    "edit_module",
                    "Edit settings",
                    "⚙️",
                    discord.ButtonStyle.primary,
                ),
            ),
            ui.TextDisplay(current_values),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(self._module_explanation(config, module)),
            ui.ActionRow(
                DashboardButton(
                    self,
                    "toggle_module",
                    "Disable module" if configured else "Enable module",
                    "📴" if configured else "▶️",
                    discord.ButtonStyle.danger
                    if configured
                    else discord.ButtonStyle.success,
                ),
                DashboardButton(self, "analyze", "Test message", "🧪"),
                DashboardButton(self, "reset_module", "Reset recommended", "↩️"),
                DashboardButton(self, "close", "Close", "✖️"),
            ),
        ]

    def _module_explanation(
        self, config: Mapping[str, Any], module: str
    ) -> str:
        explanations = {
            "rate_limit": (
                "### How it works\n"
                "Counts each member in true rolling windows. Short rules catch bursts; "
                "longer rules catch sustained flooding.\n"
                "-# **Good starting point:** 4/3s and 6/10s. Raise counts in busy servers."
            ),
            "mass_pings": (
                "### How it works\n"
                "User mentions weigh 1, roles 2, and everyone/here 4. Rolling rules "
                "count pinging messages and track distinct targets.\n"
                "-# Deleted ghost pings need View Audit Log; edited mentions do not."
            ),
            "duplicates": (
                "### How it works\n"
                "Normalizes text and destinations before comparing repeats. Images use "
                "short-lived metadata fingerprints; files are never downloaded.\n"
                "-# Very short phrases are ignored. Thread limits remove only the excess thread."
            ),
            "inhuman": (
                "### How it works\n"
                "Finds no-letter floods, vertical or empty-line walls, symbol-heavy text, "
                "dense character art, and risky large pastes.\n"
                "-# Unicode scripts count as letters. Missing typing alone never acts."
            ),
            "anti_macro": (
                "### How it works\n"
                "Compares a bounded sample of message intervals with your jitter tolerance. "
                "Recognized Fate commands are ignored.\n"
                "-# Tightening jitter or sample count? Test it in Observe mode first."
            ),
            "evasion": (
                "### How it works\n"
                "Finds invisible Unicode, excessive combining marks, spoiler walls, rapid "
                "edits, and delete/resend patterns.\n"
                "-# One delete/resend is supporting evidence only and never acts alone."
            ),
            "coordinated": (
                "### How it works\n"
                "Clusters one normalized payload across distinct members and channels. "
                "Common short phrases are ignored unless they contain a link.\n"
                "-# Only the current sender is moderated; age signals never act alone."
            ),
        }
        return explanations[module]

    def _enforcement(self, config: Mapping[str, Any]) -> list[ui.Item[Any]]:
        mode = config.get("mode", "enforce")
        mode_descriptions = {
            "observe": (
                "records signals and incidents only; no message or member changes"
            ),
            "delete": (
                "removes matched evidence but never times out, kicks, or bans"
            ),
            "enforce": (
                "removes evidence and applies the selected policy"
            ),
        }
        overrides = [
            key
            for key in config["punishments"]
            if key != "default" and key in POLICY_TARGETS
        ]
        override_lines = [
            f"- **{POLICY_TARGETS[target][0]}** → "
            f"`{format_action(config['punishments'][target])}`"
            for target in overrides[:6]
        ]
        if len(overrides) > 6:
            override_lines.append(
                f"-# +{len(overrides) - 6} more · inspect any rule in the picker"
            )
        override_text = (
            "\n".join(override_lines)
            if override_lines
            else "-# No rule-specific overrides; every detector inherits the default."
        )
        mode_icons = {"observe": "🔭", "delete": "🧹", "enforce": "🛡️"}
        mode_track = "  →  ".join(
            f"**{mode_icons[value]} {value.title()}**"
            if value == mode
            else f"{mode_icons[value]} {value.title()}"
            for value in ("observe", "delete", "enforce")
        )
        body = (
            "## Response path\n"
            f"{mode_track}\n"
            f"-# {mode_descriptions[mode].capitalize()}."
        )
        selected_label = POLICY_TARGETS[self.selected_policy][0]
        selected_action = format_action(
            effective_action(config, self.selected_policy)
        )
        policy_note = (
            "Changing the default updates every rule that does not have an override."
            if self.selected_policy == "default"
            else "Selecting Use default removes this rule-specific override."
        )
        return [
            ui.TextDisplay(body),
            ui.ActionRow(ResponseModeSelect(self)),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(
                "### Policy map\n"
                f"**Default** · `{format_action(config['punishments'].get('default'))}` · "
                f"**Overrides** · `{len(overrides)}`\n"
                "**Adaptive ladder** · `5m → 10m → 20m → 40m → 24h` · "
                "resets after `1 hour` incident-free"
            ),
            ui.TextDisplay(f"### Rule-specific overrides\n{override_text}"),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(
                f"## Editing · {selected_label}\n"
                f"{POLICY_TARGETS[self.selected_policy][1]}\n"
                f"**Effective response** · `{selected_action}`\n"
                f"-# {policy_note} Kick and Ban require confirmation."
            ),
            ui.ActionRow(PolicyTargetSelect(self)),
            ui.ActionRow(PolicyActionSelect(self)),
            ui.ActionRow(
                DashboardButton(
                    self, "reset_policies", "Reset all policies", "↩️"
                ),
                DashboardButton(self, "analyze", "Test message", "🧪"),
                DashboardButton(self, "close", "Close", "✖️"),
            ),
        ]

    def _exclusions(self, config: Mapping[str, Any]) -> list[ui.Item[Any]]:
        channels = []
        for channel_id in config.get("ignored", [])[:5]:
            target = self.guild.get_channel(channel_id) or self.guild.get_thread(
                channel_id
            )
            channels.append(
                _safe_mention(target, f"Unresolved scope ({channel_id})")
            )
        if len(config.get("ignored", [])) > 5:
            channels.append(f"+{len(config['ignored']) - 5} more saved")
        roles = [
            _safe_mention(
                self.guild.get_role(role_id), f"Unresolved role ({role_id})"
            )
            for role_id in config.get("trusted_roles", [])[:5]
        ]
        if len(config.get("trusted_roles", [])) > 5:
            roles.append(f"+{len(config['trusted_roles']) - 5} more saved")
        members = [
            _safe_mention(
                self.guild.get_member(user_id), f"Unresolved member ({user_id})"
            )
            for user_id in config.get("trusted_members", [])[:5]
        ]
        if len(config.get("trusted_members", [])) > 5:
            members.append(f"+{len(config['trusted_members']) - 5} more saved")
        def render_items(items: list[str], empty: str) -> str:
            return "\n".join(f"- {item}" for item in items) if items else empty

        return [
            ui.Section(
                ui.TextDisplay("## Bypass map"),
                ui.TextDisplay(
                    f"`{len(config.get('ignored', []))}` ignored scopes · "
                    f"`{len(config.get('trusted_roles', []))}` trusted roles · "
                    f"`{len(config.get('trusted_members', []))}` trusted members"
                ),
                ui.TextDisplay(
                    "-# Ignored parents include their threads. Trusted targets bypass "
                    "every detector; moderators and bots are already exempt."
                ),
                accessory=DashboardButton(
                    self,
                    "edit_exclusions",
                    "Edit lists",
                    "🎯",
                    discord.ButtonStyle.primary,
                ),
            ),
            ui.TextDisplay(
                "### 📍 Ignored channels and categories · "
                f"`{len(config.get('ignored', []))}`\n"
                + render_items(
                    channels,
                    "-# None saved — all visible message scopes are protected.",
                )
            ),
            ui.TextDisplay(
                f"### 🎟️ Trusted roles · `{len(config.get('trusted_roles', []))}`\n"
                + render_items(roles, "-# None saved.")
            ),
            ui.TextDisplay(
                f"### 👤 Trusted members · `{len(config.get('trusted_members', []))}`\n"
                + render_items(members, "-# None saved.")
            ),
            ui.TextDisplay(
                "-# The editor saves each list atomically and preserves IDs Discord "
                "cannot currently display. Clean stale entries removes only unresolved "
                "channels, categories, and deleted roles."
            ),
            ui.ActionRow(
                DashboardButton(
                    self,
                    "clean_exclusions",
                    "Clean stale entries",
                    "🧹",
                ),
                DashboardButton(self, "refresh", "Refresh", "🔄"),
                DashboardButton(self, "close", "Close", "✖️"),
            ),
        ]

    def _diagnostics(self, config: Mapping[str, Any]) -> list[ui.Item[Any]]:
        warnings = self.cog.configuration_warnings(self.guild, config)
        runtime = self.cog.runtime_snapshot(self.guild.id)
        permissions = self.guild.me.guild_permissions if self.guild.me else None
        permission_names = (
            ("manage_messages", "Manage Messages", "remove detected evidence"),
            ("read_message_history", "Read History", "inspect and remove earlier evidence"),
            ("moderate_members", "Moderate Members", "apply timeout responses"),
            ("send_messages", "Send Messages", "post warnings and incident notices"),
            ("embed_links", "Embed Links", "render readable notice cards"),
            ("view_audit_log", "View Audit Log", "separate staff deletes from ghost pings"),
            ("manage_threads", "Manage Threads", "remove excess open threads"),
            ("kick_members", "Kick Members", "run a configured kick policy"),
            ("ban_members", "Ban Members", "run a configured ban policy"),
        )
        configured_actions = {
            effective_action(config, target) for target in POLICY_TARGETS
        }
        permission_lines = []
        missing_permissions = []
        for key, label, _purpose in permission_names:
            required_action = (
                "kick"
                if key == "kick_members"
                else "ban"
                if key == "ban_members"
                else None
            )
            if required_action and required_action not in configured_actions:
                permission_lines.append(
                    f"➖ **{label}** · `Not required`"
                )
                continue
            ready = permissions and getattr(permissions, key, False)
            if not ready:
                missing_permissions.append(label)
            permission_lines.append(
                f"**{label}** · `{'Ready' if ready else 'Missing'}`"
            )
        permission_text = "\n".join(permission_lines)
        if runtime["recent"]:
            incidents = "\n".join(
                f"- <t:{int(item['timestamp'])}:R> · <@{item['user_id']}> in "
                f"<#{item['channel_id']}> · **{item['module'].replace('_', ' ')}** "
                f"({item['action']})"
                for item in runtime["recent"][:5]
            )
            incident_heading = (
                f"### Recent incident ledger · newest "
                f"{min(5, len(runtime['recent']))} of {len(runtime['recent'])}"
            )
        else:
            incidents = "-# No incidents recorded since this cog started."
            incident_heading = "### Recent incident ledger"
        cache_state = (
            f"`{sum(len(records) for records in self.cog.member_records.values()):,}` records · "
            f"`{len(self.cog.rate_windows):,}` rate windows · "
            f"`{len(self.cog.campaign_records):,}` campaigns · "
            f"`{len(self.cog.recent_deletes):,}` resend markers"
        )
        warning_lines = list(warnings[:5])
        if len(warnings) > 5:
            warning_lines.append(f"+{len(warnings) - 5} more")
        warning_text = (
            "\n".join(f"- {warning}" for warning in warning_lines)
            if warning_lines
            else "-# No configuration warnings."
        )
        ready = not warnings and not missing_permissions
        return [
            ui.Section(
                ui.TextDisplay(
                    f"## {'Ready for enforcement' if ready else '⚠️ Needs attention'}"
                ),
                ui.TextDisplay(
                    f"`{len(permission_names) - len(missing_permissions)}` permission checks ready · "
                    f"`{len(missing_permissions)}` missing · `{len(warnings)}` config warnings"
                ),
                ui.TextDisplay(
                    f"-# Schema {config.get('schema_version', 0)}/{SCHEMA_VERSION} · "
                    f"{max(0, len(config.get('punishments', {})) - 1)} policy overrides"
                ),
                accessory=DashboardButton(
                    self,
                    "analyze",
                    "Test message",
                    "🧪",
                    discord.ButtonStyle.primary,
                ),
            ),
            ui.TextDisplay(f"### Permission matrix\n{permission_text}"),
            ui.TextDisplay(
                f"### Configuration notes\n{warning_text}"
            ),
            ui.TextDisplay(f"### Runtime footprint\n{cache_state}"),
            ui.TextDisplay(f"{incident_heading}\n{incidents}"),
            ui.TextDisplay(
                "### Privacy\n"
                "Bounded fingerprints, timestamps, IDs, and incident summaries stay in "
                "memory. Message bodies and uploaded files are never persisted."
            ),
            ui.ActionRow(
                DashboardButton(self, "export", "Export config", "📤"),
                DashboardButton(self, "clear_runtime", "Clear runtime", "🧹"),
                DashboardButton(self, "refresh", "Refresh", "🔄"),
                DashboardButton(self, "close", "Close", "✖️"),
            ),
        ]

    def _guide(self, config: Mapping[str, Any]) -> list[ui.Item[Any]]:
        prefix = self.ctx.clean_prefix
        topic = self.selected_guide
        title, emoji, summary = GUIDE_TOPICS[topic]
        topic_bodies = {
            "rollout": (
                "### Recommended path\n"
                "**1 · Observe** — collect evidence without changing messages or members.\n"
                "**2 · Review** — test real examples and inspect Diagnostics.\n"
                "**3 · Delete** — remove matched evidence without member sanctions.\n"
                "**4 · Enforce** — apply the chosen response after the signal looks clean.\n"
                "-# Tightened a rule? Return to Observe briefly before enforcing it."
            ),
            "incident": (
                "### Detection path\n"
                "1. Exclusions and moderator exemptions are checked first.\n"
                "2. Fate normalizes content and gathers independent signals once.\n"
                "3. Strong evidence can act; weak signals must combine.\n"
                "4. Age can strengthen evidence but never create an incident alone.\n"
                "5. Evidence is removed only through its own channel.\n"
                "-# Coordinated protection moderates the current sender, never a mass of earlier participants."
            ),
            "tuning": (
                "### Tune narrowly\n"
                "- Raise the relevant count or window before disabling a module.\n"
                "- Ignore intentional high-volume channels instead of weakening the server.\n"
                "- Trust only roles or members that should bypass every detector.\n"
                "- Large pastes and missing typing are weak evidence by design.\n"
                "- Unicode letter categories keep legitimate multilingual text in scope."
            ),
            "privacy": (
                "### Data boundary\n"
                "**Memory only** · bounded fingerprints, timing, IDs, and incident summaries\n"
                "**Never persisted** · message bodies and uploaded files\n"
                "**Images** · compared through metadata; never downloaded by AntiSpam\n"
                "**Exports** · this server's non-secret settings only\n"
                "-# Runtime state can be cleared from Diagnostics without changing saved policy."
            ),
            "commands": (
                "### Direct routes\n"
                f"`{prefix}antispam` — open this control center\n"
                f"`{prefix}antispam configure` — open Protections\n"
                f"`{prefix}antispam punishments` — open Enforcement\n"
                f"`{prefix}antispam enable` / `{prefix}antispam disable` — change system state\n"
                f"`{prefix}antispam ignore #channel` — add a channel exclusion\n"
                "-# Per-module enable and disable commands remain available."
            ),
        }
        position = list(GUIDE_TOPICS).index(topic) + 1
        return [
            ui.ActionRow(GuideTopicSelect(self)),
            ui.TextDisplay(f"## {emoji} {title}\n{summary}"),
            ui.TextDisplay(topic_bodies[topic]),
            ui.TextDisplay(
                f"-# Guide topic {position}/{len(GUIDE_TOPICS)} · "
                f"System is currently {'enabled' if config.get('enabled') else 'disabled'}"
            ),
            ui.ActionRow(
                DashboardButton(
                    self,
                    "analyze",
                    "Test message",
                    "🧪",
                    discord.ButtonStyle.primary,
                ),
                DashboardButton(self, "refresh", "Refresh", "🔄"),
                DashboardButton(self, "close", "Close", "✖️"),
            ),
        ]

    async def refresh_interaction(
        self, interaction: discord.Interaction
    ) -> None:
        self.rebuild()
        self.message = await interaction.edit_original_response(view=self)

    async def refresh_message(self) -> None:
        self.rebuild()
        if self.message:
            await self.message.edit(view=self)

    async def interaction_check(
        self, interaction: discord.Interaction
    ) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "Only the moderator who opened this panel can use it.",
                ephemeral=True,
            )
            return False
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server to change AntiSpam.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.walk_children():
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class DashboardPageSelect(ui.Select):
    def __init__(self, dashboard: AntiSpamDashboard):
        self.dashboard = dashboard
        options = [
            discord.SelectOption(
                label=label,
                value=value,
                emoji=emoji,
                description=description,
                default=dashboard.page == value,
            )
            for value, (label, emoji, description) in PAGES.items()
        ]
        super().__init__(
            placeholder="Navigate the AntiSpam control center",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.dashboard.page = self.values[0]
        self.dashboard.notice = None
        await interaction.response.defer()
        await self.dashboard.refresh_interaction(interaction)


class GuideTopicSelect(ui.Select):
    def __init__(self, dashboard: AntiSpamDashboard):
        self.dashboard = dashboard
        options = [
            discord.SelectOption(
                label=label,
                value=value,
                emoji=emoji,
                description=description,
                default=dashboard.selected_guide == value,
            )
            for value, (label, emoji, description) in GUIDE_TOPICS.items()
        ]
        super().__init__(
            placeholder="Choose a guide topic",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.dashboard.selected_guide = self.values[0]
        self.dashboard.notice = None
        await interaction.response.defer()
        await self.dashboard.refresh_interaction(interaction)


class ModuleSelect(ui.Select):
    def __init__(self, dashboard: AntiSpamDashboard):
        self.dashboard = dashboard
        config = dashboard.config
        options = []
        for module in MODULE_ORDER:
            configured = _configured_module(config, module)
            state = (
                "Disabled"
                if not configured
                else "Enabled; no active checks"
                if not module_has_detectors(config, module)
                else "Enabled"
                if config.get("enabled")
                else "Enabled; AntiSpam disabled"
            )
            options.append(
                discord.SelectOption(
                    label=MODULE_INFO[module]["label"],
                    value=module,
                    emoji=MODULE_INFO[module]["emoji"],
                    description=f"{state} · {MODULE_INFO[module]['summary']}"[:100],
                    default=dashboard.selected_module == module,
                )
            )
        super().__init__(
            placeholder="Choose a protection to inspect",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.dashboard.selected_module = self.values[0]
        self.dashboard.notice = None
        await interaction.response.defer()
        await self.dashboard.refresh_interaction(interaction)


class ResponseModeSelect(ui.Select):
    MODES = {
        "observe": (
            "Observe",
            "Record evidence and telemetry without changing messages or members",
            "🔭",
        ),
        "delete": (
            "Delete only",
            "Remove matched evidence without applying member sanctions",
            "🧹",
        ),
        "enforce": (
            "Enforce",
            "Remove evidence and apply the configured response policy",
            "🛡️",
        ),
    }

    def __init__(self, dashboard: AntiSpamDashboard):
        self.dashboard = dashboard
        current = dashboard.config.get("mode", "enforce")
        options = [
            discord.SelectOption(
                label=label,
                value=value,
                description=description,
                emoji=emoji,
                default=current == value,
            )
            for value, (label, description, emoji) in self.MODES.items()
        ]
        super().__init__(
            placeholder="Choose the server-wide response mode",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        mode = self.values[0]
        await interaction.response.defer()
        config = self.dashboard.cog.get_config(
            self.dashboard.guild.id, create=True
        )
        previous = config.get("mode", "enforce")
        config["mode"] = mode
        await self.dashboard.cog.config.flush()
        if previous != mode:
            self.dashboard.cog.clear_runtime_for_guild(
                self.dashboard.guild.id, clear_telemetry=False
            )
            self.dashboard.notice = (
                f"Response mode changed to {mode.title()}. Detection windows and "
                "the timeout ladder restarted; incident telemetry was kept."
            )
        else:
            self.dashboard.notice = f"Response mode remains {mode.title()}."
        await self.dashboard.refresh_interaction(interaction)


class PolicyTargetSelect(ui.Select):
    def __init__(self, dashboard: AntiSpamDashboard):
        self.dashboard = dashboard
        options = [
            discord.SelectOption(
                label=label,
                value=value,
                description=(
                    f"{format_action(effective_action(dashboard.config, value))}"
                    f" · {description}"
                ),
                default=dashboard.selected_policy == value,
            )
            for value, (label, description) in POLICY_TARGETS.items()
        ]
        super().__init__(
            placeholder="Choose which rule response to edit",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.dashboard.selected_policy = self.values[0]
        self.dashboard.notice = None
        await interaction.response.defer()
        await self.dashboard.refresh_interaction(interaction)


class PolicyActionSelect(ui.Select):
    ACTIONS = (
        ("inherit", "Use default", "Remove this rule-specific override", "↪️"),
        ("observe", "Observe only", "Record the incident without deleting", "🔭"),
        ("delete", "Delete only", "Remove evidence; no member sanction", "🧹"),
        ("warn", "Warn + delete", "Remove evidence and post a brief incident card", "⚠️"),
        ("adaptive", "Adaptive timeout", "5m, doubling per repeat incident up to 24h", "📈"),
        ("timeout:600", "Timeout 10 minutes", "Always use an exact 10-minute timeout", "⏳"),
        ("timeout:3600", "Timeout 1 hour", "Always use an exact one-hour timeout", "⏳"),
        ("timeout:43200", "Timeout 12 hours", "Always use an exact 12-hour timeout", "⏳"),
        ("custom", "Custom timeout", "Enter any duration up to 28 days", "🛠️"),
        ("kick", "Kick", "Remove the member; confirmation required", "🚪"),
        ("ban", "Ban", "Ban the member; confirmation required", "🔨"),
    )

    def __init__(self, dashboard: AntiSpamDashboard):
        self.dashboard = dashboard
        target = dashboard.selected_policy
        punishments = dashboard.config["punishments"]
        configured = punishments.get(target)
        if target != "default" and target not in punishments:
            configured = "inherit"
        options = [
            discord.SelectOption(
                label=label,
                value=value,
                description=description,
                emoji=emoji,
                default=configured == value,
            )
            for value, label, description, emoji in self.ACTIONS
            if not (target == "default" and value == "inherit")
        ]
        super().__init__(
            placeholder="Choose the effective response",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        action = self.values[0]
        if action == "custom":
            return await interaction.response.send_modal(
                CustomTimeoutModal(self.dashboard)
            )
        if action in {"kick", "ban"}:
            return await interaction.response.send_modal(
                ConfirmDangerousPolicyModal(self.dashboard, action)
            )
        await interaction.response.defer()
        await apply_policy(self.dashboard, action)
        await self.dashboard.refresh_interaction(interaction)


async def apply_policy(
    dashboard: AntiSpamDashboard, action: str
) -> None:
    config = dashboard.cog.get_config(dashboard.guild.id, create=True)
    before = dict(config["punishments"])
    target = dashboard.selected_policy
    if action == "inherit" and target != "default":
        config["punishments"].pop(target, None)
        dashboard.notice = f"{POLICY_TARGETS[target][0]} now inherits the default response."
    else:
        config["punishments"][target] = action
        dashboard.notice = (
            f"{POLICY_TARGETS[target][0]} response set to {format_action(action)}."
        )
    if before != config["punishments"]:
        dashboard.cog.clear_runtime_for_guild(
            dashboard.guild.id, clear_telemetry=False
        )
    await dashboard.cog.config.flush()


class DashboardButton(ui.Button):
    def __init__(
        self,
        dashboard: AntiSpamDashboard,
        action: str,
        label: str,
        emoji: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ):
        super().__init__(label=label, emoji=emoji, style=style)
        self.dashboard = dashboard
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.dashboard
        config = dashboard.config
        if self.action == "close":
            await interaction.response.defer()
            await interaction.delete_original_response()
            dashboard.stop()
            return
        if self.action == "analyze":
            return await interaction.response.send_modal(
                AnalyzeSampleModal(dashboard)
            )
        if self.action == "edit_module":
            try:
                modal = ModuleConfigModal(dashboard, dashboard.selected_module)
            except ValueError as error:
                return await interaction.response.send_message(
                    f"Editor unavailable: {error}", ephemeral=True
                )
            return await interaction.response.send_modal(modal)
        if self.action == "reset_module":
            return await interaction.response.send_modal(
                ConfirmResetModuleModal(dashboard)
            )
        if self.action == "reset_policies":
            return await interaction.response.send_modal(
                ConfirmResetPoliciesModal(dashboard)
            )
        if self.action == "edit_exclusions":
            return await interaction.response.send_modal(
                ExclusionsModal(dashboard)
            )
        if self.action == "clear_runtime":
            return await interaction.response.send_modal(
                ClearRuntimeModal(dashboard)
            )
        if self.action == "toggle_system" and config.get("enabled"):
            return await interaction.response.send_modal(
                DisableAntiSpamModal(dashboard)
            )
        if self.action == "export":
            export = deepcopy(config)
            payload = json.dumps(export, indent=2, sort_keys=True).encode("utf-8")
            return await interaction.response.send_message(
                "Sanitized AntiSpam configuration. IDs are included; no message "
                "content, runtime evidence, or secrets are exported.",
                file=discord.File(
                    io.BytesIO(payload),
                    filename=f"antispam-{dashboard.guild.id}.json",
                ),
                ephemeral=True,
            )

        await interaction.response.defer()
        if self.action == "toggle_system":
            had_saved_config = dashboard.cog.get_config(
                dashboard.guild.id
            ) is not None
            await dashboard.cog.set_enabled(dashboard.guild, True)
            dashboard.notice = (
                "AntiSpam enabled with your saved settings."
                if had_saved_config
                else "AntiSpam enabled with the recommended settings."
            )
        elif self.action == "toggle_module":
            module = dashboard.selected_module
            enabled = not _configured_module(config, module)
            await dashboard.cog.set_module(dashboard.guild.id, module, enabled)
            system_note = (
                " AntiSpam itself remains disabled."
                if enabled and not dashboard.config.get("enabled")
                else ""
            )
            dashboard.notice = (
                f"{MODULE_INFO[module]['label']} "
                f"{'enabled' if enabled else 'disabled'}.{system_note}"
            )
        elif self.action == "clean_exclusions":
            stored = dashboard.cog.get_config(dashboard.guild.id, create=True)
            before_channels = len(stored["ignored"])
            before_roles = len(stored["trusted_roles"])
            stored["ignored"] = [
                target_id
                for target_id in stored["ignored"]
                if dashboard.guild.get_channel(target_id)
                or dashboard.guild.get_thread(target_id)
            ]
            stored["trusted_roles"] = [
                role_id
                for role_id in stored["trusted_roles"]
                if dashboard.guild.get_role(role_id)
            ]
            removed_channels = before_channels - len(stored["ignored"])
            removed_roles = before_roles - len(stored["trusted_roles"])
            await dashboard.cog.config.flush()
            dashboard.notice = (
                f"Removed {removed_channels} stale channel/category ID(s) and "
                f"{removed_roles} deleted role ID(s). Missing member IDs were kept "
                "because the member cache may be incomplete."
            )
        else:
            dashboard.notice = "Status refreshed from the current saved configuration."
        await dashboard.refresh_interaction(interaction)


class ModuleConfigModal(ui.Modal):
    def __init__(self, dashboard: AntiSpamDashboard, module: str):
        super().__init__(title=f"Edit {MODULE_INFO[module]['label']}")
        self.dashboard = dashboard
        self.module = module
        self.inputs: dict[str, Any] = {}
        current = deepcopy(
            dashboard.config.get(module, MODULE_DEFAULTS[module])
        )

        if module == "rate_limit":
            self._text(
                "rules",
                "Rolling limits",
                "Count/seconds pairs; use off to keep this module inert.",
                ", ".join(
                    f"{rule['threshold']}/{rule['timespan']}"
                    for rule in current
                )
                or "off",
                "4/3, 6/10",
                max_length=4000,
            )
        elif module == "mass_pings":
            self._text(
                "per_message",
                "Per-message mention weight",
                "User = 1, role = 2, everyone/here = 4. Zero disables.",
                str(current["per_message"]),
                "4",
            )
            self._text(
                "rules",
                "Rolling ping-message limits",
                "Count/seconds pairs; these count pinging messages. Use off to disable.",
                ", ".join(
                    f"{rule['threshold']}/{rule['timespan']}"
                    for rule in current["thresholds"]
                )
                or "off",
                "3/10, 6/30",
                max_length=4000,
            )
            self.inputs["ghost_pings"] = ui.Checkbox(
                custom_id="fate:antispam:ghost-pings",
                default=bool(current["ghost_pings"]),
            )
            self.add_item(
                ui.Label(
                    text="Ghost-ping detection",
                    description=(
                        "Requires View Audit Log to distinguish member deletes "
                        "from moderator/bot deletes."
                    ),
                    component=self.inputs["ghost_pings"],
                )
            )
        elif module == "duplicates":
            self._text(
                "rules",
                "Normalized text limits",
                "Count/seconds pairs for one member/fingerprint; off disables this check.",
                ", ".join(
                    f"{rule['threshold']}/{rule['timespan']}"
                    for rule in current["thresholds"]
                )
                or "off",
                "4/25",
                max_length=4000,
            )
            self._text(
                "per_message",
                "Repeated segment threshold",
                "One word or line repeated this many times. Zero disables.",
                str(current["per_message"]),
                "10",
            )
            self._text(
                "cooldowns",
                "Link and image windows",
                "Seconds as links/images. Zero disables either check.",
                f"{current['same_link']}/{current['same_image']}",
                "25/25",
            )
            self._text(
                "stickers",
                "Sticker burst/same-sticker windows",
                "Seconds as any sticker/same sticker. Zero disables.",
                f"{current['sticker']}/{current['same_sticker']}",
                "10/60",
            )
            self._text(
                "threads",
                "Maximum open threads per member",
                "Zero disables thread creation containment.",
                str(current["max_open_threads"]),
                "1",
            )
        elif module == "inhuman":
            labels = {
                "non_abc": (
                    "No-letter floods",
                    "Long content with no Unicode letters.",
                ),
                "tall_messages": (
                    "Vertical messages",
                    "Many short lines that consume excessive height.",
                ),
                "empty_lines": (
                    "Empty-line walls",
                    "Mostly empty or tiny lines.",
                ),
                "unknown_chars": (
                    "Symbol-heavy text",
                    "Long text dominated by symbols/controls.",
                ),
                "ascii": (
                    "Dense character walls",
                    "Very long text with almost no spacing.",
                ),
                "copy_paste": (
                    "Large paste risk",
                    "Weak typing-risk signal; never acts alone.",
                ),
            }
            options = [
                discord.CheckboxGroupOption(
                    label=label,
                    value=key,
                    description=description,
                    default=bool(current[key]),
                )
                for key, (label, description) in labels.items()
            ]
            self.inputs["checks"] = ui.CheckboxGroup(
                custom_id="fate:antispam:integrity-checks",
                required=False,
                min_values=0,
                max_values=len(options),
                options=options,
            )
            self.add_item(
                ui.Label(
                    text="Enabled message-integrity checks",
                    description=(
                        "Unchecked rules remain configured but do not emit signals."
                    ),
                    component=self.inputs["checks"],
                )
            )
        elif module == "anti_macro":
            self._text(
                "samples",
                "Required timing samples",
                "Longer samples reduce false positives. Recommended 14.",
                str(current["samples"]),
                "14",
            )
            self._text(
                "tolerance",
                "Maximum jitter percent",
                "Lower values require more machine-like timing. Recommended 8.",
                str(current["tolerance"]),
                "8",
            )
            self._text(
                "minimum",
                "Minimum interval seconds",
                "Cadences faster than this are left to flood control.",
                str(current["min_interval"]),
                "1",
            )
            self._text(
                "maximum",
                "Maximum interval seconds",
                "Ignore very slow regular schedules.",
                str(current["max_interval"]),
                "45",
            )
        elif module == "evasion":
            self._text(
                "unicode",
                "Invisible/Zalgo/spoiler thresholds",
                "Three counts separated by slashes.",
                f"{current['zero_width']}/{current['combining_marks']}/"
                f"{current['spoiler_segments']}",
                "6/12/10",
            )
            self._text(
                "edits",
                "Edit count/window seconds",
                "Rapid content edits on one message.",
                f"{current['max_edits']}/{current['edit_window']}",
                "4/20",
            )
            self._text(
                "resend",
                "Delete/resend window seconds",
                "Catch the same normalized payload after deletion. Zero disables.",
                str(current.get("resend_window", 30)),
                "30",
            )
        else:
            self._text(
                "users",
                "Distinct members required",
                "Only distinct authors count toward a campaign.",
                str(current["unique_users"]),
                "3",
            )
            self._text(
                "window",
                "Campaign window seconds",
                "Cross-channel normalized payload cluster window.",
                str(current["window"]),
                "20",
            )
            self._text(
                "length",
                "Minimum normalized length",
                "Shorter common phrases are ignored unless they contain a URL.",
                str(current["min_length"]),
                "18",
            )

    def _text(
        self,
        key: str,
        label: str,
        description: str,
        default: str,
        placeholder: str,
        *,
        max_length: int = 256,
    ) -> None:
        if len(default) > max_length:
            raise ValueError(
                "the saved rule list exceeds Discord's 4,000-character editor limit; "
                "export the configuration and reduce the legacy rules first"
            )
        component = ui.TextInput(
            custom_id=f"fate:antispam:{self.module}:{key}",
            default=default,
            placeholder=placeholder,
            min_length=1,
            max_length=max_length,
        )
        self.inputs[key] = component
        self.add_item(
            ui.Label(
                text=label,
                description=description,
                component=component,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            updated = self._parse()
        except (TypeError, ValueError) as error:
            return await interaction.response.send_message(
                f"Nothing saved: {error}", ephemeral=True
            )
        await interaction.response.defer()
        config = self.dashboard.cog.get_config(
            self.dashboard.guild.id, create=True
        )
        config[self.module] = updated
        while self.module in config["disabled_modules"]:
            config["disabled_modules"].remove(self.module)
        normalize_config(config)
        self.dashboard.cog.clear_runtime_for_guild(
            self.dashboard.guild.id, clear_telemetry=False
        )
        await self.dashboard.cog.config.flush()
        self.dashboard.notice = (
            f"{MODULE_INFO[self.module]['label']} saved and configured"
            f"{' (AntiSpam remains disabled)' if not config.get('enabled') else ''}: "
            f"{self.dashboard._module_value(config, self.module)}."
        )
        await self.dashboard.refresh_interaction(interaction)

    def _parse(self) -> Any:
        if self.module == "rate_limit":
            return _rules_from_text(self.inputs["rules"].value)
        if self.module == "mass_pings":
            return {
                "per_message": _parse_int(
                    self.inputs["per_message"].value,
                    "Per-message mention weight",
                    1,
                    100,
                    allow_zero=True,
                ),
                "ghost_pings": bool(self.inputs["ghost_pings"].value),
                "thresholds": _rules_from_text(self.inputs["rules"].value),
            }
        if self.module == "duplicates":
            link_raw, image_raw = self._parts(
                self.inputs["cooldowns"].value, 2, "link/image windows"
            )
            sticker_raw, same_raw = self._parts(
                self.inputs["stickers"].value, 2, "sticker windows"
            )
            return {
                "per_message": _parse_int(
                    self.inputs["per_message"].value,
                    "Repeated segment threshold",
                    2,
                    100,
                    allow_zero=True,
                ),
                "same_link": _parse_int(
                    link_raw, "Link window", 1, 3600, allow_zero=True
                ),
                "same_image": _parse_int(
                    image_raw, "Image window", 1, 3600, allow_zero=True
                ),
                "sticker": _parse_int(
                    sticker_raw, "Sticker burst window", 1, 3600, allow_zero=True
                ),
                "same_sticker": _parse_int(
                    same_raw, "Same-sticker window", 1, 3600, allow_zero=True
                ),
                "max_open_threads": _parse_int(
                    self.inputs["threads"].value,
                    "Open thread limit",
                    1,
                    100,
                    allow_zero=True,
                ),
                "thresholds": _rules_from_text(self.inputs["rules"].value),
            }
        if self.module == "inhuman":
            selected = set(self.inputs["checks"].values)
            return {
                key: key in selected
                for key in MODULE_DEFAULTS["inhuman"]
            }
        if self.module == "anti_macro":
            minimum = _parse_int(
                self.inputs["minimum"].value, "Minimum interval", 1, 300
            )
            maximum = _parse_int(
                self.inputs["maximum"].value, "Maximum interval", 2, 3600
            )
            if maximum <= minimum:
                raise ValueError(
                    "Maximum interval must be greater than the minimum."
                )
            return {
                "samples": _parse_int(
                    self.inputs["samples"].value, "Samples", 6, 30
                ),
                "tolerance": _parse_int(
                    self.inputs["tolerance"].value, "Jitter tolerance", 1, 50
                ),
                "min_interval": minimum,
                "max_interval": maximum,
            }
        if self.module == "evasion":
            zero, combining, spoilers = self._parts(
                self.inputs["unicode"].value, 3, "Unicode thresholds"
            )
            edits, edit_window = self._parts(
                self.inputs["edits"].value, 2, "edit threshold"
            )
            return {
                "zero_width": _parse_int(
                    zero, "Invisible-character threshold", 1, 100
                ),
                "combining_marks": _parse_int(
                    combining, "Combining-mark threshold", 1, 100
                ),
                "spoiler_segments": _parse_int(
                    spoilers, "Spoiler threshold", 1, 100
                ),
                "max_edits": _parse_int(edits, "Edit count", 2, 25),
                "edit_window": _parse_int(
                    edit_window, "Edit window", 2, 300
                ),
                "resend_window": _parse_int(
                    self.inputs["resend"].value,
                    "Delete/resend window",
                    1,
                    300,
                    allow_zero=True,
                ),
            }
        return {
            "unique_users": _parse_int(
                self.inputs["users"].value, "Distinct members", 2, 25
            ),
            "window": _parse_int(
                self.inputs["window"].value, "Campaign window", 3, 300
            ),
            "min_length": _parse_int(
                self.inputs["length"].value, "Minimum length", 6, 200
            ),
        }

    @staticmethod
    def _parts(raw: str, count: int, label: str) -> list[str]:
        parts = [
            part.strip()
            for part in raw.replace(",", "/").split("/")
            if part.strip()
        ]
        if len(parts) != count:
            raise ValueError(
                f"{label.title()} needs {count} slash-separated values."
            )
        return parts


class CustomTimeoutModal(ui.Modal):
    def __init__(self, dashboard: AntiSpamDashboard):
        super().__init__(title="Custom AntiSpam timeout")
        self.dashboard = dashboard
        target = dashboard.selected_policy
        current = dashboard.config["punishments"].get(target, "")
        default = (
            f"{current.split(':', 1)[1]}s"
            if isinstance(current, str) and current.startswith("timeout:")
            else "30m"
        )
        self.duration = ui.TextInput(
            custom_id="fate:antispam:custom-timeout",
            default=default,
            placeholder="30m, 2h, 1d, or seconds",
            min_length=2,
            max_length=16,
        )
        self.add_item(
            ui.Label(
                text="Exact timeout duration",
                description="Between 10 seconds and Discord's 28-day limit.",
                component=self.duration,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.duration.value.strip()
        seconds = int(raw) if raw.isdecimal() else extract_time(raw)
        if not seconds or not 10 <= seconds <= 2_419_200:
            return await interaction.response.send_message(
                "Choose a timeout between 10 seconds and 28 days.",
                ephemeral=True,
            )
        await interaction.response.defer()
        await apply_policy(self.dashboard, f"timeout:{seconds}")
        await self.dashboard.refresh_interaction(interaction)


class ConfirmDangerousPolicyModal(ui.Modal):
    def __init__(self, dashboard: AntiSpamDashboard, action: str):
        super().__init__(title=f"Confirm AntiSpam {action}")
        self.dashboard = dashboard
        self.action = action
        self.confirm = ui.Checkbox(
            custom_id=f"fate:antispam:confirm-{action}"
        )
        self.add_item(
            ui.Label(
                text=f"Use {action.title()} for this rule",
                description=(
                    "Runs when the rule fires. Role hierarchy and bot permissions "
                    "still apply."
                ),
                component=self.confirm,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.confirm.value:
            return await interaction.response.send_message(
                "Nothing changed — select the confirmation box first.",
                ephemeral=True,
            )
        await interaction.response.defer()
        await apply_policy(self.dashboard, self.action)
        await self.dashboard.refresh_interaction(interaction)


class ConfirmResetModuleModal(ui.Modal):
    def __init__(self, dashboard: AntiSpamDashboard):
        module = dashboard.selected_module
        super().__init__(title=f"Reset {MODULE_INFO[module]['label']}")
        self.dashboard = dashboard
        self.confirm = ui.Checkbox(
            custom_id="fate:antispam:confirm-module-reset"
        )
        self.add_item(
            ui.Label(
                text="Replace values with recommended defaults",
                description=(
                    "This enables the module and discards its custom thresholds."
                ),
                component=self.confirm,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.confirm.value:
            return await interaction.response.send_message(
                "Nothing changed — select the confirmation box first.",
                ephemeral=True,
            )
        await interaction.response.defer()
        module = self.dashboard.selected_module
        await self.dashboard.cog.reset_module(
            self.dashboard.guild.id, module
        )
        self.dashboard.notice = (
            f"{MODULE_INFO[module]['label']} reset to recommended values."
        )
        await self.dashboard.refresh_interaction(interaction)


class ConfirmResetPoliciesModal(ui.Modal):
    def __init__(self, dashboard: AntiSpamDashboard):
        super().__init__(title="Reset every AntiSpam policy")
        self.dashboard = dashboard
        self.confirm = ui.Checkbox(
            custom_id="fate:antispam:confirm-policy-reset"
        )
        self.add_item(
            ui.Label(
                text="Reset every response policy",
                description=(
                    "Restores the complete recommended policy map and removes overrides."
                ),
                component=self.confirm,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.confirm.value:
            return await interaction.response.send_message(
                "Nothing changed — all response policies remain saved.",
                ephemeral=True,
            )
        await interaction.response.defer()
        dashboard = self.dashboard
        stored = dashboard.cog.get_config(dashboard.guild.id, create=True)
        recommended = recommended_config()["punishments"]
        changed = stored["punishments"] != recommended
        stored["punishments"] = deepcopy(recommended)
        if changed:
            dashboard.cog.clear_runtime_for_guild(
                dashboard.guild.id, clear_telemetry=False
            )
        await dashboard.cog.config.flush()
        dashboard.notice = (
            "All response policies reset to the safer recommended defaults."
        )
        await dashboard.refresh_interaction(interaction)


class DisableAntiSpamModal(ui.Modal):
    def __init__(self, dashboard: AntiSpamDashboard):
        super().__init__(title="Disable AntiSpam")
        self.dashboard = dashboard
        self.confirm = ui.Checkbox(
            custom_id="fate:antispam:confirm-disable"
        )
        self.add_item(
            ui.Label(
                text="Disable all AntiSpam processing",
                description=(
                    "Settings stay saved and can be enabled again from this dashboard."
                ),
                component=self.confirm,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.confirm.value:
            return await interaction.response.send_message(
                "AntiSpam remains enabled.", ephemeral=True
            )
        await interaction.response.defer()
        await self.dashboard.cog.set_enabled(self.dashboard.guild, False)
        self.dashboard.notice = (
            "AntiSpam disabled. Detectors will not process messages; settings remain saved."
        )
        await self.dashboard.refresh_interaction(interaction)


class ExclusionsModal(ui.Modal):
    def __init__(self, dashboard: AntiSpamDashboard):
        super().__init__(title="AntiSpam exclusions")
        self.dashboard = dashboard
        config = dashboard.config
        guild = dashboard.guild
        channel_defaults = [
            target
            for target_id in config.get("ignored", [])
            if (
                target := guild.get_channel(target_id)
                or guild.get_thread(target_id)
            )
        ][:25]
        role_defaults = [
            role
            for role_id in config.get("trusted_roles", [])
            if (role := guild.get_role(role_id))
        ][:25]
        member_defaults = [
            member
            for user_id in config.get("trusted_members", [])
            if (member := guild.get_member(user_id))
        ][:25]
        self.preserved_channels = [
            target_id
            for target_id in config.get("ignored", [])
            if target_id not in {target.id for target in channel_defaults}
        ]
        self.preserved_roles = [
            role_id
            for role_id in config.get("trusted_roles", [])
            if role_id not in {role.id for role in role_defaults}
        ]
        self.preserved_members = [
            user_id
            for user_id in config.get("trusted_members", [])
            if user_id not in {member.id for member in member_defaults}
        ]
        self.channels = ui.ChannelSelect(
            custom_id="fate:antispam:ignored-channels",
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.forum,
                discord.ChannelType.category,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
                discord.ChannelType.news_thread,
            ],
            placeholder="Channels or categories AntiSpam should ignore",
            min_values=0,
            max_values=25,
            required=False,
            default_values=channel_defaults,
        )
        self.roles = ui.RoleSelect(
            custom_id="fate:antispam:trusted-roles",
            placeholder="Roles that fully bypass AntiSpam",
            min_values=0,
            max_values=25,
            required=False,
            default_values=role_defaults,
        )
        self.members = ui.UserSelect(
            custom_id="fate:antispam:trusted-members",
            placeholder="Members that fully bypass AntiSpam",
            min_values=0,
            max_values=25,
            required=False,
            default_values=member_defaults,
        )
        self.add_item(
            ui.Label(
                text="Ignored message scopes",
                description=(
                    "Parents cover threads; unresolved/overflow saved IDs are preserved."
                ),
                component=self.channels,
            )
        )
        self.add_item(
            ui.Label(
                text="Trusted roles",
                description="Full bypass; unresolved/overflow saved role IDs are preserved.",
                component=self.roles,
            )
        )
        self.add_item(
            ui.Label(
                text="Trusted members",
                description="Moderators are automatic; uncached/overflow IDs are preserved.",
                component=self.members,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if any(role.id == self.dashboard.guild.id for role in self.roles.values):
            return await interaction.response.send_message(
                "Nothing saved: @everyone cannot be a trusted role because it would "
                "bypass every protection for every member.",
                ephemeral=True,
            )
        await interaction.response.defer()
        config = self.dashboard.cog.get_config(
            self.dashboard.guild.id, create=True
        )
        before = (
            tuple(config["ignored"]),
            tuple(config["trusted_roles"]),
            tuple(config["trusted_members"]),
        )
        config["ignored"] = list(
            dict.fromkeys(
                [*(target.id for target in self.channels.values), *self.preserved_channels]
            )
        )
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
        after = (
            tuple(config["ignored"]),
            tuple(config["trusted_roles"]),
            tuple(config["trusted_members"]),
        )
        if before != after:
            self.dashboard.cog.clear_runtime_for_guild(
                self.dashboard.guild.id, clear_telemetry=False
            )
        await self.dashboard.cog.config.flush()
        self.dashboard.notice = (
            f"Exclusions saved: {len(config['ignored'])} scopes, "
            f"{len(config['trusted_roles'])} roles, "
            f"{len(config['trusted_members'])} members."
        )
        await self.dashboard.refresh_interaction(interaction)


class AnalyzeSampleModal(ui.Modal):
    def __init__(self, dashboard: AntiSpamDashboard):
        super().__init__(title="Test a message safely")
        self.dashboard = dashboard
        self.content = ui.TextInput(
            custom_id="fate:antispam:sample-content",
            placeholder="Paste the exact message content you want AntiSpam to test.",
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=4000,
        )
        self.user_mentions = ui.TextInput(
            custom_id="fate:antispam:sample-user-mentions",
            placeholder="0",
            default="0",
            required=False,
            max_length=3,
        )
        self.role_mentions = ui.TextInput(
            custom_id="fate:antispam:sample-role-mentions",
            placeholder="0",
            default="0",
            required=False,
            max_length=3,
        )
        self.context = ui.CheckboxGroup(
            custom_id="fate:antispam:sample-context",
            required=False,
            min_values=0,
            max_values=4,
            options=[
                discord.CheckboxGroupOption(
                    label="Treat as @everyone/@here — mention weight +4",
                    value="everyone",
                ),
                discord.CheckboxGroupOption(
                    label="Account under 7 days — +1 after a signal",
                    value="new_account",
                ),
                discord.CheckboxGroupOption(
                    label="Joined under 24 hours — +1 after a signal",
                    value="new_member",
                ),
                discord.CheckboxGroupOption(
                    label="No typing in 45 seconds — test large-paste risk",
                    value="no_typing",
                ),
            ],
        )
        self.add_item(
            ui.Label(
                text="Message to test",
                description="Test only: nothing will be sent, deleted, or moderated.",
                component=self.content,
            )
        )
        self.add_item(
            ui.Label(
                text="Individual user mentions",
                description="How many individual users the message mentions (0–100).",
                component=self.user_mentions,
            )
        )
        self.add_item(
            ui.Label(
                text="Role mentions",
                description="How many roles the message mentions; each has weight 2 (0–100).",
                component=self.role_mentions,
            )
        )
        self.add_item(
            ui.Label(
                text="Optional message and member context",
                description="Select only the details that apply to this simulation.",
                component=self.context,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        config = self.dashboard.config
        content = self.content.value
        lowered = content.casefold()
        normalized = normalize_content(content)
        urls = extract_urls(content)
        try:
            users = _parse_int(
                self.user_mentions.value or "0",
                "Individual user mentions",
                1,
                100,
                allow_zero=True,
            )
            roles = _parse_int(
                self.role_mentions.value or "0",
                "Role mentions",
                1,
                100,
                allow_zero=True,
            )
        except ValueError as error:
            return await interaction.response.send_message(
                f"Nothing tested: {error}", ephemeral=True
            )

        context = set(self.context.values)
        everyone_in_text = "@everyone" in lowered or "@here" in lowered
        everyone = "everyone" in context or everyone_in_text
        mention_weight = users + roles * 2 + (4 if everyone else 0)
        signals: list[tuple[str, str, int]] = []

        def add_signal(rule: str, label: str, detail: str, points: int) -> None:
            signals.append((rule, f"**{label}:** {detail} (+{points})", points))

        if _configured_module(config, "mass_pings"):
            limit = int(config["mass_pings"]["per_message"] or 0)
            if limit and mention_weight >= limit:
                add_signal(
                    "mass_pings_per_msg",
                    "Mention weight",
                    f"weight {mention_weight} reached the limit {limit}",
                    6,
                )
        alphabetic = sum(character.isalpha() for character in lowered)
        spaces = max(1, sum(character.isspace() for character in lowered))
        lines = lowered.splitlines()
        if _configured_module(config, "inhuman"):
            module = config["inhuman"]
            if module.get("non_abc") and len(content) > 256 and not alphabetic:
                add_signal("non_abc", "No-letter flood", "long content with no letters", 4)
            if module.get("tall_messages") and (
                (len(lines) > 8 and sum(len(line) for line in lines if line) < 21)
                or (len(lines) > 5 and alphabetic == 0)
            ):
                add_signal(
                    "tall_messages",
                    "Vertical message",
                    f"content spans {len(lines)} mostly tiny lines",
                    4,
                )
            if module.get("empty_lines"):
                small = sum(not line or len(line) < 3 for line in lines)
                large = sum(len(line) > 2 for line in lines)
                if len(lines) > 8 and small > large:
                    add_signal(
                        "empty_lines",
                        "Empty-line wall",
                        f"{small} empty or tiny lines",
                        4,
                    )
            if (
                module.get("unknown_chars")
                and len(content) > (256 if ":" in content else 128)
            ):
                ratio = len(content) / max(1, alphabetic)
                if ratio > 3 and not ("http" in lowered and len(content) < 512):
                    add_signal(
                        "unknown_chars",
                        "Symbol-heavy text",
                        "content is mostly symbols or non-letter controls",
                        3,
                    )
            if (
                module.get("ascii")
                and len(content) > 256
                and len(content) / spaces > 10
            ):
                add_signal(
                    "ascii",
                    "Dense character wall",
                    "very long content has almost no spacing",
                    3,
                )
            if (
                module.get("copy_paste")
                and "no_typing" in context
                and len(content) > (600 if "http" in lowered else 800)
            ):
                add_signal(
                    "copy_paste",
                    "Large paste risk",
                    "large content with no recent typing signal",
                    2,
                )

        if _configured_module(config, "evasion"):
            for name, reason in evasion_signals(content, config["evasion"]):
                add_signal(name, name.replace("_", " ").title(), reason, 5)
        if _configured_module(config, "duplicates"):
            repeats = repeated_segment_score(content)
            threshold = int(config["duplicates"]["per_message"] or 0)
            if threshold and repeats >= threshold:
                add_signal(
                    "duplicate_segments",
                    "Repeated segments",
                    f"{repeats} adjacent phrase/line repeats; limit {threshold}",
                    4,
                )

        evidence_score = sum(points for _rule, _line, points in signals)
        age_reasons = []
        if "new_account" in context:
            age_reasons.append("account under 7 days")
        if "new_member" in context:
            age_reasons.append("joined under 24 hours")
        age_risk_boost = len(age_reasons) if signals else 0
        total_score = evidence_score + age_risk_boost
        strongest = max(signals, key=lambda item: item[2], default=None)
        incident = bool(strongest) and (
            strongest[2] >= 4 or total_score >= 5
        )

        primary_text = "No primary rule from this message alone."
        if not incident:
            prospective_response = (
                "No single-message incident; historical or member signals would be "
                "needed to reach an action."
            )
        else:
            rule = strongest[0]
            action = effective_action(config, rule)
            parent = DETECTION_PARENT.get(rule, rule)
            punishments = config.get("punishments", {})
            if rule in punishments:
                policy_source = "rule override"
            elif parent in punishments:
                policy_source = "module override"
            else:
                policy_source = "inherited default"
            primary_label = POLICY_TARGETS.get(
                rule,
                POLICY_TARGETS.get(
                    parent,
                    (rule.replace("_", " ").title(), ""),
                ),
            )[0]
            primary_text = (
                f"**{primary_label}** · {format_action(action)} · {policy_source}"
            )
            mode = config.get("mode", "enforce")
            if mode == "observe" or action == "observe":
                prospective_response = (
                    "Observe only — record telemetry; do not delete or sanction."
                )
            elif mode == "delete" or action == "delete":
                prospective_response = (
                    "Delete the matched message; do not sanction the member."
                )
            elif action == "warn":
                prospective_response = (
                    "Delete the message and post a short warning card."
                )
            else:
                prospective_response = (
                    f"Delete the message and apply: {format_action(action)}."
                )

        if not config.get("enabled"):
            verdict = "🔴 **Preview only — AntiSpam is currently disabled.**"
            response = f"No action now. If enabled: {prospective_response}"
        elif not incident:
            verdict = "🟡 **Supporting evidence only — no incident from this message alone.**"
            response = prospective_response
        else:
            verdict = "🔶 **This message qualifies as a single-message incident.**"
            response = prospective_response

        preview = discord.utils.escape_mentions(
            discord.utils.escape_markdown(normalized[:240])
        )
        score_text = f"{evidence_score} evidence"
        if age_risk_boost:
            score_text += (
                f" + {age_risk_boost} age-risk context = {total_score} total"
            )
        everyone_text = (
            "yes — detected in pasted text"
            if everyone_in_text
            else "yes — context override"
            if "everyone" in context
            else "no"
        )
        context_text = (
            f"AntiSpam: **{'enabled' if config.get('enabled') else 'disabled'}** · "
            f"Response mode: **{str(config.get('mode', 'enforce')).title()}**\n"
            f"User mentions: **{users}** · Role mentions: **{roles}** · "
            f"Everyone/here: **{everyone_text}**\n"
            f"Normalized length: **{len(normalized)}** · Canonical URLs: **{len(urls)}** · "
            f"Mention weight: **{mention_weight}**\n"
            f"Score: **{score_text}**"
        )
        if age_reasons:
            context_text += f"\nAge context: {', '.join(age_reasons)}"

        embed = discord.Embed(
            title="🧪 AntiSpam message test",
            description=f"{verdict}\n**Previewed outcome:** {response}",
            color=(
                colors.fate
                if not config.get("enabled")
                else colors.orange
                if incident
                else colors.green
            ),
        )
        embed.add_field(name="Message profile", value=context_text, inline=False)
        embed.add_field(
            name="Primary rule and policy",
            value=primary_text,
            inline=False,
        )
        signal_lines = [f"- {line}" for _rule, line, _points in signals]
        if not signal_lines:
            signal_lines = ["- None"]
        signal_chunks: list[str] = []
        current_chunk = ""
        for line in signal_lines:
            candidate = f"{current_chunk}\n{line}" if current_chunk else line
            if len(candidate) > 1000 and current_chunk:
                signal_chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk = candidate
        if current_chunk:
            signal_chunks.append(current_chunk)
        for index, chunk in enumerate(signal_chunks):
            embed.add_field(
                name=(
                    f"Matched single-message signals · {len(signals)}"
                    if index == 0
                    else f"Matched signals · continued {index + 1}"
                ),
                value=chunk,
                inline=False,
            )
        embed.add_field(
            name="Normalized preview",
            value=preview or "(empty after normalization)",
            inline=False,
        )
        embed.add_field(
            name="Requires live event history",
            value=(
                "- **Volume and media:** rolling rate/mention volume, historical "
                "duplicates, and image/sticker repeats.\n"
                "- **Behavior and network:** cadence, edit churn, delete/resend, and "
                "coordinated-member windows."
            ),
            inline=False,
        )
        embed.set_footer(text="Test only — no messages or members were changed.")
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ClearRuntimeModal(ui.Modal):
    def __init__(self, dashboard: AntiSpamDashboard):
        super().__init__(title="Clear AntiSpam runtime state")
        self.dashboard = dashboard
        self.confirm = ui.Checkbox(
            custom_id="fate:antispam:confirm-clear-runtime"
        )
        self.add_item(
            ui.Label(
                text="Clear this server's runtime history",
                description=(
                    "Saved configuration is untouched. Detection history starts fresh."
                ),
                component=self.confirm,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.confirm.value:
            return await interaction.response.send_message(
                "Nothing cleared — select the confirmation box first.",
                ephemeral=True,
            )
        cog = self.dashboard.cog
        guild_id = self.dashboard.guild.id
        cog.clear_runtime_for_guild(guild_id)
        self.dashboard.notice = (
            "Runtime windows, incident counters, and fingerprints cleared for this server."
        )
        self.dashboard.rebuild()
        await interaction.response.edit_message(view=self.dashboard)
