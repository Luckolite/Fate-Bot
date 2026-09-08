"""Live adapters for configuring Fate cogs from Mission Control."""

from __future__ import annotations

import asyncio
import re
import sys
from contextlib import suppress
from copy import deepcopy
from typing import Any

import discord

from botutils.antiraid import (
    SCHEMA_VERSION as ANTIRAID_SCHEMA_VERSION,
    recommended_config as recommended_antiraid_config,
)
from botutils.antispam import (
    MODULE_DEFAULTS as ANTISPAM_MODULE_DEFAULTS,
    MODULE_ORDER as ANTISPAM_MODULE_ORDER,
    SCHEMA_VERSION as ANTISPAM_SCHEMA_VERSION,
    recommended_config as recommended_antispam_config,
    set_module_enabled as set_antispam_module_enabled,
)
from .module_validation import LOGGER_ARCHIVE_DEFAULTS, LOGGER_GROUPS
from .validation import ValidationError


def _id(value: Any) -> str | None:
    return str(value) if value not in (None, "", False) else None


def _ids(values) -> list[str]:
    return [str(value) for value in values or []]


def _public_logger_configuration(config: dict[str, Any] | None) -> dict[str, Any]:
    config = config or {}
    archive = config.get("local_archive")
    if not isinstance(archive, dict):
        archive = {}
    output = {
        "channel": _id(config.get("channel")),
        "channels": {
            str(event): _id(channel_id)
            for event, channel_id in config.get("channels", {}).items()
        },
        "secure": bool(config.get("secure", False)),
        "ignored_roles": _ids(config.get("ignored_roles", [])),
        "ignored_channels": _ids(config.get("ignored_channels", [])),
        "ignored_bots": _ids(config.get("ignored_bots", [])),
        "disabled": [str(event) for event in config.get("disabled", [])],
        "theme": config.get("theme"),
        "local_archive": {
            "enabled": bool(archive.get("enabled", False)),
            "retention_days": int(archive.get("retention_days", 30)),
            "cache_messages": bool(archive.get("cache_messages", True)),
            "store_attachments": bool(archive.get("store_attachments", False)),
            "attachment_size_limit_mb": int(
                archive.get("attachment_size_limit_mb", 25)
            ),
        },
    }
    if "color" in config:
        output["color"] = config["color"]
    if "colors" in config:
        output["colors"] = deepcopy(config["colors"])
    return output


def _public_module(module: str, value: dict[str, Any]) -> dict[str, Any]:
    """Keep Discord snowflakes as strings at the browser boundary."""
    output = deepcopy(value)
    if "channel_id" in output:
        output["channel_id"] = _id(output["channel_id"])
    if "ignored_channels" in output:
        output["ignored_channels"] = _ids(output["ignored_channels"])
    if module in {"anti_spam", "anti_raid"}:
        output["trusted_roles"] = _ids(output.get("trusted_roles", []))
        output["trusted_members"] = _ids(output.get("trusted_members", []))
    if module == "autorole":
        for role in output.get("roles", []):
            role["role_id"] = _id(role["role_id"])
    if module == "starboard":
        for board in output.get("boards", []):
            board["board_id"] = _id(board.get("board_id"))
            board["channel_id"] = _id(board.get("channel_id"))
    if module == "logger" and output.get("configuration") is not None:
        output["configuration"] = _public_logger_configuration(output["configuration"])
    return output


def module_defaults(*, available: bool = True) -> dict[str, dict[str, Any]]:
    common = {"available": available}
    return {
        "autorole": {**common, "enabled": False, "wait_for_verify": False, "roles": []},
        "chatfilter": {
            **common,
            "enabled": False,
            "blacklist": [],
            "whitelist": [],
            "ignored_channels": [],
            "regex": False,
            "filter_nicks": False,
            "filter_bots": False,
            "filter_webhooks": False,
            "filter_phishing": False,
        },
        "logger": {
            **common,
            "enabled": False,
            "channel_id": None,
            "secure": False,
            "theme": None,
            "groups": list(LOGGER_GROUPS),
            "ignored_channels": [],
            "local_archive": deepcopy(LOGGER_ARCHIVE_DEFAULTS),
            "configuration": _public_logger_configuration(None),
        },
        "modmail": {**common, "enabled": False, "channel_id": None},
        "anti_spam": {
            **common,
            "schema_version": ANTISPAM_SCHEMA_VERSION,
            "enabled": False,
            "mode": "enforce",
            "protections": {
                name: {
                    "enabled": True,
                    **(
                        {"thresholds": deepcopy(settings)}
                        if name == "rate_limit"
                        else deepcopy(settings)
                    ),
                }
                for name, settings in ANTISPAM_MODULE_DEFAULTS.items()
            },
            "punishments": deepcopy(recommended_antispam_config()["punishments"]),
            "ignored_channels": [],
            "trusted_roles": [],
            "trusted_members": [],
            "health": {
                "warnings": [],
                "coverage": {},
                "runtime": {},
            },
        },
        "anti_raid": {
            **common,
            **recommended_antiraid_config(enabled=False),
            "schema_version": ANTIRAID_SCHEMA_VERSION,
            "alert_channel_id": None,
            "health": {"warnings": [], "runtime": {}},
        },
        "welcome": {
            **common,
            "enabled": False,
            "channel_id": None,
            "message": "Welcome !mention",
            "use_images": False,
            "image_urls": [],
            "wait_for_verify": False,
        },
        "leave": {
            **common,
            "enabled": False,
            "channel_id": None,
            "message": "!user left !server",
            "use_images": False,
            "image_urls": [],
        },
        "restore_roles": {**common, "enabled": False, "allow_permissions": False},
        "selfroles": {**common, "enabled": False, "menus": []},
        "giveaways": {
            **common,
            "enabled": available,
            "active_count": 0,
            "recent_count": 0,
            "total_entries": 0,
            "giveaways": [],
        },
        "starboard": {**common, "enabled": False, "boards": []},
        "vc_log": {**common, "enabled": False, "channel_id": None, "keep_clean": True},
    }


