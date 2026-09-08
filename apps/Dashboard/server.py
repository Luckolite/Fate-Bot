"""Fate's public website and authenticated Discord server dashboard."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
from collections import defaultdict, deque
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import urlencode, urlparse

import aiohttp
from aiohttp import web

try:
    from .dashboard.activity_log_store import (
        BotActivityLogStore,
        UnavailableActivityLogStore,
    )
    from .dashboard.command_catalog import build_catalog
    from .dashboard.ranking_store import (
        BOARD_LABELS,
        BotRankingStore,
        MemoryRankingStore,
        MySQLRankingStore,
    )
    from .dashboard.module_store import (
        BotModuleStore,
        MemoryModuleStore,
        UnavailableModuleStore,
    )
    from .dashboard.module_validation import validate_module_settings
    from .dashboard.memory_telemetry import collect_memory_snapshot
    from .dashboard.owner_settings_store import (
        RESTART_REQUIRED_FIELDS,
        BotOwnerSettingsStore,
        ConfigConflictError,
        FileOwnerSettingsStore,
        MemoryOwnerSettingsStore,
        OwnerAccessRevokedError,
        resolve_config_path,
    )
    from .dashboard.settings_store import (
        BotSettingsStore,
        MemorySettingsStore,
        MongoSettingsStore,
    )
    from .dashboard.validation import ValidationError, can_manage_guild, validate_settings
except ImportError:  # Support `python apps/Dashboard/server.py` for local development.
    from dashboard.activity_log_store import BotActivityLogStore, UnavailableActivityLogStore
    from dashboard.command_catalog import build_catalog
    from dashboard.ranking_store import (
        BOARD_LABELS,
        BotRankingStore,
        MemoryRankingStore,
        MySQLRankingStore,
    )
    from dashboard.module_store import BotModuleStore, MemoryModuleStore, UnavailableModuleStore
    from dashboard.module_validation import validate_module_settings
    from dashboard.memory_telemetry import collect_memory_snapshot
    from dashboard.owner_settings_store import (
        RESTART_REQUIRED_FIELDS,
        BotOwnerSettingsStore,
        ConfigConflictError,
        FileOwnerSettingsStore,
        MemoryOwnerSettingsStore,
        OwnerAccessRevokedError,
        resolve_config_path,
    )
    from dashboard.settings_store import BotSettingsStore, MemorySettingsStore, MongoSettingsStore
    from dashboard.validation import ValidationError, can_manage_guild, validate_settings


DASHBOARD_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = DASHBOARD_ROOT.parents[1]
STATIC_ROOT = DASHBOARD_ROOT / "static"
DISCORD_API = "https://discord.com/api/v10"
COOKIE_NAME = "fate_dashboard_session"
DASHBOARD_PATH = "/dashboard"
LEGACY_DASHBOARD_PATH = "/fate"
SESSION_SECONDS = 60 * 60 * 24 * 7
MAX_SESSIONS_PER_USER = 5
MAX_SESSIONS = 10_000
OWNER_PREAUTH_RATE_LIMIT = 60
SESSION_READ_RATE_LIMIT = 120
DEFAULT_TRUSTED_PROXIES = "127.0.0.1/32,::1/128"
CONFIG_REVISION_PATTERN = re.compile(r"[0-9a-f]{64}")
DISCORD_SNOWFLAKE_PATTERN = re.compile(r"[0-9]{1,20}")
LOCAL_LOG_CURSOR_PATTERN = re.compile(
    r"local:(?:(?:exact|prefix|fuzzy):)?\d{1,20}"
)
DISCORD_SNOWFLAKE_MAX = (1 << 64) - 1
DEV_USER_ID = 457210410819649536
LOGGER = logging.getLogger("fate.dashboard")


def load_local_env() -> None:
    """Load a small .env file without adding another runtime dependency."""
    path = DASHBOARD_ROOT / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_secret_file(path: Path) -> str:
    """Read a single secret without ever logging its contents."""
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def client_id_from_token(token: str) -> str:
    """Extract the Discord application ID encoded in a bot token."""
    if not token or "." not in token:
        return ""
    encoded_id = token.split(".", 1)[0]
    try:
        padding = "=" * (-len(encoded_id) % 4)
        client_id = base64.urlsafe_b64decode(encoded_id + padding).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ""
    return client_id if client_id.isdigit() else ""


def trusted_proxy_networks(value: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse the explicit proxy allowlist used for forwarded client addresses."""
    networks = []
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as error:
            raise RuntimeError(f"Invalid DASHBOARD_TRUSTED_PROXIES entry: {entry}") from error
    return tuple(networks)


def avatar_url(user: dict[str, Any]) -> str:
    user_id = str(user.get("id", "0"))
    avatar = user.get("avatar")
    if avatar:
        extension = "gif" if str(avatar).startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.{extension}?size=256"
    try:
        index = (int(user_id) >> 22) % 6
    except ValueError:
        index = 0
    return f"https://cdn.discordapp.com/embed/avatars/{index}.png"


def guild_icon_url(guild: dict[str, Any]) -> str | None:
    icon = guild.get("icon")
    return f"https://cdn.discordapp.com/icons/{guild['id']}/{icon}.png?size=128" if icon else None


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    display_name = user.get("global_name") or user.get("username") or "Unknown pilot"
    return {
        "id": str(user.get("id", "")),
        "username": user.get("username", "Unknown"),
        "display_name": display_name,
        "avatar": avatar_url(user),
    }


def public_guild(guild: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(guild["id"]),
        "name": guild.get("name", "Unknown server"),
        "icon": guild_icon_url(guild),
        "owner": bool(guild.get("owner")),
    }


