"""Read and write the MongoDB collections already used by Fate."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

GENERAL_DEFAULTS = {
    "purge_limit": 1000,
    "purge_confirmation": True,
    "warns_channel": None,
    "disabled_channels": [],
    "language": "en",
}
SUPPORTED_LANGUAGES = frozenset({
    "en",
    "es",
    "fr",
    "zh-CN",
    "zh-TW",
    "pt",
    "ar",
    "de",
    "ru",
    "ko",
    "it",
    "pl",
    "ja",
    "sv",
    "uk",
    "nl",
})
PREFIX_DEFAULTS = {"prefix": ".", "override": False}
RANKING_DEFAULTS = {
    "min_xp_per_msg": 1,
    "max_xp_per_msg": 1,
    "first_lvl_xp_req": 250,
    "timeframe": 10,
    "msgs_within_timeframe": 1,
    "disabled_channels": [],
}
MESSAGE_DEFAULTS = {
    "level_up_messages": False,
    "redirect_mod_commands": False,
}
VERIFICATION_DEFAULTS = {
    "enabled": False,
    "channel_id": None,
    "verified_role_id": None,
    "temp_role_id": None,
    "delete_after": True,
    "log_channel": None,
    "kick_on_fail": False,
    "auto_start": False,
    "time_limit": 45,
    "panel_active": False,
}


def _without_id(document: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(document or {})
    result.pop("_id", None)
    return result


def _with_defaults(document: dict[str, Any] | None, defaults: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(defaults)
    result.update(_without_id(document))
    return result


def _public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Keep Discord snowflakes lossless when JSON is consumed by JavaScript."""
    result = deepcopy(settings)
    general = result["general"]
    if general.get("warns_channel"):
        general["warns_channel"] = str(general["warns_channel"])
    general["disabled_channels"] = [
        str(channel_id) for channel_id in general.get("disabled_channels", [])
    ]

    ranking = result["ranking"]
    ranking["disabled_channels"] = [
        str(channel_id) for channel_id in ranking.get("disabled_channels", [])
    ]

    messages = result["messages"]
    for key in ("level_up_messages", "redirect_mod_commands"):
        if type(messages.get(key)) is int:
            messages[key] = str(messages[key])

    verification = result["verification"]
    for key in ("channel_id", "verified_role_id", "temp_role_id", "log_channel"):
        if verification.get(key):
            verification[key] = str(verification[key])
    return result


class MongoSettingsStore:
    def __init__(self, uri: str, database: str):
        self.client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=4000)
        self.database = self.client[database]

    async def ping(self) -> None:
        await self.client.admin.command("ping")

    async def close(self) -> None:
        self.client.close()

    async def get(self, guild_id: int) -> dict[str, Any]:
        general, prefix, ranking, messages, verification = await self._documents(guild_id)
        prefix_config = _with_defaults(prefix, PREFIX_DEFAULTS)
        verification_config = _with_defaults(verification, VERIFICATION_DEFAULTS)
        verification_config["enabled"] = verification is not None
        verification_config["panel_active"] = bool(
            verification_config.pop("panel_message_id", None)
        )
        return _public_settings({
            "general": _with_defaults(general, GENERAL_DEFAULTS),
            "prefix": {
                "value": prefix_config["prefix"],
                "allow_personal": not bool(prefix_config["override"]),
            },
            "ranking": _with_defaults(ranking, RANKING_DEFAULTS),
            "messages": _with_defaults(messages, MESSAGE_DEFAULTS),
            "verification": verification_config,
        })

    async def _documents(self, guild_id: int):
        import asyncio

        return await asyncio.gather(
            self.database["settings"].find_one({"_id": guild_id}),
            self.database["GuildPrefixes"].find_one({"_id": guild_id}),
            self.database["ranking"].find_one({"_id": guild_id}),
            self.database["messages"].find_one({"_id": guild_id}),
            self.database["verification"].find_one({"_id": guild_id}),
        )

    async def save(
        self,
        guild_id: int,
        settings: dict[str, Any],
        *,
        actor_id: int | None = None,
        refresh_verification: bool = False,
    ) -> dict[str, Any]:
        import asyncio

        del actor_id, refresh_verification
        general = settings["general"]
        prefix = settings["prefix"]
        ranking = settings["ranking"]
        messages = settings["messages"]
        verification = settings["verification"]
        verification_write = {
            key: value
            for key, value in verification.items()
            if key not in {"enabled", "panel_active"}
        }
        verification_operation = (
            self.database["verification"].update_one(
                {"_id": guild_id}, {"$set": verification_write}, upsert=True
            )
            if verification["enabled"]
            else self.database["verification"].delete_one({"_id": guild_id})
        )
        await asyncio.gather(
            self.database["settings"].update_one(
                {"_id": guild_id}, {"$set": general}, upsert=True
            ),
            self.database["GuildPrefixes"].update_one(
                {"_id": guild_id},
                {"$set": {"prefix": prefix["value"], "override": not prefix["allow_personal"]}},
                upsert=True,
            ),
            self.database["ranking"].update_one(
                {"_id": guild_id}, {"$set": ranking}, upsert=True
            ),
            self.database["messages"].update_one(
                {"_id": guild_id}, {"$set": messages}, upsert=True
            ),
            verification_operation,
        )
        return await self.get(guild_id)

    async def get_profile_style(self, user_id: int) -> dict[str, Any]:
        profile = await self.database["profiles"].find_one({"_id": user_id})
        return _without_id(profile)