class MemoryModuleStore:
    """Database-free module preview used only by explicit dashboard dev mode."""

    def __init__(self):
        self.data: dict[int, dict[str, dict[str, Any]]] = {}
        self.menu_id = 9_000_000_000_000_000_000

    async def close(self) -> None:
        return None

    async def get(
        self, guild_id: int, *, actor_id: int | None = None
    ) -> dict[str, Any]:
        del actor_id
        modules = self.data.setdefault(guild_id, module_defaults())
        return {"live": False, "modules": deepcopy(modules)}

    async def save(
        self,
        guild_id: int,
        module: str,
        settings: dict[str, Any],
        *,
        actor_id: int,
    ) -> dict[str, Any]:
        del actor_id
        modules = self.data.setdefault(guild_id, module_defaults())
        if module == "selfroles":
            menus = modules[module]["menus"]
            action = settings["action"]
            if action == "delete":
                menus[:] = [menu for menu in menus if menu["menu_id"] != _id(settings["menu_id"])]
            else:
                menu_id = _id(settings["menu_id"])
                if action == "create":
                    self.menu_id += 1
                    menu_id = str(self.menu_id)
                menu = {
                    "menu_id": menu_id,
                    "channel_id": _id(settings["channel_id"]),
                    "role_ids": _ids(settings["role_ids"]),
                    "text": settings["text"],
                    "label": settings["label"],
                    "style": settings["style"],
                    "limit": settings["limit"],
                    "show_percentage": settings["show_percentage"],
                    "show_roles": settings["show_roles"],
                    "editable": True,
                }
                menus[:] = [item for item in menus if item["menu_id"] != menu_id]
                menus.append(menu)
            modules[module]["enabled"] = bool(menus)
        else:
            modules[module] = {
                "available": True,
                **_public_module(module, settings),
            }
        return deepcopy(modules[module])

    async def modmail_entries(self, guild_id: int, *, actor_id: int) -> dict[str, Any]:
        del actor_id
        config = self.data.setdefault(guild_id, module_defaults())["modmail"]
        if not config.get("enabled"):
            return {"enabled": False, "channel_id": None, "channel_name": None, "entries": []}
        return {
            "enabled": True,
            "channel_id": _id(config.get("channel_id")),
            "channel_name": "staff-orbit",
            "entries": [
                {
                    "id": "900000000000000021",
                    "name": "Case 142 - NovaPilot",
                    "case_number": "142",
                    "status": "active",
                    "message_count": 6,
                    "latest_author": "NovaPilot",
                    "latest_message": "Thanks — I can send the screenshot and the message link here.",
                    "updated_at": "2026-08-08T15:42:00+00:00",
                    "url": "https://discord.com/channels/1/900000000000000021",
                },
                {
                    "id": "900000000000000018",
                    "name": "Case 138 - OrbitFox (Closed)",
                    "case_number": "138",
                    "status": "closed",
                    "message_count": 9,
                    "latest_author": "Fate Staff",
                    "latest_message": "This has been resolved. Reply to your original Modmail message if you need us again.",
                    "updated_at": "2026-08-07T21:18:00+00:00",
                    "url": "https://discord.com/channels/1/900000000000000018",
                },
            ],
        }


class UnavailableModuleStore:
    """Avoid stale cross-process writes when the dashboard is not mounted on Fate."""

    async def close(self) -> None:
        return None

    async def get(
        self, _guild_id: int, *, actor_id: int | None = None
    ) -> dict[str, Any]:
        del actor_id
        return {"live": False, "modules": module_defaults(available=False)}

    async def save(self, *_args, **_kwargs) -> dict[str, Any]:
        raise ValidationError(
            "Module settings are temporarily unavailable. Please try again later."
        )

    async def modmail_entries(self, *_args, **_kwargs) -> dict[str, Any]:
        return {"enabled": False, "channel_id": None, "channel_name": None, "entries": []}


