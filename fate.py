"""
Fate
~~~~~

Main file intended for starting the bot

:copyright: (C) 2018-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import json
import logging
import os
import sys
import traceback
from base64 import b64decode, b64encode
from contextlib import suppress
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from time import time, monotonic
from typing import Any, Dict, Optional, Union
from urllib.parse import quote_plus

import aiohttp
import aiomysql
import discord
import psutil
import pymongo
import pymysql
from aiohttp import web
from cryptography.fernet import Fernet, InvalidToken
from discord import NotFound, Forbidden, HTTPException
from discord import app_commands
from discord.ext import commands
from discord_sentry_reporting import use_sentry
from motor.motor_asyncio import AsyncIOMotorClient
from termcolor import cprint

from botutils import (
    get_prefixes_async,
    Utils,
    FileCache,
    Cooldown,
    TemporaryList,
    colors,
    emojis,
)
from botutils.active_servers import active_guild_ids
from botutils.custom_logging import Logging
from botutils.discord_rate_limits import DiscordRateLimitTracker
from botutils.instance_handoff import InstanceHandoff
from botutils.local_databases import ensure_local_databases, mysql_settings
from botutils.localization import LocalizationManager
from botutils.log_paths import DISCORD_LOG_PATH, ensure_logging_directory
from botutils.resources import Cache as LegacyResourceCache
from botutils.resources import load_builtin_cache_snapshots
from botutils.slash_commands import install_legacy_slash_commands
from botutils.telemetry import (
    MongoTelemetryListener,
    TelemetryCollector,
    TelemetryStore,
    instrument_mysql_pool,
    merge_mongo_listeners,
    telemetry_path_for_config,
)
from botutils.topgg_votes import TopggVotes
from botutils.uptime import UptimeTracker
from checks import checks
from organism.fate_service import build_organism_service

PRIMARY_ONLY_EXTENSIONS = frozenset({"polis", "dev", "backup"})
ACTIVE_SERVER_METRIC_REFRESH_SECONDS = 5 * 60

if __name__ == "__main__":
    # Cogs import `fate` for shared types. Reuse this running module instead of
    # executing fate.py a second time under a different module name.
    sys.modules.setdefault("fate", sys.modules[__name__])


def latency_milliseconds(latency: float) -> Optional[int]:
    """Convert a valid Discord latency to milliseconds for status clients."""
    return round(latency * 1000) if isfinite(latency) else None


def load_auth_key() -> bytes:
    """Load the auth-encryption key from the environment or an external file."""
    key = os.environ.get("FATE_AUTH_KEY", "").strip()
    if not key:
        configured_path = os.environ.get("FATE_AUTH_KEY_PATH")
        key_path = Path(
            os.path.expandvars(
                os.path.expanduser(
                    configured_path or str(Path.home() / "Desktop" / "fate_auth.key")
                )
            )
        )
        try:
            key = key_path.read_text(encoding="utf-8-sig").strip()
        except OSError as error:
            raise RuntimeError(
                "Encrypted auth configuration requires FATE_AUTH_KEY or "
                f"a readable FATE_AUTH_KEY_PATH (looked for {key_path})."
            ) from error
    try:
        Fernet(key.encode())
    except (ValueError, TypeError) as error:
        raise ValueError("FATE_AUTH_KEY is not a valid Fernet key.") from error
    return key.encode()


def load_auth_config(path: Union[str, os.PathLike]) -> Dict[str, Any]:
    """Load a plaintext or Fernet-encrypted authentication config."""
    auth_path = Path(path)
    raw = auth_path.read_bytes().strip()
    if not raw:
        raise ValueError(f"Authentication config is empty: {auth_path}")

    try:
        auth = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        try:
            decrypted = Fernet(load_auth_key()).decrypt(raw)
        except (InvalidToken, ValueError) as decrypt_error:
            raise ValueError(
                f"{auth_path} is neither valid JSON nor decryptable with "
                "the configured Fate auth key"
            ) from decrypt_error
        try:
            auth = json.loads(decrypted)
        except (json.JSONDecodeError, UnicodeDecodeError) as decrypt_json_error:
            raise ValueError(
                f"Decrypted authentication config is not valid JSON: {auth_path}"
            ) from decrypt_json_error

    if not isinstance(auth, dict):
        raise TypeError(
            f"Authentication config must contain a JSON object: {auth_path}"
        )
    return auth


def load_bot_token(
    auth: Dict[str, Any], token_id: str, token_path: Optional[str] = None
) -> str:
    """Load a Discord token from an external file or the auth config fallback."""
    if token_path:
        resolved_path = Path(os.path.expandvars(os.path.expanduser(token_path)))
        try:
            token = resolved_path.read_text(encoding="utf-8-sig").strip()
        except OSError as error:
            raise RuntimeError(
                f"Unable to read Discord token file: {resolved_path}"
            ) from error
        if not token:
            raise ValueError(f"Discord token file is empty: {resolved_path}")
        return token

    token = auth["tokens"][token_id].strip()
    if not token:
        raise ValueError(
            "Discord token is empty; set FATE_TOKEN_PATH or configure the selected token"
        )
    return token


def resolve_external_token_path() -> Optional[Path]:
    """Return the configured token file, or Desktop/token.txt when present."""
    configured_path = os.environ.get("FATE_TOKEN_PATH")
    if configured_path:
        return Path(os.path.expandvars(os.path.expanduser(configured_path)))
    desktop_token = Path.home() / "Desktop" / "token.txt"
    return desktop_token if desktop_token.exists() else None


class Fate(commands.AutoShardedBot):
    loop: asyncio.BaseEventLoop
    content_limit: int = 3800

    def __init__(self, **options):
        self.external_token_path = resolve_external_token_path()
        explicit_config_path = os.environ.get("FATE_CONFIG_PATH")
        repository_root = Path(__file__).resolve().parent
        self.main_config_path = (repository_root / "data" / "config.json").resolve()
        test_config_path = repository_root / "data" / "config.test.json"
        self.using_test_profile = bool(
            self.external_token_path
            and not explicit_config_path
            and test_config_path.exists()
        )
        selected_config_path = (
            str(test_config_path)
            if self.using_test_profile
            else explicit_config_path or "./data/config.json"
        )
        expanded_config_path = Path(
            os.path.expandvars(os.path.expanduser(selected_config_path))
        )
        if not expanded_config_path.is_absolute():
            expanded_config_path = repository_root / expanded_config_path
        self.config_path = (
            expanded_config_path.parent.resolve() / expanded_config_path.name
        )
        if self.config_path.is_symlink():
            raise RuntimeError("Fate's config path cannot be a symbolic link")
        with self.config_path.open("r", encoding="utf-8") as file:
            self.config: Dict[str, Any] = json.load(file)
        website_config = self.config.get("website", {})
        if not isinstance(website_config, dict):
            raise TypeError("config.json website must be an object")
        configured_dashboard_host = website_config.get("host", "127.0.0.1")
        configured_dashboard_port = website_config.get("port", 16420)
        if not isinstance(configured_dashboard_host, str) or not configured_dashboard_host.strip():
            raise TypeError("config.json website.host must be a non-empty string")
        if type(configured_dashboard_port) is not int or not 1 <= configured_dashboard_port <= 65535:
            raise TypeError("config.json website.port must be an integer between 1 and 65535")
        self.dashboard_host = os.getenv(
            "DASHBOARD_HOST", configured_dashboard_host.strip()
        )
        self.dashboard_port = int(
            os.getenv("DASHBOARD_PORT", str(configured_dashboard_port))
        )
        if self.config_path == self.main_config_path:
            main_config = self.config
        else:
            with self.main_config_path.open("r", encoding="utf-8") as file:
                main_config = json.load(file)
        debug_mode = main_config.get("debug_mode", False)
        if type(debug_mode) is not bool:
            raise TypeError("config.json debug_mode must be true or false")
        debug_logging = main_config.get("debug_logging", False)
        if type(debug_logging) is not bool:
            raise TypeError("config.json debug_logging must be true or false")
        self.debug_mode = debug_mode
        self.debug_logging = debug_logging
        self.organism = build_organism_service(
            self.config.get("organism"),
            repository_root=repository_root,
        )

        self.instance_handoff = InstanceHandoff(self.config_path)
        self._instance_watch_task: asyncio.Task | None = None
        datastore_path = Path(
            os.path.expandvars(os.path.expanduser(self.config["datastore_location"]))
        )
        if not datastore_path.is_absolute():
            datastore_path = repository_root / datastore_path
        uptime_state = f"uptime-{self.config_path.stem}.runtime.json"
        self.uptime_tracker = UptimeTracker(datastore_path / uptime_state)
        self._uptime_task: asyncio.Task | None = None
        telemetry_path = telemetry_path_for_config(
            self.config_path,
            self.config,
            repository_root=repository_root,
        )
        self.telemetry = TelemetryCollector(TelemetryStore(telemetry_path))
        self.discord_rate_limit_tracker = DiscordRateLimitTracker(
            self.telemetry, options.get("http_trace")
        )
        options["http_trace"] = self.discord_rate_limit_tracker.trace
        self._mongo_telemetry_listener = MongoTelemetryListener(self.telemetry)
        self._telemetry_task: asyncio.Task | None = None
        self._active_server_count: int | None = None
        self._active_server_count_sampled_at = 0.0
        self._top_gg_url = str(self.config.get("top.gg", "")).rstrip("/")

        Path(self.config["datastore_location"]).mkdir(parents=True, exist_ok=True)
        with open(
            "./data/userdata/disabled_commands.json", "r", encoding="utf-8"
        ) as file:
            self.disabled_commands = json.load(file)

        # Runtime state must belong to this bot instance, not the Fate class.
        self.auth: Dict[str, Any] = {}
        self.app_is_running = False
        self.pool = None
        self._pool_ready = asyncio.Event()
        self.blocked = []
        self.suppressed = TemporaryList(keep_items_for=15)
        self.restricted = {}
        self.toggles = {}
        self.file_locks = {}
        self.operation_locks = []
        self.tasks = {}
        self.filtered_messages = {}
        self.views = {}
        self.login_errors = []
        self.logs = []
        self.ignored_locations = []
        self._mongo_client = None
        self._aio_mongo_client = None
        self._resource_cache_snapshots = None
        self._resource_cache_hydration_lock = asyncio.Lock()
        self._resource_session = None
        self._app_runner = None
        self._app_site = None
        self._status_server_restart_lock = asyncio.Lock()
        self._status_server_control_task = None
        self.status_server_restart_path = (
            repository_root / "data" / f"fate-status-server-{self.dashboard_port}.restart"
        )
        self._tree_synced = False
        self._application_command_sync_at = 0.0
        self._status_command_usage = 0
        self._status_command_usage_at = 0.0
        self._status_command_usage_month = None
        self.translator = None

        configured_owner_ids = {
            self.config["bot_owner_id"],
            *self.config["bot_owner_ids"],
        }
        self.theme_color = self.config["theme_color"]

        # Ping application
        self.app = web.Application()
        self.app.router.add_get("/ping", self.acknowledge)
        self.app.router.add_get("/status", self.status)
        self.topgg_votes = TopggVotes(self)
        self.app.router.add_post("/webhooks/topgg", self.topgg_votes.handle)

        self.allow_user_mentions = discord.AllowedMentions(
            users=True, roles=False, everyone=False
        )

        # Set the oauth_url for users to invite the bot with
        perms = discord.Permissions(0)
        perms.update(**self.config["bot_invite_permissions"])
        self.invite_url = discord.utils.oauth_url(
            client_id=self.config["bot_user_id"],
            permissions=perms,
            scopes=("bot", "applications.commands"),
        )
        self.user_install_url = (
            "https://discord.com/oauth2/authorize"
            f"?client_id={self.config['bot_user_id']}"
            "&integration_type=1&scope=applications.commands"
        )

        super().__init__(
            command_prefix=get_prefixes_async,
            intents=discord.Intents.all(),
            activity=discord.Game(name=self.config["activity_status"]),
            max_messages=self.config["max_cached_messages"],
            owner_ids=configured_owner_ids,
            allowed_contexts=app_commands.AppCommandContext(
                guild=True,
                dm_channel=True,
                private_channel=True,
            ),
            allowed_installs=app_commands.AppInstallationType(
                guild=True,
                user=True,
            ),
            **options,
        )

    @property
    def instance_role(self) -> str:
        """Return the role implied by the current debug-mode setting."""
        return "secondary" if self.debug_mode else "primary"

    @property
    def vote_url(self) -> Optional[str]:
        """Expose public voting only while Fate is outside debug mode."""
        if self.debug_mode or not self._top_gg_url:
            return None
        return f"{self._top_gg_url}/vote"

    async def setup_hook(self):
        self.log = Logging(bot=self)
        self.utils = Utils(self)
        self.cache = FileCache(self)
        self.telemetry.start()
        self._telemetry_task = asyncio.create_task(
            self._sample_runtime_telemetry(),
            name="fate-runtime-telemetry",
        )

        started_at = getattr(self, "start_time", datetime.now(tz=timezone.utc))
        try:
            await asyncio.to_thread(self.uptime_tracker.start, started_at)
        except OSError as error:
            self.log.critical(f"Couldn't persist uptime history: {error}")
        self._uptime_task = asyncio.create_task(
            self._checkpoint_uptime(),
            name="fate-uptime-checkpoint",
        )

        self.log.info(f"Instance role: {self.instance_role}", color="yellow")
        if self.organism is None:
            self.log.info("Organism body service: disabled", color="yellow")
        else:
            association_state = (
                "shared sensory recall enabled"
                if self.organism.associations_enabled
                else "guild sectors only"
            )
            discovery_state = (
                "discovery configured"
                if self.organism.discovery_enabled
                else "discovery disabled"
            )
            self.log.info(
                "Organism body service: standby "
                f"({association_state}; {discovery_state})",
                color="yellow",
            )
        self._instance_watch_task = asyncio.create_task(
            self.instance_handoff.watch_for_replacement(
                self._replace_with_new_instance
            ),
            name="fate-instance-handoff",
        )
        skipped = self.primary_only_extensions()
        if skipped:
            self.log.info(
                "Secondary instance skipped primary-only extensions: "
                + ", ".join(skipped),
                color="yellow",
            )
        self._resource_cache_snapshots = await asyncio.to_thread(
            load_builtin_cache_snapshots, self
        )
        extensions = self.configured_extensions()
        if extensions:
            self.log.info("Loading initial cogs", color="yellow")
            await self.load_extensions(*extensions)
            self.log.info(
                "Finished loading initial cogs\nAuthenticating with token..",
                color="yellow",
            )

        self.translator = LocalizationManager(self)
        self.translator.install()

        self.restricted = self.utils.cache("restricted")
        self._resource_cache_snapshots = None
        self.attrs = self.utils.attrs
        # setup_hook runs after Discord authenticates but before the gateway is
        # connected. Sync here so every loaded hybrid/application command is
        # present before the bot becomes ready. Offline probes have no user.
        if self.user is not None:
            await self.sync_application_commands()

    async def _checkpoint_uptime(self) -> None:
        """Checkpoint availability so an unclean exit loses at most one minute."""
        while True:
            await asyncio.sleep(60)
            try:
                await asyncio.to_thread(
                    self.uptime_tracker.heartbeat,
                    datetime.now(tz=timezone.utc),
                )
            except OSError as error:
                self.log.critical(f"Couldn't checkpoint uptime history: {error}")

    async def _sample_runtime_telemetry(self) -> None:
        """Persist content-free runtime gauges every five seconds."""
        await self.wait_until_ready()
        while not self.is_closed():
            self.telemetry.gauge("servers", len(self.guilds))
            self.telemetry.gauge(
                "users", sum(guild.member_count or 0 for guild in self.guilds)
            )
            sampled_at = monotonic()
            if (
                self.pool is not None
                and (
                    self._active_server_count is None
                    or sampled_at - self._active_server_count_sampled_at
                    >= ACTIVE_SERVER_METRIC_REFRESH_SECONDS
                )
            ):
                self._active_server_count_sampled_at = sampled_at
                try:
                    self._active_server_count = len(await active_guild_ids(self))
                except Exception as error:
                    self.log.warning(
                        f"Couldn't sample the active-server metric: {error}"
                    )
            if self._active_server_count is not None:
                self.telemetry.gauge("active_servers", self._active_server_count)
            await asyncio.sleep(5)

    async def _replace_with_new_instance(self, request: dict) -> None:
        requester_pid = request.get("requester_pid", "unknown")
        self.log.info(
            f"A new Fate instance (PID {requester_pid}) is starting; logging out this instance.",
            color="yellow",
        )
        await self.close()

    def dispatch(self, event_name: str, /, *args, **kwargs) -> None:
        # Metrics retain only registered command labels and numeric totals.  Bot
        # messages are counted from Discord's MESSAGE_CREATE acknowledgement,
        # never by inspecting or retaining message content.
        if event_name == "message" and args and self.user is not None:
            author = getattr(args[0], "author", None)
            if getattr(author, "id", None) == self.user.id:
                self.telemetry.increment("messages_sent")
        elif event_name == "command" and args:
            context = args[0]
            if getattr(context, "interaction", None) is None:
                command = getattr(context, "command", None)
                self.telemetry.increment(
                    "commands", dimension=getattr(command, "qualified_name", "")
                )
        elif event_name == "interaction" and args:
            interaction = args[0]
            if getattr(interaction, "type", None) == discord.InteractionType.application_command:
                data = getattr(interaction, "data", None)
                if isinstance(data, dict):
                    names = [data.get("name")]
                    options = data.get("options")
                    while isinstance(options, list) and options:
                        option = options[0]
                        if not isinstance(option, dict) or option.get("type") not in {1, 2}:
                            break
                        names.append(option.get("name"))
                        options = option.get("options")
                    self.telemetry.increment(
                        "commands",
                        dimension=" ".join(
                            name for name in names if isinstance(name, str)
                        ),
                    )
        # Discord interaction response routes contain only a short-lived token,
        # so retain its guild before the command or component callback runs.
        if event_name == "interaction" and args and self.translator is not None:
            self.translator.register_interaction(args[0])
        super().dispatch(event_name, *args, **kwargs)

    async def sync_application_commands(self, *, force: bool = False):
        """Publish the current application-command tree and log the result."""
        if self._tree_synced and not force:
            return self.tree.get_commands()
        self.prepare_application_command_tree()
        self.configure_application_command_scopes()
        self._application_command_sync_at = monotonic()
        try:
            synced = await self.tree.sync()
        except Exception:
            self._tree_synced = False
            self.log.critical(
                f"Couldn't sync application commands``````{traceback.format_exc()}"
            )
            return None
        self._tree_synced = True
        self.log.info(
            f"Synced {len(synced)} global application commands",
            color="green",
        )
        return synced

    def prepare_application_command_tree(self) -> int:
        """Add grouped slash adapters for public legacy prefix commands."""
        return install_legacy_slash_commands(self)

    def configure_application_command_scopes(self) -> None:
        """Keep guild-only hybrid commands out of user installations and DMs."""
        guild_contexts = app_commands.AppCommandContext(
            guild=True,
            dm_channel=False,
            private_channel=False,
        )
        guild_installs = app_commands.AppInstallationType(guild=True, user=False)
        for command in self.tree.get_commands():
            if getattr(command, "guild_only", False):
                command.allowed_contexts = guild_contexts
                command.allowed_installs = guild_installs

    def all_configured_extensions(self) -> tuple:
        """Return every configured cog path in its declared load order."""
        return tuple(
            f"{category}.{cog}"
            for category, cogs in self.config.get("extensions", {}).items()
            for cog in cogs
        )

    def primary_only_extensions(self) -> tuple:
        """Return configured extensions disabled while Fate is in debug mode."""
        if not self.debug_mode:
            return ()
        return tuple(
            extension
            for extension in self.all_configured_extensions()
            if extension.rsplit(".", 1)[-1] in PRIMARY_ONLY_EXTENSIONS
        )

    def configured_extensions(self) -> tuple:
        """Return the extensions this primary or secondary instance may load."""
        skipped = set(self.primary_only_extensions())
        return tuple(
            extension
            for extension in self.all_configured_extensions()
            if extension not in skipped
        )

    async def acknowledge(self, _request) -> web.Response:
        return web.Response(text="pong")

    async def restart_status_server(self) -> None:
        """Recycle only Fate's HTTP listener, leaving Discord and stores alive."""
        async with self._status_server_restart_lock:
            if self._app_runner is None:
                raise RuntimeError("The Fate status application is not initialized")
            previous_site = self._app_site
            self._app_site = None
            self.app_is_running = False
            if previous_site is not None:
                await previous_site.stop()
            replacement = web.TCPSite(
                self._app_runner,
                host=self.dashboard_host,
                port=self.dashboard_port,
            )
            await replacement.start()
            self._app_site = replacement
            self.app_is_running = True
            self.log.info("Fate status server restarted")

    async def watch_status_server_controls(self) -> None:
        """Consume authenticated restart requests written locally by FateControl."""
        while not self.is_closed():
            await asyncio.sleep(0.25)
            request_path = self.status_server_restart_path
            try:
                requested = request_path.is_file() and not request_path.is_symlink()
            except OSError:
                continue
            if not requested:
                continue
            try:
                await self.restart_status_server()
                await asyncio.to_thread(request_path.unlink, missing_ok=True)
            except Exception as error:
                self.log.critical(f"Couldn't restart Fate status server: {error}")
                await asyncio.sleep(1)

    async def commands_used_this_month(self, now: datetime) -> int:
        """Return calendar-month command usage with a short status cache."""
        month_key = (now.year, now.month)
        if (
            self._status_command_usage_month == month_key
            and monotonic() - self._status_command_usage_at < 60
        ):
            return self._status_command_usage
        if not self.pool:
            return 0
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        try:
            async with self.utils.cursor() as cursor:
                await cursor.execute(
                    "select coalesce(sum(total), 0) from commands where ran_at >= %s;",
                    (int(month_start.timestamp()),),
                )
                row = await cursor.fetchone()
            usage = max(0, int(row[0] if row else 0))
        except Exception:
            # Status must remain available while the database is reconnecting.
            return (
                self._status_command_usage
                if self._status_command_usage_month == month_key
                else 0
            )
        self._status_command_usage = usage
        self._status_command_usage_at = monotonic()
        self._status_command_usage_month = month_key
        return usage

    @staticmethod
    def process_memory_status() -> dict[str, Any]:
        """Return a lightweight memory view even without FateControl."""
        try:
            process = psutil.Process(os.getpid())
            processes = [process, *process.children(recursive=True)]
        except (psutil.Error, OSError):
            processes = []
        breakdown = []
        for index, item in enumerate(processes):
            try:
                size = max(0, item.memory_info().rss)
                name = "Fate runtime" if index == 0 else Path(item.name()).stem
            except (psutil.Error, OSError):
                continue
            if size:
                breakdown.append({"label": name or "Child process", "bytes": size})
        breakdown.sort(key=lambda item: item["bytes"], reverse=True)
        total = sum(item["bytes"] for item in breakdown)
        try:
            host = psutil.virtual_memory()
            host_total = host.total
            host_used = max(0, host.total - host.available)
            host_available = host.available
            host_percent = round(host.percent, 1)
        except (psutil.Error, OSError):
            host_total = host_used = host_available = 0
            host_percent = 0
        return {
            "total_bytes": host_total,
            "used_bytes": host_used,
            "available_bytes": host_available,
            "used_percent": host_percent,
            "bot_total_bytes": total,
            "bot_breakdown": breakdown,
        }

    async def status(self, _request) -> web.Response:
        """Public, read-only status used by lightweight monitoring clients."""
        now = datetime.now(tz=timezone.utc)
        started_at = getattr(self, "start_time", now)
        uptime = max(0, round((now - started_at).total_seconds()))
        latency_ms = latency_milliseconds(self.latency)
        users = sum(guild.member_count or 0 for guild in self.guilds)
        commands_this_month = await self.commands_used_this_month(now)
        if self.is_closed():
            state = "offline"
        elif self.is_ready():
            state = "online"
        else:
            state = "starting"
        configured_extensions = self.configured_extensions()
        loaded_extensions = tuple(self.extensions)
        database_state = "connected" if self.pool else "connecting"
        health_summary = "Healthy"
        if state != "online":
            health_summary = "Discord connection is starting"
        elif database_state != "connected":
            health_summary = "Database connection is starting"
        elif len(loaded_extensions) < len(configured_extensions):
            health_summary = "Some extensions did not load"
        memory_status = await asyncio.to_thread(self.process_memory_status)
        return web.json_response(
            {
                "service": "fate-bot",
                "api_version": 1,
                "instance": {
                    "role": self.instance_role,
                    "debug_mode": self.debug_mode,
                    "config": self.config_path.name,
                },
                "online": self.is_ready() and not self.is_closed(),
                "status": state,
                "servers": len(self.guilds),
                "users": users,
                "shards": self.shard_count or 1,
                "latency_ms": latency_ms,
                "uptime_seconds": uptime,
                "commands_this_month": commands_this_month,
                "capabilities": {"status_server_restart": True},
                "resources": {"memory": memory_status},
                "health": {
                    "summary": health_summary,
                    "discord": state,
                    "database": database_state,
                    "extensions_loaded": len(loaded_extensions),
                    "extensions_configured": len(configured_extensions),
                    "commands": len(self.commands),
                    "organism": (
                        "standby" if self.organism is not None else "disabled"
                    ),
                },
                "updated_at": now.isoformat(),
            },
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
        )

    async def create_log(self, message: str = "", **kwargs) -> bool:
        """Send an internal Fate event without depending on a developer cog."""
        destination = kwargs.pop("channel", "log_channel")
        embeds = kwargs.pop("embeds", [])
        embedded = kwargs.pop("embedded", True)
        color = kwargs.pop("color", colors.fate)

        if isinstance(color, str):
            color = getattr(colors, color, colors.fate)
        message = (
            message.replace("!on", emojis.on)
            .replace("!off", emojis.off)
            .replace("!pin", emojis.pin)
        )

        channel_id = (
            self.config.get(destination)
            if isinstance(destination, str)
            else destination
        )
        channel = self.get_channel(channel_id) if channel_id else None
        if channel is None and channel_id and self.is_ready():
            with suppress(NotFound, HTTPException, Forbidden):
                channel = await self.fetch_channel(channel_id)
        if channel is None:
            if self.is_ready():
                self.log.warning(
                    f"Internal log destination is unavailable: {destination}"
                )
            else:
                self.log.debug(
                    f"Internal log destination unavailable while starting: {destination}"
                )
            return False

        try:
            if embedded:
                await channel.send(
                    embed=discord.Embed(color=color, description=message)
                )
            else:
                await channel.send(message, embeds=embeds)
        except (Forbidden, HTTPException) as error:
            self.log.warning(f"Couldn't send internal log to {destination}: {error}")
            return False
        return True

    def get_fp_for(self, path) -> str:
        """Return the path for the set storage location"""
        return os.path.join(self.config["datastore_location"], path)

    async def get_resource(self, url: str, method: str = "get", *args, **kwargs):
        """Download a resource using a reusable HTTP session."""
        label = kwargs.pop("label", url)
        try:
            if self._resource_session is None or self._resource_session.closed:
                self._resource_session = aiohttp.ClientSession()
            operation = getattr(self._resource_session, method.lower(), None)
            if operation is None:
                raise ValueError(f"Unsupported HTTP method: {method}")
            async with operation(url, *args, **kwargs) as response:
                if response.content_length and response.content_length > 8_000_000:
                    raise commands.BadArgument(f"{label} is too large")
                if response.status != 200:
                    raise commands.BadArgument(f"Failed to fetch {label}")
                return await response.read()
        except asyncio.TimeoutError:
            raise commands.BadArgument(f"Timed out fetching {label}")
        except aiohttp.ClientPayloadError:
            raise commands.BadArgument(f"Failed to fetch {label}")

    def get_message(self, message_id: int) -> Optional[discord.Message]:
        """Return a message from the internal cache if it exists"""
        for message in self.cached_messages:
            if message.id == message_id:
                return message
        return None

    def _mongo_config(self):
        conf = self.auth["MongoDB"]
        if "username" in conf and "password" in conf:
            username = quote_plus(conf["username"])
            password = quote_plus(conf["password"])
            url = conf["url"].replace("mongodb://", f"mongodb://{username}:{password}@")
        else:
            url = conf["url"]
        return conf, url

    @property
    def mongo(self) -> pymongo.database.Database:
        """Return the cached synchronous Mongo database."""
        conf, url = self._mongo_config()
        if self._mongo_client is None:
            self._mongo_client = pymongo.MongoClient(
                url,
                **merge_mongo_listeners(
                    conf.get("connection_args", {}), self._mongo_telemetry_listener
                ),
            )
        return self._mongo_client[conf["db"]]

    @property
    def aio_mongo(self):
        """Return the cached asynchronous Mongo database."""
        conf, url = self._mongo_config()
        if self._aio_mongo_client is None:
            self._aio_mongo_client = AsyncIOMotorClient(
                url,
                **merge_mongo_listeners(
                    conf.get("connection_args", {}), self._mongo_telemetry_listener
                ),
            )
        return self._aio_mongo_client.get_database(conf["db"])

    async def create_pool(self) -> None:
        """Initialize the bot's MySQL pool."""
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None
            self._pool_ready.clear()
            self.log.critical("Closed the existing pool to start a new one")
        sql = mysql_settings(self.auth, self.config)
        for attempt in range(5):
            try:
                self.log("Connecting to db")
                pool = await aiomysql.create_pool(
                    host=sql["host"],
                    port=sql["port"],
                    user=sql["user"],
                    password=sql["password"],
                    db=sql["db"],
                    autocommit=sql.get("autocommit", True),
                    loop=self.loop,
                    minsize=sql.get("min_pool_size", 1),
                    maxsize=sql.get("max_pool_size", 16),
                )
                self.pool = instrument_mysql_pool(pool, self.telemetry)
                self._pool_ready.set()
                break
            except (ConnectionRefusedError, pymysql.err.OperationalError):
                self.log.critical(
                    "Couldn't connect to MySQL server, retrying in 25 seconds.."
                )
                self.log.critical(traceback.format_exc())
                if attempt < 4:
                    await asyncio.sleep(25)
        else:
            self.log.critical(
                f"Couldn't connect to MySQL server, reached max attempts``````{traceback.format_exc()}"
            )
            await self.unload_extensions(*self.configured_extensions(), log=False)
            self.log.critical("Logging out..")
            await self.close()
            return
        self.log.info(f"Initialized db {sql['db']} with {sql['user']}@{sql['host']}")

    async def wait_for_pool(self):
        """Wait for and return the initialized MySQL pool."""
        if not self.pool:
            await self._pool_ready.wait()
        return self.pool

    async def execute(self, sql: str, args: Optional[tuple] = None) -> None:
        """Executes a given query and returns nothing"""
        await self.wait_for_pool()
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                with suppress(RuntimeError):
                    await cur.execute(sql, args)
        return None

    async def fetch(self, sql: str) -> tuple:
        """Fetches the results of a given query"""
        await self.wait_for_pool()
        async with self.utils.cursor() as cur:
            await cur.execute(sql)
            r = await cur.fetchall()
        return r

    async def rowcount(self, sql: str, args: Optional[tuple] = None) -> int:
        """Execute a query and return its affected/result row count."""
        await self.wait_for_pool()
        async with self.utils.cursor() as cur:
            await cur.execute(sql, args)
            return cur.rowcount

    def load_collection(self, collection: pymongo.collection) -> dict:
        """Return Mongo documents indexed by ID, without duplicate ID fields."""
        return {
            config["_id"]: {k: v for k, v in config.items() if k != "_id"}
            for config in collection.find({})
        }

    def get_asset(self, asset: str) -> str:
        """Return the best matching local asset path."""
        asset = asset.lstrip("/")
        if "." not in asset or not os.path.exists(f"./assets/{asset}"):
            directory = "./assets/"
            paths = asset.split("/")
            filename = paths[-1]
            if "/" in asset:
                directory += paths[0]
            for file in os.listdir(directory):
                if filename in file:
                    asset = asset.replace(filename, file)
                    break
            else:
                for root, dirs, files in os.walk(dir):
                    for file in files:
                        if filename in file:
                            asset = os.path.join(root.lstrip("./assets/"), file)
                            break
        return f"./assets/{asset}"

    async def load_extension(self, name: str, *, package: str = None) -> None:
        """Load one extension with legacy Mongo caches hydrated off-loop."""
        if self._resource_cache_snapshots is not None:
            return await super().load_extension(name, package=package)
        async with self._resource_cache_hydration_lock:
            self._resource_cache_snapshots = await asyncio.to_thread(
                load_builtin_cache_snapshots, self
            )
            try:
                return await super().load_extension(name, package=package)
            finally:
                self._resource_cache_snapshots = None

    def legacy_cache_collections_for_extension(self, name: str) -> frozenset[str]:
        """Return legacy Mongo caches owned by the currently loaded extension."""
        collections = set()
        module_prefix = f"{name}."
        for cog in self.cogs.values():
            module_name = type(cog).__module__
            if module_name != name and not module_name.startswith(module_prefix):
                continue
            collections.update(
                value.collection
                for value in vars(cog).values()
                if isinstance(value, LegacyResourceCache)
            )
        return frozenset(collections)

    async def reload_extension(self, name: str, *, package: str = None) -> None:
        """Reload one extension with only its own Mongo caches preloaded."""
        if self._resource_cache_snapshots is not None:
            return await super().reload_extension(name, package=package)
        collections = self.legacy_cache_collections_for_extension(name)
        if not collections:
            return await super().reload_extension(name, package=package)
        async with self._resource_cache_hydration_lock:
            self._resource_cache_snapshots = await asyncio.to_thread(
                load_builtin_cache_snapshots, self, collections
            )
            try:
                return await super().reload_extension(name, package=package)
            finally:
                self._resource_cache_snapshots = None

    async def load_extensions(self, *extensions) -> None:
        for cog in extensions:
            before = monotonic()
            try:
                await self.load_extension(f"cogs.{cog}")
                self.log.info(f"Loaded {cog}", end="\r")
            except commands.ExtensionNotFound:
                self.log.critical(f"Couldn't find {cog}")
                self.log.info("Continuing..")
            except commands.ExtensionError:
                self.log.critical(f"Couldn't load {cog}``````{traceback.format_exc()}")
                self.log.info("Continuing..")
            except Exception:
                self.log.critical(f"Couldn't load {cog}``````{traceback.format_exc()}")
                self.log.info("Continuing..")
            ping = round((monotonic() - before) * 1000)
            self.log(f"Loaded {cog.split('.')[1]} ({ping}ms)")
        self.paginate()

    async def unload_extensions(self, *extensions, log=True) -> None:
        for cog in extensions:
            try:
                await self.unload_extension(f"cogs.{cog}")
                if log:
                    self.log.info(f"Unloaded {cog}")
            except commands.ExtensionNotLoaded:
                if log:
                    self.log.info(f"Failed to unload {cog}")


    async def close(self) -> None:
        """Close shared resources before Discord shuts down."""
        telemetry_task = self._telemetry_task
        if telemetry_task:
            telemetry_task.cancel()
            with suppress(asyncio.CancelledError):
                await telemetry_task
        self._telemetry_task = None
        uptime_task = self._uptime_task
        if uptime_task:
            uptime_task.cancel()
            with suppress(asyncio.CancelledError):
                await uptime_task
        self._uptime_task = None
        try:
            await asyncio.to_thread(
                self.uptime_tracker.stop,
                datetime.now(tz=timezone.utc),
            )
        except OSError as error:
            self.log.critical(f"Couldn't finalize uptime history: {error}")
        watcher = self._instance_watch_task
        current = asyncio.current_task()
        if watcher and watcher is not current:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
        self._instance_watch_task = None
        if self.translator:
            await self.translator.close()
            self.translator = None
        if self._resource_session and not self._resource_session.closed:
            await self._resource_session.close()
        status_control_task = self._status_server_control_task
        if status_control_task:
            status_control_task.cancel()
            with suppress(asyncio.CancelledError):
                await status_control_task
        self._status_server_control_task = None
        if self._app_runner:
            await self._app_runner.cleanup()
            self._app_runner = None
            self._app_site = None
            self.app_is_running = False
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None
            self._pool_ready.clear()
        if self._aio_mongo_client:
            self._aio_mongo_client.close()
            self._aio_mongo_client = None
        if self._mongo_client:
            self._mongo_client.close()
            self._mongo_client = None
        await super().close()
        cache = getattr(self, "cache", None)
        if cache:
            await cache.close()
        local_log_archive = getattr(self, "_local_log_archive", None)
        if local_log_archive:
            await local_log_archive.close()
            self._local_log_archive = None
        await asyncio.to_thread(self.telemetry.close)

    def paginate(self):
        """Map out each modules enable command for use of `.enable module`"""
        remap = not not self.toggles
        self.toggles.clear()
        for module, cls in self.cogs.items():
            if hasattr(cls, "enable_command") and hasattr(cls, "disable_command"):
                self.toggles[module] = [
                    getattr(cls, "enable_command"),
                    getattr(cls, "disable_command"),
                ]
        self.log(f"{'Rem' if remap else 'M'}apped modules")

    async def load(self, data: str) -> Union[list, dict]:
        """Load JSON outside the event loop."""
        return await asyncio.to_thread(json.loads, data)

    async def dump(self, data: Union[list, dict]) -> str:
        """Serialize JSON outside the event loop."""
        return await asyncio.to_thread(json.dumps, data)

    def encode(self, string: str) -> str:
        """Returns a string encoded in Base64"""
        return b64encode(string.encode()).decode()

    def decode(self, string: str) -> str:
        """Returns a decoded string from Base64"""
        return b64decode(string.encode()).decode()

    def run(self):
        explicit_auth_path = os.environ.get("FATE_AUTH_PATH")
        test_auth_path = Path("./data/auth.test.json")
        if self.using_test_profile and not test_auth_path.exists():
            raise RuntimeError(
                "Test auth configuration is missing; run "
                "apps/DevServices/start-databases.ps1 first"
            )
        auth_path = (
            str(test_auth_path)
            if self.using_test_profile and not explicit_auth_path
            else explicit_auth_path
            or os.path.join(self.config["datastore_location"], "auth.json")
        )
        self.auth = load_auth_config(auth_path)

        mysql_started, mongo_started = ensure_local_databases(
            self.auth, self.config, root=Path(__file__).resolve().parent
        )
        if mysql_started or mongo_started:
            started = ", ".join(
                name
                for name, did_start in (
                    ("MySQL", mysql_started),
                    ("MongoDB", mongo_started),
                )
                if did_start
            )
            cprint(f"Started local database services: {started}", "yellow")

        use_sentry(self, dsn=self.auth["sentry_dsn"])

        self.guild_prefixes = self.load_collection(self.mongo["GuildPrefixes"])
        self.user_prefixes = self.load_collection(self.mongo["UserPrefixes"])

        token = load_bot_token(
            self.auth,
            self.config["token_id"],
            str(self.external_token_path) if self.external_token_path else None,
        )
        self.runtime_token = token
        super().run(token)