@web.middleware
async def security_headers(request: web.Request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as error:
        response = error
    except ValidationError as error:
        response = web.json_response({"error": str(error)}, status=400)
    except (ConnectionError, OSError, RuntimeError, TimeoutError) as error:
        LOGGER.warning("Dashboard dependency unavailable: %s", error)
        response = web.json_response(
            {"error": "A Fate service is temporarily unavailable. Please try again soon."}, status=503
        )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' https: data:; "
        "style-src 'self'; script-src 'self'; connect-src 'self'; "
        "font-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self' https://discord.com"
    )
    dashboard_document = request.path in {
        DASHBOARD_PATH,
        LEGACY_DASHBOARD_PATH,
    }
    if dashboard_document or request.path.startswith(("/api/", "/auth/")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


class Dashboard:
    def __init__(self, bot=None):
        load_local_env()
        self.telemetry = getattr(bot, "telemetry", None)
        bot_config = getattr(bot, "config", {})
        website_config = (
            bot_config.get("website", {}) if isinstance(bot_config, dict) else {}
        )
        if not isinstance(website_config, dict):
            website_config = {}

        def configured(name: str, key: str, default: Any) -> Any:
            environment_value = os.getenv(name)
            return environment_value if environment_value is not None else website_config.get(key, default)

        raw_dev_mode = configured("DASHBOARD_DEV_MODE", "dev_mode", False)
        self.dev_mode = (
            raw_dev_mode.strip() == "1"
            if isinstance(raw_dev_mode, str)
            else raw_dev_mode is True
        )
        if bot is not None and self.dev_mode:
            raise RuntimeError(
                "DASHBOARD_DEV_MODE cannot be enabled when the dashboard is mounted in Fate."
            )
        self.bot = bot
        configured_bot_token = ""
        if bot:
            configured_bot_token = getattr(bot, "runtime_token", "")
            if not configured_bot_token and getattr(bot, "auth", None):
                configured_bot_token = bot.auth.get("tokens", {}).get(
                    bot.config["token_id"], ""
                )
        self.bot_token = os.getenv("DISCORD_BOT_TOKEN", configured_bot_token)
        inferred_client_id = client_id_from_token(self.bot_token)
        self.client_id = str(
            configured(
                "DISCORD_CLIENT_ID",
                "discord_client_id",
                inferred_client_id or "506735111543193601",
            )
        )
        secret_path = Path(
            os.path.expandvars(
                os.path.expanduser(
                    str(configured(
                        "DISCORD_CLIENT_SECRET_PATH",
                        "discord_client_secret_path",
                        str(Path.home() / "Desktop" / "client_secret.txt"),
                    ))
                )
            )
        )
        self.client_secret = str(
            configured("DISCORD_CLIENT_SECRET", "discord_client_secret", "") or ""
        ) or read_secret_file(secret_path)
        default_redirect = (
            "http://localhost:16420/auth/callback"
            if bot is not None
            else "http://localhost:8080/auth/callback"
        )
        self.redirect_uri = str(
            configured(
                "DISCORD_REDIRECT_URI", "discord_redirect_uri", default_redirect
            )
        )
        parsed_redirect = urlparse(self.redirect_uri)
        if (
            not self.dev_mode
            and parsed_redirect.scheme != "https"
            and parsed_redirect.hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise RuntimeError("Public Discord OAuth callbacks must use HTTPS.")
        secure_cookie_setting = os.getenv("DASHBOARD_SECURE_COOKIES")
        self.cookie_secure = bool(
            parsed_redirect.scheme == "https"
            or (
                secure_cookie_setting == "1"
                if secure_cookie_setting is not None
                else website_config.get("secure_cookies", False) is True
            )
        )
        self.session_secret = str(
            configured("DASHBOARD_SESSION_SECRET", "session_secret", "") or ""
        ) or secrets.token_urlsafe(48)
        self.sessions: dict[str, dict[str, Any]] = {}
        self.oauth_states: dict[str, dict[str, Any]] = {}
        self.rate_limits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        proxy_setting = configured(
            "DASHBOARD_TRUSTED_PROXIES",
            "trusted_proxies",
            DEFAULT_TRUSTED_PROXIES,
        )
        if isinstance(proxy_setting, list):
            proxy_setting = ",".join(str(value) for value in proxy_setting)
        self.trusted_proxies = trusted_proxy_networks(str(proxy_setting))
        self.user_cache: dict[tuple[str, str | None], tuple[float, dict[str, Any]]] = {}
        self.commands = build_catalog(REPOSITORY_ROOT / "cogs")
        self.reload_revision = secrets.token_hex(12)
        self.http: aiohttp.ClientSession | None = None

        if bot is not None:
            self.settings = BotSettingsStore(bot)
            self.ranking = BotRankingStore(bot)
            self.modules = BotModuleStore(bot)
            self.activity_logs = BotActivityLogStore(bot)
            self.owner_settings = BotOwnerSettingsStore(
                bot, resolve_config_path(REPOSITORY_ROOT, bot)
            )
        elif self.dev_mode:
            self.settings = MemorySettingsStore()
            self.ranking = MemoryRankingStore()
            self.modules = MemoryModuleStore()
            self.activity_logs = UnavailableActivityLogStore(preview=True)
            self.owner_settings = MemoryOwnerSettingsStore({DEV_USER_ID})
        else:
            self.settings = MongoSettingsStore(
                os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017"),
                os.getenv("MONGODB_DATABASE", "fate"),
            )
            self.ranking = MySQLRankingStore(
                host=os.getenv("MYSQL_HOST", "127.0.0.1"),
                port=int(os.getenv("MYSQL_PORT", "3306")),
                user=os.getenv("MYSQL_USER", ""),
                password=os.getenv("MYSQL_PASSWORD", ""),
                db=os.getenv("MYSQL_DATABASE", "fate"),
            )
            self.modules = UnavailableModuleStore()
            self.activity_logs = UnavailableActivityLogStore()
            self.owner_settings = FileOwnerSettingsStore(
                resolve_config_path(REPOSITORY_ROOT)
            )

    async def startup(self, _app: web.Application) -> None:
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))

    async def cleanup(self, _app: web.Application) -> None:
        if self.http:
            await self.http.close()
        await self.settings.close()
        await self.ranking.close()
        await self.modules.close()
        await self.owner_settings.close()

    def sign(self, token: str) -> str:
        signature = hmac.new(
            self.session_secret.encode(), token.encode(), hashlib.sha256
        ).hexdigest()
        return f"{token}.{signature}"

    def session(self, request: web.Request) -> dict[str, Any] | None:
        signed = request.cookies.get(COOKIE_NAME, "")
        token, separator, signature = signed.partition(".")
        if not separator or not hmac.compare_digest(self.sign(token), signed):
            return None
        session = self.sessions.get(token)
        if not session or session["expires_at"] < time():
            self.sessions.pop(token, None)
            return None
        return session

    def require_session(self, request: web.Request) -> dict[str, Any]:
        session = self.session(request)
        if not session:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "Sign in with Discord to continue."}),
                content_type="application/json",
            )
        return session

    @staticmethod
    def session_user_id(session: dict[str, Any]) -> int | None:
        raw_user_id = session.get("user", {}).get("id")
        if isinstance(raw_user_id, str) and DISCORD_SNOWFLAKE_PATTERN.fullmatch(raw_user_id):
            user_id = int(raw_user_id)
            return user_id if 0 < user_id <= DISCORD_SNOWFLAKE_MAX else None
        if type(raw_user_id) is int and 0 < raw_user_id <= DISCORD_SNOWFLAKE_MAX:
            return raw_user_id
        return None

    async def is_bot_owner(self, session: dict[str, Any]) -> bool:
        user_id = self.session_user_id(session)
        return bool(user_id and await self.owner_settings.is_owner(user_id))

    async def require_owner(
        self,
        request: web.Request,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = session if session is not None else self.require_session(request)
        if not await self.is_bot_owner(session):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Only a Fate bot owner can manage these settings."}),
                content_type="application/json",
            )
        return session

    async def require_guild(
        self, request: web.Request
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        session = self.require_session(request)
        guild_id = request.match_info["guild_id"]
        guilds = await self.authorized_guilds(session)
        guild = next(
            (guild for guild in guilds if str(guild.get("id")) == guild_id),
            None,
        )
        if not guild or not can_manage_guild(guild):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "You need Manage Server permission for this server."}),
                content_type="application/json",
            )
        return session, guild

    def require_csrf(self, request: web.Request, session: dict[str, Any]) -> None:
        supplied = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(supplied, session["csrf"]):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "The page session expired. Refresh and try again."}),
                content_type="application/json",
            )

    def enforce_rate_limit(
        self,
        request: web.Request,
        bucket: str,
        *,
        limit: int,
        window: int,
        subject: str | None = None,
    ) -> None:
        now = time()
        key = (bucket, subject or self.client_ip(request))
        attempts = self.rate_limits[key]
        while attempts and attempts[0] <= now - window:
            attempts.popleft()
        if len(attempts) >= limit:
            retry_after = max(1, round(window - (now - attempts[0])))
            raise web.HTTPTooManyRequests(
                text=json.dumps({"error": "Too many requests. Please wait and try again."}),
                content_type="application/json",
                headers={"Retry-After": str(retry_after)},
            )
        attempts.append(now)

    def client_ip(self, request: web.Request) -> str:
        """Return a canonical client address without trusting unapproved proxies."""
        raw_peer = request.remote or ""
        try:
            peer = ipaddress.ip_address(raw_peer.split("%", 1)[0])
        except ValueError:
            return "unknown"

        if not any(peer in network for network in self.trusted_proxies):
            return str(peer)

        forwarded_for = getattr(request, "headers", {}).get("X-Forwarded-For", "")
        if not forwarded_for:
            return str(peer)
        for entry in reversed(forwarded_for.split(",")):
            try:
                candidate = ipaddress.ip_address(entry.strip().split("%", 1)[0])
            except ValueError:
                return str(peer)
            if any(candidate in network for network in self.trusted_proxies):
                continue
            return str(candidate)
        return str(peer)

    def evict_user_sessions_for_new_login(self, user_id: int) -> None:
        """Keep one Discord account from exhausting the global session pool."""
        matching = []
        for token, session in self.sessions.items():
            if self.session_user_id(session) != user_id:
                continue
            created_at = session.get("created_at")
            if not isinstance(created_at, (int, float)):
                created_at = session.get("expires_at", 0) - SESSION_SECONDS
            matching.append((created_at, token))
        matching.sort()
        for _created_at, token in matching[: max(0, len(matching) - MAX_SESSIONS_PER_USER + 1)]:
            self.sessions.pop(token, None)

    def record_dashboard_signin(self) -> None:
        """Count a completed OAuth sign-in without retaining user identity."""
        if self.telemetry is None:
            return
        try:
            self.telemetry.increment("dashboard_signins")
        except Exception as error:
            LOGGER.warning("Could not record dashboard sign-in telemetry: %s", error)

    def prune_ephemeral_state(self) -> None:
        now = time()
        for state, pending in list(self.oauth_states.items()):
            if pending["expires_at"] < now:
                self.oauth_states.pop(state, None)
        for token, session in list(self.sessions.items()):
            if session["expires_at"] < now:
                self.sessions.pop(token, None)
        for key, attempts in list(self.rate_limits.items()):
            while attempts and attempts[0] <= now - 600:
                attempts.popleft()
            if not attempts:
                self.rate_limits.pop(key, None)

    async def refresh_oauth_guilds(self, session: dict[str, Any]) -> None:
        if self.dev_mode or self.bot is not None:
            return
        if session.get("guilds_refreshed_at", 0) > time() - 60:
            return
        access_token = session.get("access_token")
        if not access_token or not self.http:
            session["guilds"] = []
            return
        async with self.http.get(
            f"{DISCORD_API}/users/@me/guilds",
            headers={"Authorization": f"Bearer {access_token}"},
        ) as response:
            if response.status == 401:
                session["guilds"] = []
                session.pop("access_token", None)
                raise web.HTTPUnauthorized(
                    text=json.dumps({"error": "Your Discord session expired. Sign in again."}),
                    content_type="application/json",
                )
            if response.status != 200:
                raise ConnectionError(f"Discord guild refresh returned {response.status}")
            guilds = await response.json()
        session["guilds"] = [guild for guild in guilds if can_manage_guild(guild)]
        session["guilds_refreshed_at"] = time()

    async def bot_member_can_manage(self, guild_id: int, user_id: int) -> bool:
        if self.bot is None:
            return False
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                return False
        permissions = member.guild_permissions
        return bool(
            member.id == guild.owner_id
            or permissions.administrator
            or permissions.manage_guild
        )

    async def authorized_guilds(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        await self.refresh_oauth_guilds(session)
        guilds = [guild for guild in session["guilds"] if can_manage_guild(guild)]
        if self.bot is None or self.dev_mode:
            return guilds
        user_id = int(session["user"]["id"])
        checks = await asyncio.gather(
            *[self.bot_member_can_manage(int(guild["id"]), user_id) for guild in guilds]
        )
        return [guild for guild, allowed in zip(guilds, checks) if allowed]

    async def index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_ROOT / "index.html")

    async def dashboard_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_ROOT / "dashboard.html")

    async def legacy_dashboard_redirect(
        self, _request: web.Request
    ) -> web.StreamResponse:
        raise web.HTTPPermanentRedirect(DASHBOARD_PATH)

    def refresh_runtime(self) -> str:
        """Refresh cached website data and tell connected pages to reload."""
        self.commands = build_catalog(REPOSITORY_ROOT / "cogs")
        self.user_cache.clear()
        self.reload_revision = secrets.token_hex(12)
        return self.reload_revision

    async def api_runtime_revision(self, _request: web.Request) -> web.Response:
        return web.json_response({"revision": self.reload_revision})

    async def api_commands(self, _request: web.Request) -> web.Response:
        return web.json_response({"commands": self.commands, "count": len(self.commands)})

    async def api_session(self, request: web.Request) -> web.Response:
        session = self.session(request)
        is_owner = False
        if session:
            user_id = self.session_user_id(session)
            self.enforce_rate_limit(
                request,
                "session-read",
                limit=SESSION_READ_RATE_LIMIT,
                window=60,
                subject=f"user:{user_id}" if user_id is not None else "invalid-session",
            )
            try:
                is_owner = await self.is_bot_owner(session)
            except RuntimeError as error:
                LOGGER.warning("Could not determine dashboard owner access: %s", error)
        return web.json_response(
            {
                "authenticated": bool(session),
                "auth_available": bool(self.client_secret),
                "dev_mode": self.dev_mode,
                "user": public_user(session["user"]) if session else None,
                "csrf": session["csrf"] if session else None,
                "is_owner": is_owner,
            }
        )

    async def auth_discord(self, request: web.Request) -> web.StreamResponse:
        self.enforce_rate_limit(request, "oauth-start", limit=10, window=60)
        self.prune_ephemeral_state()
        if not self.client_secret:
            raise web.HTTPServiceUnavailable(
                text="Discord sign-in is unavailable right now. Please try again later."
            )
        state = secrets.token_urlsafe(32)
        destination = request.query.get("next", DASHBOARD_PATH)
        if not destination.startswith("/") or destination.startswith("//"):
            destination = DASHBOARD_PATH
        if len(self.oauth_states) >= 5_000:
            raise web.HTTPServiceUnavailable(
                text="Discord sign-in is temporarily busy. Please try again shortly."
            )
        self.oauth_states[state] = {"expires_at": time() + 600, "next": destination}
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": "identify guilds",
                "state": state,
            }
        )
        response = web.HTTPFound(f"https://discord.com/oauth2/authorize?{query}")
        response.set_cookie(
            "fate_oauth_state",
            state,
            max_age=600,
            httponly=True,
            secure=self.cookie_secure,
            samesite="Lax",
            path="/auth/callback",
        )
        raise response

    async def auth_callback(self, request: web.Request) -> web.StreamResponse:
        self.enforce_rate_limit(request, "oauth-callback", limit=20, window=60)
        self.prune_ephemeral_state()
        state = request.query.get("state", "")
        pending = self.oauth_states.pop(state, None)
        if (
            not pending
            or pending["expires_at"] < time()
            or not hmac.compare_digest(request.cookies.get("fate_oauth_state", ""), state)
        ):
            raise web.HTTPBadRequest(text="The Discord sign-in request expired. Please try again.")
        code = request.query.get("code")
        if not code or not self.http:
            raise web.HTTPBadRequest(text="Discord sign-in could not be completed. Please try again.")

        async with self.http.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            },
        ) as response:
            if response.status != 200:
                LOGGER.warning("Discord token exchange failed with %s", response.status)
                raise web.HTTPBadGateway(text="Discord sign-in could not be completed. Please try again.")
            token = await response.json()

        headers = {"Authorization": f"Bearer {token['access_token']}"}
        user_response, guild_response = await asyncio.gather(
            self.http.get(f"{DISCORD_API}/users/@me", headers=headers),
            self.http.get(f"{DISCORD_API}/users/@me/guilds", headers=headers),
        )
        async with user_response, guild_response:
            if user_response.status != 200 or guild_response.status != 200:
                raise web.HTTPBadGateway(text="Fate could not load your Discord servers. Please sign in again.")
            user = await user_response.json()
            guilds = await guild_response.json()

        user_id = self.session_user_id({"user": user})
        if user_id is None:
            raise web.HTTPBadGateway(
                text="Discord returned an invalid account. Please sign in again."
            )
        self.evict_user_sessions_for_new_login(user_id)
        session_id = secrets.token_urlsafe(32)
        if len(self.sessions) >= MAX_SESSIONS:
            raise web.HTTPServiceUnavailable(
                text="Discord sign-in is temporarily busy. Please try again shortly."
            )
        try:
            oauth_lifetime = max(60, int(token.get("expires_in", SESSION_SECONDS)))
        except (TypeError, ValueError):
            oauth_lifetime = SESSION_SECONDS
        self.sessions[session_id] = {
            "user": user,
            "guilds": [guild for guild in guilds if can_manage_guild(guild)],
            "access_token": token["access_token"],
            "guilds_refreshed_at": time(),
            "csrf": secrets.token_urlsafe(24),
            "created_at": time(),
            "expires_at": time() + min(SESSION_SECONDS, oauth_lifetime),
        }
        self.record_dashboard_signin()
        response = web.HTTPFound(pending["next"])
        response.del_cookie("fate_oauth_state", path="/auth/callback")
        response.set_cookie(
            COOKIE_NAME,
            self.sign(session_id),
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=self.cookie_secure,
            samesite="Lax",
            path="/",
        )
        raise response

    async def auth_dev(self, _request: web.Request) -> web.StreamResponse:
        if not self.dev_mode:
            raise web.HTTPNotFound()
        self.prune_ephemeral_state()
        self.evict_user_sessions_for_new_login(DEV_USER_ID)
        if len(self.sessions) >= MAX_SESSIONS:
            raise web.HTTPServiceUnavailable(
                text="Discord sign-in is temporarily busy. Please try again shortly."
            )
        session_id = secrets.token_urlsafe(32)
        self.sessions[session_id] = {
            "user": {
                "id": str(DEV_USER_ID),
                "username": "StarlightPilot",
                "global_name": "Starlight Pilot",
                "avatar": None,
            },
            "guilds": [
                {
                    "id": "397415086295089155",
                    "name": "Fate Observatory",
                    "icon": None,
                    "owner": True,
                    "permissions": str((1 << 3) | (1 << 5)),
                },
                {
                    "id": "787038012877176893",
                    "name": "Andromeda Station",
                    "icon": None,
                    "owner": False,
                    "permissions": str(1 << 5),
                },
            ],
            "csrf": secrets.token_urlsafe(24),
            "created_at": time(),
            "expires_at": time() + SESSION_SECONDS,
        }
        response = web.HTTPFound(DASHBOARD_PATH)
        response.set_cookie(
            COOKIE_NAME,
            self.sign(session_id),
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=self.cookie_secure,
            samesite="Lax",
            path="/",
        )
        raise response

    async def logout(self, request: web.Request) -> web.Response:
        session = self.require_session(request)
        self.require_csrf(request, session)
        signed = request.cookies.get(COOKIE_NAME, "")
        token = signed.partition(".")[0]
        self.sessions.pop(token, None)
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE_NAME, path="/")
        return response

    async def api_guilds(self, request: web.Request) -> web.Response:
        session = self.require_session(request)
        guilds = [public_guild(guild) for guild in await self.authorized_guilds(session)]
        return web.json_response({"guilds": guilds})

    async def api_owner_settings(self, request: web.Request) -> web.Response:
        session = self.require_session(request)
        user_id = self.session_user_id(session)
        if user_id is None:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Only a Fate bot owner can manage these settings."}),
                content_type="application/json",
            )
        self.enforce_rate_limit(
            request,
            "owner-settings-preauth",
            limit=OWNER_PREAUTH_RATE_LIMIT,
            window=60,
            subject=f"user:{user_id}",
        )
        session = await self.require_owner(request, session)
        self.enforce_rate_limit(
            request,
            "owner-settings-read",
            limit=60,
            window=60,
            subject=f"user:{user_id}",
        )
        try:
            snapshot = await self.owner_settings.get_snapshot_for_owner(user_id)
        except OwnerAccessRevokedError as error:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Bot-owner access was revoked. Refresh the page."}),
                content_type="application/json",
            ) from error
        return web.json_response(
            {
                "settings": snapshot.settings,
                "revision": snapshot.revision,
                "requires_restart": list(RESTART_REQUIRED_FIELDS),
            }
        )

    def message_cache_snapshot(self) -> dict[str, Any]:
        """Read Discord's live cache counters on the bot event-loop thread."""
        if self.bot is None:
            return {"available": False}
        try:
            current = len(self.bot.cached_messages)
        except (AttributeError, TypeError):
            return {"available": False}

        capacity = getattr(getattr(self.bot, "_connection", None), "max_messages", None)
        if type(capacity) is not int or capacity < 0:
            config = getattr(self.bot, "config", {})
            capacity = config.get("max_cached_messages") if isinstance(config, dict) else None
        return {
            "available": True,
            "current": current,
            "capacity": capacity,
        }

    async def api_owner_memory(self, request: web.Request) -> web.Response:
        session = self.require_session(request)
        user_id = self.session_user_id(session)
        if user_id is None:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Only a Fate bot owner can view runtime memory."}),
                content_type="application/json",
            )
        self.enforce_rate_limit(
            request,
            "owner-memory-preauth",
            limit=OWNER_PREAUTH_RATE_LIMIT,
            window=60,
            subject=f"user:{user_id}",
        )
        await self.require_owner(request, session)
        self.enforce_rate_limit(
            request,
            "owner-memory-read",
            limit=60,
            window=60,
            subject=f"user:{user_id}",
        )
        cache_snapshot = self.message_cache_snapshot()
        try:
            snapshot = await asyncio.to_thread(
                collect_memory_snapshot, cache_snapshot
            )
        except Exception as error:
            LOGGER.exception("Could not collect owner memory telemetry")
            raise web.HTTPServiceUnavailable(
                text=json.dumps(
                    {"error": "Runtime memory telemetry is temporarily unavailable."}
                ),
                content_type="application/json",
            ) from error
        return web.json_response(snapshot)

    async def save_owner_settings(self, request: web.Request) -> web.Response:
        session = self.require_session(request)
        user_id = self.session_user_id(session)
        if user_id is None:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Only a Fate bot owner can manage these settings."}),
                content_type="application/json",
            )
        self.enforce_rate_limit(
            request,
            "owner-settings-preauth",
            limit=OWNER_PREAUTH_RATE_LIMIT,
            window=60,
            subject=f"user:{user_id}",
        )
        session = await self.require_owner(request, session)
        self.require_csrf(request, session)
        self.enforce_rate_limit(
            request,
            "owner-settings-write",
            limit=20,
            window=60,
            subject=f"user:{user_id}",
        )
        if request.content_length and request.content_length > 64_000:
            raise web.HTTPRequestEntityTooLarge(max_size=64_000, actual_size=request.content_length)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, LookupError, TypeError, UnicodeError) as error:
            raise ValidationError(
                "Those owner settings could not be saved. Refresh and try again."
            ) from error
        if not isinstance(payload, dict) or set(payload) != {"settings", "revision"}:
            raise ValidationError(
                "Owner settings must contain exactly settings and revision fields."
            )
        revision = payload["revision"]
        if not isinstance(revision, str) or not CONFIG_REVISION_PATTERN.fullmatch(revision):
            raise ValidationError(
                "The owner settings revision is invalid. Reload the latest values and try again."
            )

        try:
            previous = await self.owner_settings.get_snapshot_for_owner(user_id)
            result = await self.owner_settings.save(
                payload["settings"],
                actor_id=user_id,
                expected_revision=revision,
            )
        except OwnerAccessRevokedError as error:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Bot-owner access was revoked. Refresh the page."}),
                content_type="application/json",
            ) from error
        except ConfigConflictError as error:
            raise web.HTTPConflict(
                text=json.dumps(
                    {
                        "error": (
                            "Owner settings changed elsewhere. Reload the latest values and try again."
                        )
                    }
                ),
                content_type="application/json",
            ) from error
        restart_required = (
            previous.settings["max_cached_messages"]
            != result.settings["max_cached_messages"]
        )
        changed_fields = sorted(
            key
            for key, value in result.settings.items()
            if previous.settings.get(key) != value
        )
        LOGGER.info(
            "Bot owner %s updated owner setting fields: %s",
            user_id,
            ", ".join(changed_fields) or "none",
        )
        return web.json_response(
            {
                "settings": result.settings,
                "revision": result.revision,
                "requires_restart": list(RESTART_REQUIRED_FIELDS),
                "restart_required": restart_required,
                "presence_synced": result.presence_synced,
                "presence_warning": result.presence_warning,
            }
        )

    async def discord_channels(self, guild_id: str) -> list[dict[str, Any]]:
        if self.dev_mode:
            return [
                {"id": "100000000000000001", "name": "general", "type": 0},
                {"id": "100000000000000002", "name": "mod-log", "type": 0},
                {"id": "100000000000000003", "name": "level-ups", "type": 0},
                {"id": "100000000000000004", "name": "staff-orbit", "type": 0},
                {"id": "100000000000000005", "name": "welcome", "type": 0},
            ]
        if not self.bot_token or not self.http:
            return []
        async with self.http.get(
            f"{DISCORD_API}/guilds/{guild_id}/channels",
            headers={"Authorization": f"Bot {self.bot_token}"},
        ) as response:
            if response.status in {401, 403, 404}:
                return []
            if response.status != 200:
                raise ConnectionError(f"Discord channel request returned {response.status}")
            channels = await response.json()
        return [
            {"id": str(channel["id"]), "name": channel["name"], "type": channel["type"]}
            for channel in channels
            if channel.get("type") in {0, 5}
        ]

    async def discord_roles(self, guild_id: str) -> list[dict[str, Any]]:
        if self.dev_mode:
            return [
                {"id": "200000000000000001", "name": "Verified", "position": 4},
                {"id": "200000000000000002", "name": "Awaiting verification", "position": 3},
                {"id": "200000000000000003", "name": "Member", "position": 2},
            ]
        if not self.bot_token or not self.http:
            return []
        async with self.http.get(
            f"{DISCORD_API}/guilds/{guild_id}/roles",
            headers={"Authorization": f"Bot {self.bot_token}"},
        ) as response:
            if response.status in {401, 403, 404}:
                return []
            if response.status != 200:
                raise ConnectionError(f"Discord role request returned {response.status}")
            roles = await response.json()
        return sorted(
            (
                {
                    "id": str(role["id"]),
                    "name": role["name"],
                    "position": int(role.get("position", 0)),
                }
                for role in roles
                if str(role.get("id")) != guild_id and not role.get("managed")
            ),
            key=lambda role: role["position"],
            reverse=True,
        )

    async def validate_setting_resources(
        self, guild_id: str, settings: dict[str, Any]
    ) -> None:
        """Reject tampered channel and role IDs before standalone DB writes."""
        if self.bot is not None or self.dev_mode:
            return
        channels, roles = await asyncio.gather(
            self.discord_channels(guild_id), self.discord_roles(guild_id)
        )
        allowed_channels = {int(channel["id"]) for channel in channels}
        allowed_roles = {int(role["id"]) for role in roles}
        selected_channels = {
            value
            for value in (
                settings["general"].get("warns_channel"),
                *settings["general"].get("disabled_channels", []),
                *settings["ranking"].get("disabled_channels", []),
                settings["messages"].get("level_up_messages"),
                settings["messages"].get("redirect_mod_commands"),
                settings["verification"].get("channel_id"),
                settings["verification"].get("log_channel"),
            )
            if type(value) is int
        }
        selected_roles = {
            value
            for value in (
                settings["verification"].get("verified_role_id"),
                settings["verification"].get("temp_role_id"),
            )
            if type(value) is int
        }
        if not selected_channels.issubset(allowed_channels):
            raise ValidationError("One or more selected channels are not in this server.")
        if not selected_roles.issubset(allowed_roles):
            raise ValidationError("One or more selected roles are not in this server.")

    async def api_settings(self, request: web.Request) -> web.Response:
        _session, guild = await self.require_guild(request)
        guild_id = int(guild["id"])
        settings, channels, roles = await asyncio.gather(
            self.settings.get(guild_id),
            self.discord_channels(str(guild_id)),
            self.discord_roles(str(guild_id)),
        )
        return web.json_response(
            {"settings": settings, "channels": channels, "roles": roles}
        )

    async def save_settings(self, request: web.Request) -> web.Response:
        session, guild = await self.require_guild(request)
        self.require_csrf(request, session)
        self.enforce_rate_limit(request, "settings-write", limit=30, window=60)
        if request.content_length and request.content_length > 64_000:
            raise web.HTTPRequestEntityTooLarge(max_size=64_000, actual_size=request.content_length)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, TypeError) as error:
            raise ValidationError("Those settings could not be saved. Refresh and try again.") from error
        settings = validate_settings(payload)
        await self.validate_setting_resources(str(guild["id"]), settings)
        saved = await self.settings.save(
            int(guild["id"]),
            settings,
            actor_id=int(session["user"]["id"]),
            refresh_verification=request.query.get("refresh") == "verification",
        )
        return web.json_response({"settings": saved, "saved": True})

    async def api_modules(self, request: web.Request) -> web.Response:
        session, guild = await self.require_guild(request)
        return web.json_response(
            await self.modules.get(
                int(guild["id"]), actor_id=int(session["user"]["id"])
            )
        )

    async def api_activity_logs(self, request: web.Request) -> web.Response:
        session, guild = await self.require_guild(request)
        self.enforce_rate_limit(request, "activity-log-read", limit=60, window=60)
        before_value = request.query.get("before")
        local_before = bool(
            before_value and LOCAL_LOG_CURSOR_PATTERN.fullmatch(before_value)
        )
        if before_value and not local_before and (
            not before_value.isdigit() or len(before_value) > 20
        ):
            raise ValidationError("That Logging page is not available.")
        search_query = request.query.get("q", "").strip()
        if len(search_query) > 200:
            raise ValidationError("Logging searches must be 200 characters or fewer.")
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError as error:
            raise ValidationError("That Logging page size is not available.") from error
        if not 20 <= limit <= 100:
            raise ValidationError("Activity log page size must be between 20 and 100.")
        payload = await self.activity_logs.get(
            int(guild["id"]),
            actor_id=int(session["user"]["id"]),
            before=(before_value if local_before else int(before_value)) if before_value else None,
            limit=limit,
            query=search_query or None,
        )
        return web.json_response(payload)

    async def api_modmail_entries(self, request: web.Request) -> web.Response:
        session, guild = await self.require_guild(request)
        self.enforce_rate_limit(request, "modmail-entry-read", limit=30, window=60)
        payload = await self.modules.modmail_entries(
            int(guild["id"]),
            actor_id=int(session["user"]["id"]),
        )
        return web.json_response(payload)

    async def save_module(self, request: web.Request) -> web.Response:
        session, guild = await self.require_guild(request)
        self.require_csrf(request, session)
        self.enforce_rate_limit(request, "module-write", limit=30, window=60)
        if request.content_length and request.content_length > 64_000:
            raise web.HTTPRequestEntityTooLarge(max_size=64_000, actual_size=request.content_length)
        module = request.match_info["module"]
        try:
            payload = await request.json()
        except (json.JSONDecodeError, TypeError) as error:
            raise ValidationError("Those module settings could not be saved. Refresh and try again.") from error
        settings = validate_module_settings(module, payload)
        saved = await self.modules.save(
            int(guild["id"]),
            module,
            settings,
            actor_id=int(session["user"]["id"]),
        )
        return web.json_response({"module": module, "settings": saved, "saved": True})

    async def api_profile(self, request: web.Request) -> web.Response:
        session, guild = await self.require_guild(request)
        guild_id = int(guild["id"])
        user_id = int(session["user"]["id"])
        settings, style = await asyncio.gather(
            self.settings.get(guild_id),
            self.settings.get_profile_style(user_id),
        )
        ranks = await self.ranking.profile(user_id, guild_id, settings["ranking"])
        return web.json_response(
            {
                "user": public_user(session["user"]),
                "guild": public_guild(guild),
                "title": style.get("title", "Use .set title to customize"),
                "background": style.get("background"),
                "ranks": ranks,
            }
        )

    @staticmethod
    def discord_object_identity(user: Any, user_id: str) -> dict[str, Any]:
        """Convert a cached discord.py user/member into dashboard identity data."""
        display_name = (
            getattr(user, "display_name", None)
            or getattr(user, "global_name", None)
            or getattr(user, "name", None)
            or f"Pilot {user_id[-4:]}"
        )
        display_avatar = getattr(user, "display_avatar", None)
        avatar = getattr(display_avatar, "url", None)
        return {
            "name": str(display_name),
            "avatar": str(avatar) if avatar else None,
        }

    async def resolve_bot_user(
        self, user_id: str, guild_id: str, guild_board: bool
    ) -> dict[str, Any] | None:
        """Prefer Fate's member/user cache and rate-limit-aware Discord client."""
        if self.bot is None:
            return None
        try:
            numeric_user_id = int(user_id)
            guild = self.bot.get_guild(int(guild_id)) if guild_board else None
            if guild is not None:
                member = guild.get_member(numeric_user_id)
                if member is not None:
                    return self.discord_object_identity(member, user_id)

            user = self.bot.get_user(numeric_user_id)
            if user is not None:
                return self.discord_object_identity(user, user_id)

            if guild is not None:
                try:
                    member = await guild.fetch_member(numeric_user_id)
                except Exception:
                    member = None
                if member is not None:
                    return self.discord_object_identity(member, user_id)

            user = await self.bot.fetch_user(numeric_user_id)
            return self.discord_object_identity(user, user_id)
        except Exception:
            return None

    async def resolve_user(
        self, user_id: str, guild_id: str, guild_board: bool
    ) -> dict[str, Any]:
        cache_key = (user_id, guild_id if guild_board else None)
        cached = self.user_cache.get(cache_key)
        if cached and cached[0] > time():
            return cached[1]

        resolved = await self.resolve_bot_user(user_id, guild_id, guild_board)
        if resolved is not None:
            self.user_cache[cache_key] = (time() + 900, resolved)
            return resolved

        if not self.bot_token or not self.http:
            return {"name": f"Pilot {user_id[-4:]}", "avatar": None}

        endpoints = []
        if guild_board:
            endpoints.append(f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}")
        endpoints.append(f"{DISCORD_API}/users/{user_id}")
        for endpoint in endpoints:
            async with self.http.get(
                endpoint, headers={"Authorization": f"Bot {self.bot_token}"}
            ) as response:
                if response.status != 200:
                    continue
                payload = await response.json()
            user = payload.get("user", payload)
            resolved = {
                "name": (
                    payload.get("nick")
                    or user.get("global_name")
                    or user.get("username")
                    or f"Pilot {user_id[-4:]}"
                ),
                "avatar": avatar_url(user),
            }
            self.user_cache[cache_key] = (time() + 900, resolved)
            return resolved
        return {"name": f"Pilot {user_id[-4:]}", "avatar": None}

    async def api_leaderboard(self, request: web.Request) -> web.Response:
        _session, guild = await self.require_guild(request)
        board = request.query.get("board", "guild")
        if board not in BOARD_LABELS:
            raise ValidationError("That leaderboard view is not available.")
        guild_id = str(guild["id"])
        rows = await self.ranking.leaderboard(int(guild_id), board)
        if board != "commands":
            unresolved = [row for row in rows if "name" not in row]
            identities = await asyncio.gather(
                *[
                    self.resolve_user(row["id"], guild_id, board in {"guild", "monthly"})
                    for row in unresolved
                ]
            )
            for row, identity in zip(unresolved, identities):
                row.update(identity)
        return web.json_response(
            {"board": board, "label": BOARD_LABELS[board], "entries": rows}
        )