class MemorySettingsStore:
    """Local development store. It is enabled only by DASHBOARD_DEV_MODE."""

    def __init__(self):
        self.settings: dict[int, dict[str, Any]] = {}

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def get(self, guild_id: int) -> dict[str, Any]:
        if guild_id not in self.settings:
            self.settings[guild_id] = {
                "general": deepcopy(GENERAL_DEFAULTS),
                "prefix": {"value": ".", "allow_personal": True},
                "ranking": deepcopy(RANKING_DEFAULTS),
                "messages": deepcopy(MESSAGE_DEFAULTS),
                "verification": deepcopy(VERIFICATION_DEFAULTS),
            }
        return _public_settings(self.settings[guild_id])

    async def save(
        self,
        guild_id: int,
        settings: dict[str, Any],
        *,
        actor_id: int | None = None,
        refresh_verification: bool = False,
    ) -> dict[str, Any]:
        del actor_id, refresh_verification
        self.settings[guild_id] = deepcopy(settings)
        return await self.get(guild_id)

    async def get_profile_style(self, _user_id: int) -> dict[str, Any]:
        return {"title": "Charting the stars with Fate"}


class BotSettingsStore:
    """In-process adapter that keeps Fate's live caches and MongoDB in sync."""

    def __init__(self, bot):
        self.bot = bot

    async def ping(self) -> None:
        await self.bot.aio_mongo.command("ping")

    async def close(self) -> None:
        return None

    async def _actor(self, guild, actor_id: int | None):
        from .validation import ValidationError

        if not actor_id:
            raise ValidationError("Fate could not verify your Discord account. Please sign in again.")
        actor = guild.get_member(actor_id)
        if actor is None:
            try:
                actor = await guild.fetch_member(actor_id)
            except Exception as error:
                raise ValidationError(
                    "Fate could not confirm your membership in that server."
                ) from error
        permissions = actor.guild_permissions
        if not (
            actor.id == guild.owner_id
            or permissions.administrator
            or permissions.manage_guild
        ):
            raise ValidationError("You need Manage Server permission to save these settings.")
        return actor

    @staticmethod
    def _validate_channels(guild, settings: dict[str, Any]) -> None:
        from .validation import ValidationError

        channel_ids = []
        general = settings["general"]
        if general.get("warns_channel"):
            channel_ids.append((general["warns_channel"], "Warning channel", True))
        channel_ids.extend(
            (channel_id, "Disabled channel", False)
            for channel_id in general.get("disabled_channels", [])
        )
        channel_ids.extend(
            (channel_id, "XP-disabled channel", False)
            for channel_id in settings.get("ranking", {}).get("disabled_channels", [])
        )
        for key, label in (
            ("level_up_messages", "Level-up channel"),
            ("redirect_mod_commands", "Moderation response channel"),
        ):
            value = settings["messages"].get(key)
            if type(value) is int:
                channel_ids.append((value, label, True))

        for channel_id, label, needs_send in channel_ids:
            channel = guild.get_channel(channel_id)
            if channel is None or not hasattr(channel, "permissions_for"):
                raise ValidationError(f"The selected {label.lower()} is not in this server.")
            if needs_send and not channel.permissions_for(guild.me).send_messages:
                raise ValidationError(f"Fate cannot send messages in the selected {label.lower()}.")

    async def _preflight_verification(
        self,
        guild_id: int,
        incoming: dict[str, Any],
        cog,
        *,
        actor,
        refresh: bool,
    ) -> None:
        from .validation import ValidationError

        if not incoming["enabled"]:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise ValidationError("Fate is not currently connected to that server.")
        old = dict(cog.get_config(guild_id) or {})
        public_keys = {
            "channel_id",
            "verified_role_id",
            "temp_role_id",
            "delete_after",
            "log_channel",
            "kick_on_fail",
            "auto_start",
            "time_limit",
        }
        if old and not refresh and all(
            old.get(key) == incoming.get(key) for key in public_keys
        ):
            return
        config = {
            key: value
            for key, value in incoming.items()
            if key not in {"enabled", "panel_active"}
        }
        verified_role = guild.get_role(config["verified_role_id"])
        restricted_role = guild.get_role(config["temp_role_id"] or 0)
        if verified_role and (error := cog.validate_role(guild, verified_role, actor)):
            raise ValidationError(error)
        if restricted_role and (
            error := cog.validate_role(guild, restricted_role, actor)
        ):
            raise ValidationError(error)
        warnings = cog.configuration_warnings(guild, config)
        if warnings:
            raise ValidationError(warnings[0])

    async def get(self, guild_id: int) -> dict[str, Any]:
        settings_cog = self.bot.get_cog("Settings")
        ranking_cog = self.bot.get_cog("Ranking")
        messages_cog = self.bot.get_cog("Messages")
        verification_cog = self.bot.get_cog("Verification")
        if not settings_cog or not ranking_cog or not messages_cog or not verification_cog:
            raise RuntimeError(
                "Fate's settings, ranking, messages, and verification modules must be loaded."
            )

        general = settings_cog.get_config(guild_id)
        prefix = self.bot.guild_prefixes.get(guild_id, PREFIX_DEFAULTS)
        ranking = await ranking_cog.config[guild_id] or deepcopy(RANKING_DEFAULTS)
        messages = messages_cog.config.get(guild_id, deepcopy(MESSAGE_DEFAULTS))
        raw_verification = verification_cog.get_config(guild_id)
        verification = deepcopy(VERIFICATION_DEFAULTS)
        if raw_verification is not None:
            verification.update(
                {
                    key: value
                    for key, value in raw_verification.items()
                    if key in VERIFICATION_DEFAULTS
                }
            )
            verification["enabled"] = True
            verification["panel_active"] = bool(
                raw_verification.get("panel_message_id")
            )
        return _public_settings({
            "general": _with_defaults(general, GENERAL_DEFAULTS),
            "prefix": {
                "value": prefix.get("prefix", "."),
                "allow_personal": not bool(prefix.get("override", False)),
            },
            "ranking": _with_defaults(dict(ranking), RANKING_DEFAULTS),
            "messages": _with_defaults(messages, MESSAGE_DEFAULTS),
            "verification": verification,
        })

    async def save(
        self,
        guild_id: int,
        settings: dict[str, Any],
        *,
        actor_id: int | None = None,
        refresh_verification: bool = False,
    ) -> dict[str, Any]:
        settings_cog = self.bot.get_cog("Settings")
        ranking_cog = self.bot.get_cog("Ranking")
        messages_cog = self.bot.get_cog("Messages")
        verification_cog = self.bot.get_cog("Verification")
        if not settings_cog or not ranking_cog or not messages_cog or not verification_cog:
            raise RuntimeError("Fate's settings modules are unavailable.")

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            from .validation import ValidationError

            raise ValidationError("Fate is not currently connected to that server.")
        actor = await self._actor(guild, actor_id)
        self._validate_channels(guild, settings)
        await self._preflight_verification(
            guild_id,
            settings["verification"],
            verification_cog,
            actor=actor,
            refresh=refresh_verification,
        )

        general = deepcopy(settings["general"])
        prefix = settings["prefix"]
        messages = deepcopy(settings["messages"])
        settings_cog.config[guild_id] = general
        await settings_cog.config.flush()

        prefix_config = {
            "prefix": prefix["value"],
            "override": not prefix["allow_personal"],
        }
        await self.bot.aio_mongo["GuildPrefixes"].update_one(
            {"_id": guild_id}, {"$set": prefix_config}, upsert=True
        )
        self.bot.guild_prefixes[guild_id] = prefix_config

        ranking = await ranking_cog.config[guild_id]
        ranking.clear()
        ranking.update(deepcopy(settings["ranking"]))
        await ranking.save()

        messages_cog.config[guild_id] = messages
        await messages_cog.config.flush()

        await self._save_verification(
            guild_id,
            settings["verification"],
            verification_cog,
            actor_id=actor_id,
            refresh=refresh_verification,
        )
        return await self.get(guild_id)

    async def _save_verification(
        self,
        guild_id: int,
        incoming: dict[str, Any],
        cog,
        *,
        actor_id: int | None,
        refresh: bool,
    ) -> None:
        # Imported lazily to avoid a module cycle with the defaults above.
        from .validation import ValidationError

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise ValidationError("Fate is not currently connected to that server.")

        old = dict(cog.get_config(guild_id) or {})
        if not incoming["enabled"]:
            if old:
                await cog.disable_for_guild(guild)
            return

        public_keys = {
            "channel_id",
            "verified_role_id",
            "temp_role_id",
            "delete_after",
            "log_channel",
            "kick_on_fail",
            "auto_start",
            "time_limit",
        }
        if old and not refresh and all(
            old.get(key) == incoming.get(key) for key in public_keys
        ):
            return

        config = {
            key: value
            for key, value in incoming.items()
            if key not in {"enabled", "panel_active"}
        }
        config["panel_message_id"] = old.get("panel_message_id")
        changed_channel = old.get("channel_id") != config["channel_id"]
        if changed_channel:
            config["panel_message_id"] = None

        actor = guild.get_member(actor_id) if actor_id else None
        if actor is None and actor_id:
            try:
                actor = await guild.fetch_member(actor_id)
            except Exception as error:
                raise ValidationError(
                    "Fate could not confirm your role permissions in that server."
                ) from error

        verified_role = guild.get_role(config["verified_role_id"])
        restricted_role = guild.get_role(config["temp_role_id"] or 0)
        if verified_role and (error := cog.validate_role(guild, verified_role, actor)):
            raise ValidationError(error)
        if restricted_role and (
            error := cog.validate_role(guild, restricted_role, actor)
        ):
            raise ValidationError(error)

        warnings = cog.configuration_warnings(guild, config)
        if warnings:
            raise ValidationError(warnings[0])

        was_enabled = bool(old)
        cog.config[guild_id] = config
        await cog.config.flush()

        should_publish = (
            refresh
            or not was_enabled
            or changed_channel
            or not old.get("panel_message_id")
        )
        if should_publish:
            await cog.publish_panel(guild)
        if changed_channel and old:
            await cog.delete_panel(guild, old)
        if not was_enabled:
            await self.bot.create_log(
                message=f"!on **Verification** - `{guild}`",
                channel="module_log",
                embedded=True,
                color="green",
            )

    async def get_profile_style(self, user_id: int) -> dict[str, Any]:
        ranking_cog = self.bot.get_cog("Ranking")
        if ranking_cog:
            return dict(await ranking_cog.profile[user_id] or {})
        profile = await self.bot.aio_mongo["profiles"].find_one({"_id": user_id})
        return _without_id(profile)