# Configure the Discord log only for the actual bot process. Importing Fate for
# tests or tooling must not truncate a log owned by a running bot.
start_time = time()
logger = logging.getLogger("discord")
logger.setLevel(logging.INFO)
if __name__ == "__main__":
    ensure_logging_directory()
    handler = logging.FileHandler(
        filename=DISCORD_LOG_PATH,
        encoding="utf-8",
        mode="w",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s:%(levelname)s:%(name)s: %(message)s")
    )
    logger.addHandler(handler)

# Initialize the bot
bot = Fate(case_insensitive=True)
bot.allowed_mentions = discord.AllowedMentions(everyone=False, roles=False, users=False)
bot.remove_command("help")  # Default help command
bot.add_check(checks.command_is_enabled)
bot.add_check(checks.blocked)
bot.add_check(checks.restricted)


@bot.event
async def on_shard_connect(shard_id):
    if not bot.pool and shard_id == 0:
        await bot.create_pool()
    if shard_id == 0:
        bot.log.info(
            f"------------\nLogging in as\n{bot.user}\n{bot.user.id}\n------------",
            color="green",
        )
    bot.log.info(f"Shard {shard_id} connected")


@bot.event
async def on_connect():
    if not bot._tree_synced:
        await bot.sync_application_commands()
    cprint("Initializing cache", "yellow", end="\r")
    index = 0
    chars = r"-/-\-"
    while not bot.is_ready():
        cprint(f"Initializing cache {chars[index]}", "yellow", end="\r")
        index += 1
        if index + 1 == len(chars):
            index = 0
        await asyncio.sleep(0.21)


