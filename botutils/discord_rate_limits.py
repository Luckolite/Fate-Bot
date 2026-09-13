"""Discord REST pacing and privacy-bounded rate-limit telemetry.

The tracker runs on aiohttp response hooks, before discord.py decides whether to
sleep and retry. It records normalized endpoint paths for 429 responses, retaining
channel and message IDs for incident tracing while excluding query strings,
payloads, response bodies, and secret webhook/interaction tokens. It never reports
through Discord while Discord itself is rate limited. Authenticated bot requests
are paced before acquiring a connection; discord.py retains its per-route retry
handling, and interaction callbacks keep their separate request allowance.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from contextlib import suppress
from math import isfinite
from time import monotonic, time
from typing import Any

import aiohttp

RATE_LIMIT_METRIC = "discord_rate_limits"
GLOBAL_RATE_LIMIT_METRIC = "discord_global_rate_limits"
INVALID_REQUEST_METRIC = "discord_invalid_requests"
API_BLOCK_METRIC = "discord_api_blocks"
_API_PREFIX = re.compile(r"^/api(?:/v\d+)?")
_SNOWFLAKE = re.compile(r"^\d{5,25}$")
_REDACT_NEXT_SEGMENT = frozenset({"invites", "templates"})
_SAFE_STATIC_SEGMENT = re.compile(r"^[a-z0-9_@.*-]+$")
_MESSAGE_DELETE_PATH = re.compile(r"^/channels/(\d+)/messages/(\d+)$")
# Conservative spacing for the old-message sublimit observed during purges.
# Discord's response deadlines can extend this; it is not a global REST cap.
OLD_MESSAGE_DELETE_INTERVAL = 1.1


def _old_delete_channel(params: Any) -> str | None:
    if str(getattr(params, "method", "")).upper() != "DELETE":
        return None
    match = _MESSAGE_DELETE_PATH.fullmatch(_API_PREFIX.sub("", params.url.path, count=1))
    if match is None:
        return None
    created_at = ((int(match[2]) >> 22) + 1420070400000) / 1000
    return match[1] if time() - created_at >= 14 * 24 * 60 * 60 else None


def normalize_discord_route(method: Any, url: Any) -> str | None:
    """Return a bounded endpoint label with channel/message IDs retained.

    Other snowflakes are collapsed to ``:id`` to reduce cardinality. Secret URL
    components are always redacted, even though this database is local.
    """
    try:
        host = str(url.host or "").lower()
        path = str(url.path or "")
    except (AttributeError, TypeError, ValueError):
        return None
    if host not in {"discord.com", "discordapp.com"} or not path.startswith("/api/"):
        return None

    verb = str(method or "").strip().upper()
    if verb not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        verb = "OTHER"
    route_path = _API_PREFIX.sub("", path, count=1) or "/"
    segments = [segment for segment in route_path.split("/") if segment]
    normalized: list[str] = []
    for index, segment in enumerate(segments):
        previous = segments[index - 1] if index else ""
        if segments[0] in {"webhooks", "interactions"} and index == 1:
            normalized.append(":id")
            continue
        if segments[0] in {"webhooks", "interactions"} and index == 2:
            normalized.append(":token")
            continue
        if previous in _REDACT_NEXT_SEGMENT:
            normalized.append(":code")
            continue
        if previous == "reactions":
            normalized.append(":reaction")
            continue
        if _SNOWFLAKE.fullmatch(segment):
            # These two positions are explicitly useful for tracing the source
            # of a rate-limit incident and are safe to retain without content.
            if previous in {"channels", "messages", "pins"}:
                normalized.append(segment)
            else:
                normalized.append(":id")
            continue
        static_segment = segment.lower()
        normalized.append(
            static_segment if _SAFE_STATIC_SEGMENT.fullmatch(static_segment) else ":segment"
        )
    return f"{verb} /{'/'.join(normalized)}"[:240]


def classify_discord_response(status: int, headers: Mapping[str, Any]) -> tuple[str, ...]:
    """Return content-free counters for one Discord API response."""
    metrics: list[str] = []
    try:
        scope = str(headers.get("X-RateLimit-Scope", "")).strip().lower()
        global_header = str(headers.get("X-RateLimit-Global", "")).strip().lower()
        via = str(headers.get("Via", "")).strip()
    except (AttributeError, TypeError, ValueError):
        scope = ""
        global_header = ""
        via = ""

    if status == 429:
        metrics.append(RATE_LIMIT_METRIC)
        if scope == "global" or global_header == "true":
            metrics.append(GLOBAL_RATE_LIMIT_METRIC)
        # discord.py treats a 429 without Discord's Via header as a Cloudflare-style
        # API block instead of a normal retryable bucket response.
        if not via:
            metrics.append(API_BLOCK_METRIC)
        # Discord excludes shared-resource 429s from the invalid-request threshold.
        if scope != "shared":
            metrics.append(INVALID_REQUEST_METRIC)
    elif status in {401, 403}:
        metrics.append(INVALID_REQUEST_METRIC)
    return tuple(metrics)


class DiscordRateLimitTracker:
    """Pace bot REST attempts and attach safe counters to discord.py's session."""

    def __init__(self, collector: Any, trace: aiohttp.TraceConfig | None = None) -> None:
        self.collector = collector
        self.trace = trace if trace is not None else aiohttp.TraceConfig()
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._global_retry_at = 0.0
        self._old_delete_deadlines: dict[str, float] = {}
        # Wait before aiohttp acquires a connection. Sleeping in the headers hook
        # holds an idle transport that Discord can close during a slow purge.
        # Each discord.py retry starts a new session.request and runs this hook.
        self.trace.on_request_start.append(self._before_request_start)
        self.trace.on_request_end.append(self._on_request_end)

    async def _before_request_start(self, _session: Any, _context: Any, params: Any) -> None:
        if not self._is_discord_api_request(params.url):
            return
        authorization = str(params.headers.get("Authorization", ""))
        path = _API_PREFIX.sub("", params.url.path, count=1)
        if not authorization.startswith("Bot ") or path.startswith("/interactions/"):
            return
        channel_id = _old_delete_channel(params)
        while True:
            async with self._request_lock:
                current = monotonic()
                self._old_delete_deadlines = {
                    channel: deadline for channel, deadline in self._old_delete_deadlines.items()
                    if deadline > current
                }
                delay = max(
                    self._next_request_at, self._global_retry_at,
                    self._old_delete_deadlines.get(channel_id, 0.0),
                ) - current
                if delay <= 0:
                    # Smooth 40 requests/second, leaving headroom below Discord's 50/s.
                    self._next_request_at = current + 0.025
                    if channel_id is not None:
                        self._old_delete_deadlines[channel_id] = current + OLD_MESSAGE_DELETE_INTERVAL
                    return
            # A channel's slow purge must not hold the global lock while waiting.
            await asyncio.sleep(delay)

    @staticmethod
    def _is_discord_api_request(url: Any) -> bool:
        try:
            host = str(url.host or "").lower()
            path = str(url.path or "")
        except (AttributeError, TypeError, ValueError):
            return False
        return host in {"discord.com", "discordapp.com"} and path.startswith("/api/")

    async def _on_request_end(self, _session: Any, _context: Any, params: Any) -> None:
        """Record the raw response attempt without ever disrupting Discord traffic."""
        try:
            if not self._is_discord_api_request(params.url):
                return
            response = params.response
            metrics = classify_discord_response(int(response.status), response.headers)
            channel_id = _old_delete_channel(params)
            if channel_id is not None:
                with suppress(TypeError, ValueError):
                    delay = 0.0
                    if int(response.status) == 429:
                        delay = float(response.headers.get("Retry-After", 0))
                    elif response.headers.get("X-RateLimit-Remaining") == "0":
                        delay = float(response.headers.get("X-RateLimit-Reset-After", 0))
                    if isfinite(delay) and delay > 0:
                        self._old_delete_deadlines[channel_id] = max(
                            self._old_delete_deadlines.get(channel_id, 0.0),
                            monotonic() + delay + 0.1,
                        )
            if GLOBAL_RATE_LIMIT_METRIC in metrics:
                with suppress(TypeError, ValueError):
                    retry_after = float(response.headers.get("Retry-After", 0))
                    if isfinite(retry_after) and retry_after > 0:
                        self._global_retry_at = max(
                            self._global_retry_at, monotonic() + retry_after,
                        )
            route = normalize_discord_route(getattr(params, "method", ""), params.url)
            for metric in metrics:
                with suppress(Exception):
                    self.collector.increment(
                        metric,
                        dimension=route or "",
                    )
        except Exception:
            # An observability hook must never become an API failure of its own.
            return