class BotModuleStore:
    """Translate dashboard settings into each loaded cog's native live state."""

    COGS = {
        "autorole": "AutoRole",
        "chatfilter": "ChatFilter",
        "logger": "Logger",
        "modmail": "ModMail",
        "anti_spam": "AntiSpam",
        "anti_raid": "AntiRaid",
        "welcome": "Welcome",
        "leave": "Leave",
        "restore_roles": "RestoreRoles",
        "selfroles": "SelfRoles",
        "giveaways": "Giveaways",
        "starboard": "Starboard",
        "vc_log": "VcLog",
    }
    REQUIRED_PERMISSIONS = {
        "autorole": ("manage_roles", "Manage Roles"),
        "chatfilter": ("manage_messages", "Manage Messages"),
        "logger": ("manage_guild", "Manage Server"),
        "modmail": ("administrator", "Administrator"),
        "anti_spam": ("administrator", "Administrator"),
        "anti_raid": ("administrator", "Administrator"),
        "welcome": ("manage_guild", "Manage Server"),
        "leave": ("manage_guild", "Manage Server"),
        "restore_roles": ("administrator", "Administrator"),
        "selfroles": ("manage_roles", "Manage Roles"),
        "giveaways": ("manage_guild", "Manage Server"),
        "starboard": ("manage_guild", "Manage Server"),
        "vc_log": ("manage_channels", "Manage Channels"),
    }

    def __init__(self, bot):
        self.bot = bot

    async def close(self) -> None:
        return None

    def _cog(self, module: str):
        return self.bot.get_cog(self.COGS[module])

    async def _actor(self, guild, actor_id: int):
        actor = guild.get_member(actor_id)
        if actor is None:
            with suppress(Exception):
                actor = await guild.fetch_member(actor_id)
        if actor is None:
            raise ValidationError("Your Discord membership could not be verified.")
        return actor

    @staticmethod
    def _require_permission(guild, actor, permission: str, label: str) -> None:
        permissions = actor.guild_permissions
        if actor.id == guild.owner_id or permissions.administrator:
            return
        if not getattr(permissions, permission, False):
            raise ValidationError(f"You need {label} permission to change this setting.")

    @classmethod
    def _module_access(cls, guild, actor, module: str) -> dict[str, Any]:
        permission, label = cls.REQUIRED_PERMISSIONS[module]
        permissions = actor.guild_permissions
        allowed = bool(
            actor.id == guild.owner_id
            or getattr(permissions, "administrator", False)
            or getattr(permissions, permission, False)
        )
        return {
            "locked": not allowed,
            "required_permission": label,
        }

    @classmethod
    def _apply_module_access(
        cls,
        modules: dict[str, dict[str, Any]],
        guild,
        actor,
    ) -> None:
        safe_defaults = module_defaults()
        for module, config in list(modules.items()):
            access = cls._module_access(guild, actor, module)
            if access["locked"]:
                redacted = safe_defaults[module]
                redacted["available"] = config["available"]
                redacted["enabled"] = bool(config.get("enabled", False))
                modules[module] = redacted
            modules[module].update(access)

    @staticmethod
    def _require_bot_permission(guild, permission: str, label: str) -> None:
        member = guild.me
        if not member or not getattr(member.guild_permissions, permission, False):
            raise ValidationError(f"Fate needs {label} permission before this can be enabled.")

    @staticmethod
    def _channel(guild, channel_id: int | None, label: str):
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "permissions_for"):
            raise ValidationError(f"The selected {label.lower()} no longer exists.")
        permissions = channel.permissions_for(guild.me)
        if not permissions.send_messages:
            raise ValidationError(f"Fate cannot send messages in the selected {label.lower()}.")
        return channel

    @staticmethod
    def _role(guild, actor, role_id: int, label: str):
        role = guild.get_role(role_id)
        if role is None:
            raise ValidationError(f"The selected {label.lower()} no longer exists.")
        if role.is_default() or role.managed:
            raise ValidationError(f"{label} cannot be managed by Fate.")
        if role.position >= guild.me.top_role.position:
            raise ValidationError(f"{label} is above Fate's highest role.")
        if actor.id != guild.owner_id and role.position >= actor.top_role.position:
            raise ValidationError(f"{label} is above your highest role.")
        return role

    async def get(
        self, guild_id: int, *, actor_id: int | None = None
    ) -> dict[str, Any]:
        defaults = module_defaults()
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise ValidationError("Fate is not currently connected to that server.")
        actor = await self._actor(guild, actor_id) if actor_id is not None else None
        for module in defaults:
            if self._cog(module) is None:
                defaults[module]["available"] = False

        await self._read_autorole(guild_id, defaults["autorole"])
        self._read_chatfilter(guild_id, defaults["chatfilter"])
        self._read_logger(guild_id, defaults["logger"])
        self._read_modmail(guild_id, defaults["modmail"])
        self._read_anti_spam(guild_id, defaults["anti_spam"])
        await self._read_anti_raid(guild_id, defaults["anti_raid"])
        self._read_welcome(guild_id, defaults["welcome"])
        self._read_leave(guild_id, defaults["leave"])
        self._read_restore_roles(guild_id, defaults["restore_roles"])
        self._read_selfroles(guild_id, defaults["selfroles"])
        self._read_giveaways(guild_id, defaults["giveaways"])
        self._read_starboard(guild_id, defaults["starboard"])
        self._read_vc_log(guild_id, defaults["vc_log"])
        if actor is not None:
            self._apply_module_access(defaults, guild, actor)
        return {"live": True, "modules": defaults}

    async def _read_autorole(self, guild_id: int, output: dict[str, Any]) -> None:
        cog = self._cog("autorole")
        if not cog:
            return
        config = await cog.get_config(guild_id)
        output.update(
            enabled=bool(config.get("roles")),
            wait_for_verify=bool(config.get("wait_for_verify", False)),
            roles=[
                {"role_id": str(role_id), "delay": int(delay or 0)}
                for role_id, delay in config.get("roles", {}).items()
            ],
        )

    def _read_chatfilter(self, guild_id: int, output: dict[str, Any]) -> None:
        cog = self._cog("chatfilter")
        if not cog:
            return
        config = cog.config.get(guild_id, {})
        output.update(
            enabled=bool(config.get("toggle", False)),
            blacklist=list(config.get("blacklist", [])),
            whitelist=list(config.get("whitelist", [])),
            ignored_channels=_ids(
                value for value in config.get("ignored", [])
                if self.bot.get_channel(int(value)) is not None
            ),
            regex="regex" in config,
            filter_nicks="filter_nicks" in config,
            filter_bots="bots" in config,
            filter_webhooks="webhooks" in config,
            filter_phishing="phishing" in config,
        )

    def _read_logger(self, guild_id: int, output: dict[str, Any]) -> None:
        cog = self._cog("logger")
        if not cog:
            return
        config = cog.config.get(str(guild_id), {})
        disabled = set(config.get("disabled", []))
        groups = [
            name for name, events in cog.categories.items()
            if any(event not in disabled for event in events)
        ]
        output.update(
            enabled=bool(config),
            channel_id=_id(config.get("channel")),
            secure=bool(config.get("secure", False)),
            theme=config.get("theme") if config.get("theme") in {"Role Color", "RGB"} else None,
            groups=groups,
            ignored_channels=_ids(config.get("ignored_channels", [])),
            local_archive=deepcopy(
                _public_logger_configuration(config)["local_archive"]
            ),
            configuration=_public_logger_configuration(config),
        )

    def _read_modmail(self, guild_id: int, output: dict[str, Any]) -> None:
        cog = self._cog("modmail")
        if not cog:
            return
        config = cog.config.get(guild_id, cog.config.get(str(guild_id), {}))
        output.update(
            enabled=bool(config),
            channel_id=_id(config.get("channel_id")),
        )

    def _read_anti_spam(self, guild_id: int, output: dict[str, Any]) -> None:
        cog = self._cog("anti_spam")
        if not cog:
            return
        saved = cog.get_config(guild_id)
        config = deepcopy(saved or recommended_antispam_config())
        if saved is None:
            config["enabled"] = False
        disabled = set(config.get("disabled_modules", []))
        protections = {}
        for name in ANTISPAM_MODULE_ORDER:
            current = deepcopy(config.get(name, ANTISPAM_MODULE_DEFAULTS[name]))
            if name == "rate_limit":
                current = {"thresholds": current}
            protections[name] = {
                "enabled": name in config and name not in disabled,
                **current,
            }
        guild = self.bot.get_guild(guild_id)
        warnings = cog.configuration_warnings(guild, config) if guild else []
        coverage = cog.coverage_snapshot(guild, config) if guild else {}
        runtime = cog.runtime_snapshot(guild_id)
        modules = dict(runtime.get("modules", {}))
        top_module = max(modules, key=modules.get) if modules else None
        runtime_public = {
            key: value
            for key, value in runtime.items()
            if key not in {"modules", "recent"}
        }
        runtime_public.update(modules=modules, top_module=top_module)
        output.update(
            schema_version=ANTISPAM_SCHEMA_VERSION,
            enabled=bool(config.get("enabled", False)),
            mode=config.get("mode", "enforce"),
            protections=protections,
            punishments=deepcopy(config.get("punishments", {})),
            ignored_channels=_ids(config.get("ignored", [])),
            trusted_roles=_ids(config.get("trusted_roles", [])),
            trusted_members=_ids(config.get("trusted_members", [])),
            health={
                "warnings": warnings,
                "coverage": coverage,
                "runtime": runtime_public,
            },
        )

    async def _read_anti_raid(self, guild_id: int, output: dict[str, Any]) -> None:
        cog = self._cog("anti_raid")
        if not cog:
            return
        config = cog.get_config(guild_id) or recommended_antiraid_config(enabled=False)
        guild = self.bot.get_guild(guild_id)
        runtime = cog.runtime_snapshot(guild_id)
        warnings = cog.permission_warnings(guild, config) if guild else []
        output.update(
            schema_version=ANTIRAID_SCHEMA_VERSION,
            enabled=bool(config.get("enabled", False)),
            mode=config.get("mode", "enforce"),
            protections=deepcopy(config.get("protections", {})),
            response=deepcopy(config.get("response", {})),
            alert_channel_id=_id(config.get("alert_channel_id")),
            trusted_roles=_ids(config.get("trusted_roles", [])),
            trusted_members=_ids(config.get("trusted_members", [])),
            health={"warnings": warnings, "runtime": runtime},
        )

    def _read_welcome(self, guild_id: int, output: dict[str, Any]) -> None:
        cog = self._cog("welcome")
        if not cog:
            return
        config = cog.config.get(guild_id, {})
        output.update(
            enabled=bool(config.get("enabled", False)),
            channel_id=_id(config.get("channel")),
            message=config.get("format", output["message"]),
            use_images=bool(config.get("useimages", False)),
            image_urls=list(config.get("images", [])),
            wait_for_verify=bool(config.get("wait_for_verify", False)),
        )

    def _read_leave(self, guild_id: int, output: dict[str, Any]) -> None:
        cog = self._cog("leave")
        if not cog:
            return
        key = str(guild_id)
        output.update(
            enabled=key in cog.toggle,
            channel_id=_id(cog.channel.get(key)),
            message=cog.format.get(key, output["message"]),
            use_images=key in cog.useimages,
            image_urls=list(cog.images.get(key, [])),
        )

    def _read_restore_roles(self, guild_id: int, output: dict[str, Any]) -> None:
        cog = self._cog("restore_roles")
        if not cog:
            return
        key = str(guild_id)
        output.update(enabled=key in cog.guilds, allow_permissions=key in cog.allow_perms)

    def _read_selfroles(self, guild_id: int, output: dict[str, Any]) -> None:
        cog = self._cog("selfroles")
        if not cog:
            return
        menus = []
        for menu_id, config in cog.config.get(guild_id, {}).items():
            menus.append(
                {
                    "menu_id": str(menu_id),
                    "channel_id": _id(config.get("channel_id")),
                    "role_ids": _ids(config.get("roles", {}).keys()),
                    "text": config.get("text", "Choose your role"),
                    "label": config.get("label", "Select your role"),
                    "style": config.get("style", "dropdown"),
                    "limit": config.get("limit") or 0,
                    "show_percentage": bool(config.get("show_percentage", True)),
                    "show_roles": bool(config.get("show_roles", True)),
                    "editable": config.get("style") != "category",
                }
            )
        output.update(enabled=bool(menus), menus=menus)

    def _read_giveaways(self, guild_id: int, output: dict[str, Any]) -> None:
        cog = self._cog("giveaways")
        if not cog:
            return
        records = list(cog.data.get(str(guild_id), {}).values())
        records.sort(
            key=lambda item: (
                item.get("status") == "active",
                str(item.get("end_at") or item.get("ended_at") or ""),
            ),
            reverse=True,
        )
        active_count = sum(item.get("status") == "active" for item in records)
        total_entries = sum(
            len({int(user_id) for user_id in item.get("entrants", [])})
            for item in records
            if item.get("status") == "active"
        )
        giveaways = []
        for item in records[:25]:
            channel_id = _id(item.get("channel_id"))
            message_id = _id(item.get("message_id"))
            giveaways.append(
                {
                    "id": str(item.get("id") or message_id or ""),
                    "prize": str(item.get("prize") or "Giveaway"),
                    "status": str(item.get("status") or "active"),
                    "channel_id": channel_id,
                    "message_id": message_id,
                    "winner_count": int(item.get("winner_count") or 1),
                    "entry_count": len(
                        {int(user_id) for user_id in item.get("entrants", [])}
                    ),
                    "end_at": item.get("end_at"),
                    "ended_at": item.get("ended_at"),
                    "url": (
                        f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
                        if channel_id and message_id
                        else None
                    ),
                }
            )
        output.update(
            enabled=True,
            active_count=active_count,
            recent_count=max(0, len(records) - active_count),
            total_entries=total_entries,
            giveaways=giveaways,
        )

    def _read_starboard(self, guild_id: int, output: dict[str, Any]) -> None:
        cog = self._cog("starboard")
        if not cog:
            return
        output.update(cog.public_config(guild_id))

    def _read_vc_log(self, guild_id: int, output: dict[str, Any]) -> None:
        cog = self._cog("vc_log")
        if not cog:
            return
        config = cog.config.get(guild_id, {})
        output.update(
            enabled=bool(config),
            channel_id=_id(config.get("channel")),
            keep_clean=bool(config.get("keep_clean", True)),
        )

    async def save(
        self,
        guild_id: int,
        module: str,
        settings: dict[str, Any],
        *,
        actor_id: int,
    ) -> dict[str, Any]:
        guild = self.bot.get_guild(guild_id)
        cog = self._cog(module)
        if guild is None:
            raise ValidationError("Fate is not currently connected to that server.")
        if cog is None:
            raise ValidationError("That feature is temporarily unavailable. Please try again later.")
        actor = await self._actor(guild, actor_id)
        handler = getattr(self, f"_save_{module}")
        await handler(guild, actor, cog, settings)
        payload = await self.get(guild_id, actor_id=actor_id)
        return payload["modules"][module]

    async def modmail_entries(self, guild_id: int, *, actor_id: int) -> dict[str, Any]:
        guild = self.bot.get_guild(guild_id)
        cog = self._cog("modmail")
        if guild is None:
            raise ValidationError("Fate is not currently connected to that server.")
        if cog is None:
            raise ValidationError("Modmail is temporarily unavailable. Please try again later.")

        actor = await self._actor(guild, actor_id)
        if actor.id != guild.owner_id and not actor.guild_permissions.administrator:
            raise ValidationError("Only server administrators can view Modmail conversations.")

        config = cog.config.get(guild_id, cog.config.get(str(guild_id), {}))
        channel_id = config.get("channel_id")
        if not config or not channel_id:
            return {"enabled": False, "channel_id": None, "channel_name": None, "entries": []}

        channel = guild.get_channel(int(channel_id))
        if channel is None or not hasattr(channel, "archived_threads"):
            return {
                "enabled": True,
                "channel_id": _id(channel_id),
                "channel_name": None,
                "entries": [],
                "warning": "The saved Modmail channel no longer exists.",
            }

        threads = []
        seen = set()
        for thread in getattr(channel, "threads", []):
            if thread.id not in seen and str(getattr(thread, "name", "")).startswith("Case "):
                threads.append(thread)
                seen.add(thread.id)
        try:
            async for thread in channel.archived_threads(limit=25):
                if thread.id not in seen and str(getattr(thread, "name", "")).startswith("Case "):
                    threads.append(thread)
                    seen.add(thread.id)
                if len(threads) >= 25:
                    break
        except (discord.Forbidden, discord.HTTPException):
            pass

        threads.sort(
            key=lambda thread: int(getattr(thread, "last_message_id", None) or thread.id),
            reverse=True,
        )
        entries = await asyncio.gather(
            *(self._serialize_modmail_thread(guild, thread) for thread in threads[:25])
        )
        return {
            "enabled": True,
            "channel_id": _id(channel.id),
            "channel_name": getattr(channel, "name", None),
            "entries": entries,
        }

    @staticmethod
    async def _serialize_modmail_thread(guild, thread) -> dict[str, Any]:
        message = None
        last_message_id = getattr(thread, "last_message_id", None)
        if last_message_id:
            try:
                message = await thread.fetch_message(last_message_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass

        preview = ""
        author = None
        updated_at = getattr(thread, "archive_timestamp", None) or getattr(thread, "created_at", None)
        if message is not None:
            preview = (getattr(message, "clean_content", None) or getattr(message, "content", "")).strip()
            if not preview:
                for embed in getattr(message, "embeds", []):
                    preview = (getattr(embed, "description", None) or getattr(embed, "title", None) or "").strip()
                    if preview:
                        break
            message_author = getattr(message, "author", None)
            author = (
                getattr(message_author, "display_name", None)
                or getattr(message_author, "name", None)
                or (str(message_author) if message_author is not None else None)
            )
            updated_at = getattr(message, "created_at", None) or updated_at

        preview = re.sub(r"\s+", " ", preview)
        if len(preview) > 240:
            preview = f"{preview[:237].rstrip()}…"
        name = str(getattr(thread, "name", "Modmail conversation"))
        case_match = re.match(r"Case\s+(\d+)", name, re.IGNORECASE)
        archived = bool(getattr(thread, "archived", False))
        status = "closed" if name.casefold().endswith("(closed)") else "archived" if archived else "active"
        return {
            "id": str(thread.id),
            "name": name,
            "case_number": case_match.group(1) if case_match else None,
            "status": status,
            "message_count": getattr(thread, "message_count", None),
            "latest_author": author,
            "latest_message": preview or None,
            "updated_at": updated_at.isoformat() if updated_at else None,
            "url": f"https://discord.com/channels/{guild.id}/{thread.id}",
        }

    async def _save_autorole(self, guild, actor, cog, settings) -> None:
        self._require_permission(guild, actor, "manage_roles", "Manage Roles")
        self._require_bot_permission(guild, "manage_roles", "Manage Roles")
        roles = {}
        for item in settings["roles"]:
            role = self._role(guild, actor, item["role_id"], "Auto role")
            roles[str(role.id)] = item["delay"]
        config = await cog.get_config(guild.id, fresh=True)
        old_roles = set(config.get("roles", {}))
        new_roles = set(roles) if settings["enabled"] else set()
        for role_id in old_roles - new_roles:
            await cog.cancel_role_timers(guild.id, int(role_id))
        if not settings["enabled"]:
            await cog.config.remove(guild.id)
            return
        config["roles"] = roles
        config["wait_for_verify"] = settings["wait_for_verify"]
        await config.save()

    async def _save_chatfilter(self, guild, actor, cog, settings) -> None:
        self._require_permission(guild, actor, "manage_messages", "Manage Messages")
        if settings["regex"]:
            checker = getattr(sys.modules[cog.__class__.__module__], "safe_regex_query", None)
            if checker and any(checker(pattern) is None for pattern in settings["blacklist"]):
                raise ValidationError(
                    "One or more advanced patterns could slow down the filter. Simplify them and try again."
                )
        current = cog.config.get(guild.id, {})
        preserved = [
            value for value in current.get("ignored", [])
            if guild.get_role(int(value)) is not None or guild.get_member(int(value)) is not None
        ]
        config = {
            "toggle": settings["enabled"],
            "blacklist": settings["blacklist"],
            "whitelist": settings["whitelist"],
            "ignored": list(dict.fromkeys(preserved + settings["ignored_channels"])),
        }
        for enabled, key in (
            (settings["regex"], "regex"),
            (settings["filter_nicks"], "filter_nicks"),
            (settings["filter_bots"], "bots"),
            (settings["filter_webhooks"], "webhooks"),
            (settings["filter_phishing"], "phishing"),
        ):
            if enabled:
                config[key] = True
        cog.config[guild.id] = config
        await cog.config.flush()

    async def _save_logger(self, guild, actor, cog, settings) -> None:
        self._require_permission(guild, actor, "manage_guild", "Manage Server")
        key = str(guild.id)
        current = cog.config.get(key)
        secure_now = bool(current and current.get("secure"))
        configuration = settings.get("configuration")
        secure_next = bool(configuration.get("secure")) if configuration else settings["secure"]
        if actor.id != guild.owner_id and (secure_now or secure_next):
            raise ValidationError("Only the server owner can change owner-only Logging settings.")
        if not settings["enabled"]:
            await cog.disable_for_guild(guild.id)
            return
        channel_id = configuration["channel"] if configuration else settings["channel_id"]
        channel = self._channel(guild, channel_id, "Logging channel")
        permissions = channel.permissions_for(guild.me)
        if not permissions.embed_links or not permissions.attach_files:
            raise ValidationError("Fate needs Embed Links and Attach Files in the logging channel.")
        if current is None:
            await cog.enable_for_guild(guild.id, channel.id)

        if configuration is not None:
            known_events = set(cog.log_types)
            configured_events = (
                set(configuration["channels"])
                | set(configuration["disabled"])
                | set(configuration.get("colors", {}))
            )
            unknown = configured_events - known_events
            if unknown:
                raise ValidationError(f"That Logging event is not available: {sorted(unknown)[0]}")

            old_routes = (current or {}).get("channels", {})
            for event, destination_id in configuration["channels"].items():
                if old_routes.get(event) == destination_id:
                    continue
                destination = guild.get_channel(destination_id)
                if destination is None and hasattr(guild, "get_thread"):
                    destination = guild.get_thread(destination_id)
                if destination is None:
                    raise ValidationError(
                        f"The destination for {event} is not an available channel or thread."
                    )
                destination_permissions = destination.permissions_for(guild.me)
                if not destination_permissions.send_messages:
                    raise ValidationError(f"Fate cannot send {event} records to that destination.")

            cog.config[key] = deepcopy(configuration)
            await cog.save_data()
            archive_manager = getattr(cog, "local_archive", None)
            if archive_manager is not None:
                with suppress(Exception):
                    await archive_manager.prune_guild(
                        key,
                        configuration["local_archive"]["retention_days"],
                    )
            return

        config = cog.config[key]
        enabled_events = {
            event
            for group in settings["groups"]
            for event in cog.categories[group]
        }
        config["channel"] = channel.id
        config["secure"] = settings["secure"]
        config["theme"] = settings["theme"]
        config["ignored_channels"] = settings["ignored_channels"]
        config["disabled"] = sorted(set(cog.log_types) - enabled_events)
        config["local_archive"] = deepcopy(settings["local_archive"])
        await cog.save_data()
        archive_manager = getattr(cog, "local_archive", None)
        if archive_manager is not None:
            with suppress(Exception):
                await archive_manager.prune_guild(
                    key,
                    config["local_archive"]["retention_days"],
                )

    async def _save_modmail(self, guild, actor, cog, settings) -> None:
        self._require_permission(guild, actor, "administrator", "Administrator")
        if not settings["enabled"]:
            await cog.config.remove(guild.id)
            return

        self._require_bot_permission(guild, "view_audit_log", "View Audit Log")
        channel = self._channel(guild, settings["channel_id"], "Modmail channel")
        permissions = channel.permissions_for(guild.me)
        required = (
            ("embed_links", "Embed Links"),
            ("read_message_history", "Read Message History"),
            ("create_public_threads", "Create Public Threads"),
            ("send_messages_in_threads", "Send Messages in Threads"),
            ("manage_threads", "Manage Threads"),
        )
        missing = [label for permission, label in required if not getattr(permissions, permission, False)]
        if missing:
            raise ValidationError(
                f"Fate needs {', '.join(missing)} in the selected Modmail channel."
            )

        old = deepcopy(cog.config.get(guild.id, cog.config.get(str(guild.id), {})))
        if _id(old.get("channel_id")) != _id(channel.id):
            old["references"] = {}
        old["channel_id"] = channel.id
        old.setdefault("references", {})
        old.setdefault("blocked", [])
        cog.config[guild.id] = old
        await cog.config.flush()

    async def _save_anti_spam(self, guild, actor, cog, settings) -> None:
        self._require_permission(guild, actor, "administrator", "Administrator")
        existing = cog.get_config(guild.id)
        config = deepcopy(existing or recommended_antispam_config())
        config["schema_version"] = settings["schema_version"]
        config["enabled"] = settings["enabled"]
        config["mode"] = settings["mode"]
        for name in ANTISPAM_MODULE_ORDER:
            protection = deepcopy(settings["protections"][name])
            enabled = protection.pop("enabled")
            config[name] = (
                protection.pop("thresholds")
                if name == "rate_limit"
                else protection
            )
            set_antispam_module_enabled(config, name, enabled)
        config["punishments"] = deepcopy(settings["punishments"])
        config["ignored"] = settings["ignored_channels"]
        config["trusted_roles"] = settings["trusted_roles"]
        config["trusted_members"] = settings["trusted_members"]
        if existing != config:
            cog.clear_runtime_for_guild(guild.id, clear_telemetry=False)
        cog.config[guild.id] = config
        await cog.config.flush()

    async def _save_anti_raid(self, guild, actor, cog, settings) -> None:
        self._require_permission(guild, actor, "administrator", "Administrator")
        protections = settings["protections"]
        response = settings["response"]
        if settings["enabled"] and protections["destructive_actions"]["enabled"]:
            self._require_bot_permission(guild, "view_audit_log", "View Audit Log")
        if settings["enabled"] and settings["mode"] == "enforce":
            if (
                protections["join_burst"]["enabled"]
                or protections["suspicious_accounts"]["enabled"]
            ):
                permission, label = (
                    ("ban_members", "Ban Members")
                    if response["join_action"] == "temporary_ban"
                    else ("kick_members", "Kick Members")
                )
                self._require_bot_permission(guild, permission, label)
            if protections["destructive_actions"]["enabled"]:
                permission, label = {
                    "strip_roles": ("manage_roles", "Manage Roles"),
                    "kick": ("kick_members", "Kick Members"),
                    "ban": ("ban_members", "Ban Members"),
                }[response["staff_action"]]
                self._require_bot_permission(guild, permission, label)
        if settings.get("alert_channel_id"):
            channel = self._channel(guild, settings["alert_channel_id"], "Incident alert channel")
            permissions = channel.permissions_for(guild.me)
            if not permissions.send_messages or not permissions.embed_links:
                raise ValidationError(
                    "Fate needs Send Messages and Embed Links in the incident alert channel."
                )
        await cog.replace_config(guild, settings)

    async def _save_welcome(self, guild, actor, cog, settings) -> None:
        self._require_permission(guild, actor, "manage_guild", "Manage Server")
        channel = self._channel(guild, settings["channel_id"], "Welcome channel") if settings["enabled"] else None
        if channel and settings["use_images"]:
            permissions = channel.permissions_for(guild.me)
            if not permissions.embed_links or (not settings["image_urls"] and not permissions.attach_files):
                raise ValidationError("Fate needs Embed Links and Attach Files for welcome images.")
        config = deepcopy(cog.config.get(guild.id, {}))
        config.update(
            enabled=settings["enabled"],
            channel=settings["channel_id"],
            format=settings["message"],
            useimages=settings["use_images"],
            images=settings["image_urls"],
            wait_for_verify=settings["wait_for_verify"],
        )
        cog.config[guild.id] = config
        await cog.config.flush()
        if settings["enabled"] and "!inviter" in settings["message"]:
            with suppress(Exception):
                await self.bot.invite_manager.init(guild)

    async def _save_leave(self, guild, actor, cog, settings) -> None:
        self._require_permission(guild, actor, "manage_guild", "Manage Server")
        channel = self._channel(guild, settings["channel_id"], "Leave channel") if settings["enabled"] else None
        if channel and settings["use_images"]:
            permissions = channel.permissions_for(guild.me)
            if not permissions.embed_links or (not settings["image_urls"] and not permissions.attach_files):
                raise ValidationError("Fate needs Embed Links and Attach Files for leave images.")
        key = str(guild.id)
        if settings["enabled"]:
            cog.toggle[key] = "enabled"
        else:
            cog.toggle.pop(key, None)
        cog.channel[key] = settings["channel_id"]
        cog.format[key] = settings["message"]
        if settings["use_images"]:
            cog.useimages[key] = "enabled"
        else:
            cog.useimages.pop(key, None)
        if settings["image_urls"]:
            cog.images[key] = settings["image_urls"]
        else:
            cog.images.pop(key, None)
        await cog.save_data()

    async def _save_restore_roles(self, guild, actor, cog, settings) -> None:
        self._require_permission(guild, actor, "administrator", "Administrator")
        self._require_bot_permission(guild, "manage_roles", "Manage Roles")
        key = str(guild.id)
        old_allow = key in cog.allow_perms
        if old_allow != settings["allow_permissions"] and actor.id != guild.owner_id:
            raise ValidationError("Only the server owner can restore moderation roles.")
        if settings["enabled"] and key not in cog.guilds:
            cog.guilds.append(key)
        elif not settings["enabled"] and key in cog.guilds:
            await cog.disable_module(key)
            return
        if settings["allow_permissions"] and key not in cog.allow_perms:
            cog.allow_perms.append(key)
        elif not settings["allow_permissions"] and key in cog.allow_perms:
            cog.allow_perms.remove(key)
        await cog.save_data()

    async def _save_selfroles(self, guild, actor, cog, settings) -> None:
        self._require_permission(guild, actor, "manage_roles", "Manage Roles")
        self._require_bot_permission(guild, "manage_roles", "Manage Roles")
        action = settings["action"]
        menu_id = _id(settings.get("menu_id"))
        menus = cog.config.get(guild.id, {})
        if action == "delete":
            if not menu_id or menu_id not in menus:
                raise ValidationError("That self-role menu no longer exists.")
            channel = guild.get_channel(menus[menu_id].get("channel_id"))
            if channel:
                with suppress(Exception):
                    message = await channel.fetch_message(int(menu_id))
                    await message.delete()
            await cog.config.remove_sub(guild.id, menu_id)
            if not cog.config.get(guild.id, {}):
                await cog.config.remove(guild.id)
            return

        roles = {}
        old_menu = menus.get(menu_id, {}) if menu_id else {}
        old_roles = old_menu.get("roles", {})
        for role_id in settings["role_ids"]:
            role = self._role(guild, actor, role_id, "Self role")
            roles[str(role.id)] = deepcopy(
                old_roles.get(str(role.id), {"emoji": None, "label": None, "description": None})
            )

        config = {
            "channel_id": settings["channel_id"] or old_menu.get("channel_id"),
            "label": settings["label"],
            "roles": roles,
            "text": settings["text"],
            "style": settings["style"],
            "limit": settings["limit"] or None,
            "show_percentage": settings["show_percentage"],
            "show_roles": settings["show_roles"],
        }
        if action == "create":
            channel = self._channel(guild, settings["channel_id"], "Self-role channel")
            if not channel.permissions_for(guild.me).embed_links:
                raise ValidationError("Fate needs Embed Links in the self-role channel.")
            message = await channel.send(
                settings["text"], allowed_mentions=discord.AllowedMentions.none()
            )
            if guild.id not in cog.config:
                cog.config[guild.id] = {}
            menu_id = str(message.id)
            cog.config[guild.id][menu_id] = config
            try:
                view_type = sys.modules[cog.__class__.__module__].RoleView
                view = view_type(cog, guild.id, message.id)
                await message.edit(
                    content=cog.format_text(guild.id, menu_id),
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await cog.config.flush()
            except Exception:
                await cog.config.remove_sub(guild.id, menu_id)
                with suppress(Exception):
                    await message.delete()
                raise
            return

        if not menu_id or menu_id not in menus:
            raise ValidationError("That self-role menu no longer exists.")
        if old_menu.get("style") == "category":
            raise ValidationError("Combined category menus must be managed from Discord.")
        config["channel_id"] = old_menu["channel_id"]
        cog.config[guild.id][menu_id] = config
        await cog.refresh_menu(guild.id, menu_id)
        await cog.config.flush()

    async def _save_vc_log(self, guild, actor, cog, settings) -> None:
        self._require_permission(guild, actor, "manage_channels", "Manage Channels")
        if not settings["enabled"]:
            await cog.config.remove(guild.id)
            return
        channel = self._channel(guild, settings["channel_id"], "Voice log channel")
        if not channel.permissions_for(guild.me).embed_links:
            raise ValidationError("Fate needs Embed Links in the voice log channel.")
        if settings["keep_clean"] and not channel.permissions_for(guild.me).manage_messages:
            raise ValidationError("Fate needs Manage Messages to keep the voice log channel clean.")
        cog.config[guild.id] = {"channel": channel.id, "keep_clean": settings["keep_clean"]}
        await cog.config.flush()

    async def _save_starboard(self, guild, actor, cog, settings) -> None:
        self._require_permission(guild, actor, "manage_guild", "Manage Server")
        for board in settings["boards"]:
            channel = self._channel(guild, board["channel_id"], f"{board['name']} destination")
            permissions = channel.permissions_for(guild.me)
            if not permissions.embed_links or not permissions.read_message_history:
                raise ValidationError(
                    f"Fate needs Embed Links and Read Message History in the {board['name']} destination."
                )
        try:
            await cog.replace_boards(guild, settings["boards"])
        except ValueError as error:
            raise ValidationError(str(error)) from error