@bot.event
async def on_ready():
    bot.log.info("Finished initializing cache", color="yellow")
    # Reconnect the nodes if the bot is reconnecting
    if not bot.pool:
        await bot.create_pool()
    if cog := bot.get_cog("Tasks"):
        cog.ensure_all()  # type: ignore
    seconds = round(time() - start_time)
    if not bot.app_is_running:
        bot._app_runner = web.AppRunner(bot.app)
        await bot._app_runner.setup()
        bot._app_site = web.TCPSite(
            bot._app_runner,
            host=bot.dashboard_host,
            port=bot.dashboard_port,
        )
        await bot._app_site.start()
        bot.app_is_running = True
        bot.log.info("Ping application started")
    if bot._status_server_control_task is None or bot._status_server_control_task.done():
        bot._status_server_control_task = asyncio.create_task(
            bot.watch_status_server_controls(),
            name="fate-status-server-controls",
        )
    bot.log.info(f"Startup took {seconds} seconds")
    for error in bot.login_errors:
        bot.log.critical(f"Error ignored during startup:\n{error}")


get_prefix_cd = Cooldown(1, 10)


@bot.event
async def on_message(msg):
    # Check if the channels been rate limited
    if (
        msg.guild and msg.guild.id in bot.ignored_locations
    ) or msg.author.id in bot.ignored_locations:
        return

    # Send the prefix if the bot's mentioned
    if (
        not msg.author.bot
        and bot.user.mentioned_in(msg)
        and len(msg.content.split()) == 1
    ):
        if str(bot.user.id) in msg.content:
            rate_limited = get_prefix_cd.check(msg.author.id)
            if not rate_limited:
                r = await get_prefixes_async(bot, msg)
                prefixes = "\n".join(r[1:])
                if len(prefixes.split("\n")) > 2:
                    return
                with suppress(NotFound, Forbidden, HTTPException, AttributeError):
                    await msg.channel.send(f"The prefixes you can use are:\n{prefixes}")
                return

    if (
        msg.guild
        and msg.guild.me
        and not msg.channel.permissions_for(msg.guild.me).send_messages
    ):
        return

    # Parse prefix, run checks, and execute
    await bot.process_commands(msg)


if __name__ == "__main__":
    print("Starting Bot")
    bot.start_time = datetime.now(tz=timezone.utc)
    handoff_completed = False
    try:
        handoff_completed = bot.instance_handoff.claim()
        if handoff_completed:
            print("Previous Fate instance logged out; continuing startup")
        bot.run()
    except discord.LoginFailure:
        print("Invalid Token")
    except asyncio.exceptions.CancelledError:
        pass
    except RuntimeError as error:
        print(f"Startup failed: {error}", file=sys.stderr)
        raise
    except KeyboardInterrupt:
        pass
    finally:
        bot.instance_handoff.release()
