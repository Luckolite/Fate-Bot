"""Authenticated Top.gg v1 vote delivery into the faction redemption queue."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from time import time

from aiohttp import web

log = logging.getLogger(__name__)


class TopggVotes:
    def __init__(self, bot):
        self.bot = bot
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def handle(self, request):
        if self.bot.debug_mode:
            raise web.HTTPServiceUnavailable(text="Voting is disabled on this instance.")
        secret_path = Path(__file__).resolve().parents[1] / "data" / "topgg-webhook.secret"
        try:
            secret = os.getenv("TOPGG_WEBHOOK_SECRET", "").strip() or secret_path.read_text().strip()
        except OSError:
            secret = ""
        if not secret:
            raise web.HTTPServiceUnavailable(text="Top.gg signing secret is not configured.")
        if request.content_length is not None and request.content_length > 16384:
            raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=request.content_length)
        body = bytearray()
        async for chunk in request.content.iter_chunked(4096):
            body.extend(chunk)
            if len(body) > 16384:
                raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=len(body))
        raw = bytes(body)
        try:
            fields = dict(part.strip().split("=", 1) for part in request.headers.get("x-topgg-signature", "").split(","))
            timestamp = fields["t"]
            if abs(time() - int(timestamp)) > 300:
                raise ValueError("Expired signature")
            expected = hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, fields["v1"]):
                raise ValueError("Invalid signature")
        except (KeyError, ValueError):
            raise web.HTTPUnauthorized(text="Invalid Top.gg signature.") from None
        try:
            payload = json.loads(raw)
            event = payload["type"]
            data = payload["data"]
            project = data["project"]
            if (project["type"] != "bot" or project["platform"] != "discord"
                    or str(project["platform_id"]) != str(self.bot.config["bot_user_id"])):
                raise ValueError("Wrong project")
            if event == "webhook.test":
                return web.json_response({"ok": True, "test": True})
            if event != "vote.create":
                raise ValueError("Unsupported event")
            vote_id = str(data["id"])
            user_id = int(data["user"]["platform_id"])
            voted_at = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
            if not vote_id.isascii() or not vote_id.isdigit() or len(vote_id) > 64 or not 0 < user_id < 2**64 or voted_at.tzinfo is None:
                raise ValueError("Invalid vote")
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            raise web.HTTPBadRequest(text="Invalid Top.gg vote payload.") from None
        if not self.bot.pool:
            raise web.HTTPServiceUnavailable(text="Vote storage is not ready.")
        try:
            # Leave time for Top.gg's five-second delivery deadline. Failed writes
            # are not acknowledged, so the provider can retry the same event.
            inserted = await asyncio.wait_for(self.record(vote_id, user_id, voted_at.timestamp()), timeout=3.5)
        except Exception:
            log.exception("Could not persist Top.gg vote")
            raise web.HTTPServiceUnavailable(text="Vote storage temporarily unavailable.") from None
        if inserted:
            self.bot.dispatch("dbl_vote", {"user": str(user_id)})
        return web.json_response({"ok": True, "duplicate": not inserted})

    async def record(self, vote_id, user_id, voted_at):
        async with self._schema_lock:
            if not self._schema_ready:
                async with self.bot.pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "CREATE TABLE IF NOT EXISTS topgg_vote_receipts ("
                            "vote_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY, "
                            "user_id BIGINT UNSIGNED NOT NULL, vote_time DOUBLE NOT NULL) ENGINE=InnoDB"
                        )
                self._schema_ready = True
        async with self.bot.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT IGNORE INTO topgg_vote_receipts (vote_id, user_id, vote_time) VALUES (%s, %s, %s)",
                        (vote_id, user_id, voted_at),
                    )
                    inserted = cur.rowcount == 1
                    if inserted:
                        await cur.execute(
                            "INSERT INTO votes (user_id, vote_time) VALUES (%s, %s)", (user_id, voted_at)
                        )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return inserted