def register_routes(app: web.Application, dashboard: Dashboard) -> None:
    """Attach dashboard routes to either the standalone or Fate web app."""
    if security_headers not in app.middlewares:
        app.middlewares.append(security_headers)
    app["dashboard"] = dashboard
    app.on_startup.append(dashboard.startup)
    app.on_cleanup.append(dashboard.cleanup)
    app.router.add_get("/", dashboard.index)
    app.router.add_get(DASHBOARD_PATH, dashboard.dashboard_page)
    app.router.add_get(LEGACY_DASHBOARD_PATH, dashboard.legacy_dashboard_redirect)
    app.router.add_get("/auth/discord", dashboard.auth_discord)
    app.router.add_get("/auth/callback", dashboard.auth_callback)
    app.router.add_get("/auth/dev", dashboard.auth_dev)
    app.router.add_post("/auth/logout", dashboard.logout)
    app.router.add_get("/api/runtime-revision", dashboard.api_runtime_revision)
    app.router.add_get("/api/commands", dashboard.api_commands)
    app.router.add_get("/api/session", dashboard.api_session)
    app.router.add_get("/api/guilds", dashboard.api_guilds)
    app.router.add_get("/api/owner/settings", dashboard.api_owner_settings)
    app.router.add_get("/api/owner/memory", dashboard.api_owner_memory)
    app.router.add_put("/api/owner/settings", dashboard.save_owner_settings)
    app.router.add_get("/api/guilds/{guild_id}/settings", dashboard.api_settings)
    app.router.add_put("/api/guilds/{guild_id}/settings", dashboard.save_settings)
    app.router.add_get("/api/guilds/{guild_id}/modules", dashboard.api_modules)
    app.router.add_get(
        "/api/guilds/{guild_id}/activity-logs", dashboard.api_activity_logs
    )
    app.router.add_get(
        "/api/guilds/{guild_id}/modules/modmail/entries",
        dashboard.api_modmail_entries,
    )
    app.router.add_put("/api/guilds/{guild_id}/modules/{module}", dashboard.save_module)
    app.router.add_get("/api/guilds/{guild_id}/profile", dashboard.api_profile)
    app.router.add_get("/api/guilds/{guild_id}/leaderboard", dashboard.api_leaderboard)
    app.router.add_static("/static", STATIC_ROOT, append_version=True)


def create_app() -> web.Application:
    dashboard = Dashboard()
    app = web.Application(middlewares=[security_headers], client_max_size=64_000)
    register_routes(app, dashboard)
    return app


def mount_on_bot(bot) -> Dashboard:
    """Mount Mission Control on Fate's existing aiohttp application."""
    existing = bot.app.get("dashboard")
    if existing is not None:
        return existing
    dashboard = Dashboard(bot=bot)
    register_routes(bot.app, dashboard)
    return dashboard


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    web.run_app(
        create_app(),
        host=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.getenv("DASHBOARD_PORT", "8080")),
    )
