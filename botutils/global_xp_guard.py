"""Persistent, global-only XP safeguards. No presence or message content is stored."""

import asyncio
import json
import math
from dataclasses import asdict, dataclass, field
from time import time
from uuid import uuid4
from weakref import WeakValueDictionary


DAY = 86400
REASONS = {
    "sustained_activity": "Dense XP activity throughout at least 36 hours",
    "regular_cadence": "Near-identical message timing sustained for at least two hours",
}


@dataclass
class Activity:
    last_award: float = 0
    last_message: int = 0
    started: float = 0
    hours: dict = field(default_factory=dict)
    interval: float = 0
    regular_since: float = 0
    regular_count: int = 0
    blocked_until: float = 0
    last_flag: float = 0
    strikes: int = 0
    reason: str = ""

    def observe(self, now, message_id):
        """Return (award, flag). Call only for fresh human-account guild messages."""
        if now < self.blocked_until or message_id <= self.last_message:
            return False, None
        if now - self.last_award < 10:
            return False, None

        previous = self.last_award
        gap = now - previous
        if not self.started or gap > 3600:
            self.started = now
            self.hours = {}
        slot = int(now // 1800)
        self.hours = {int(k): v for k, v in self.hours.items() if int(k) >= slot - 72}
        self.hours[slot] = self.hours.get(slot, 0) + 1

        if previous and 10 <= gap <= 300:
            if self.regular_count and abs(gap - self.interval) <= 0.15:
                self.regular_count += 1
            else:
                self.interval = gap
                self.regular_since = previous
                self.regular_count = 1
        else:
            self.regular_count = 0
            self.regular_since = now
        self.last_award = now
        self.last_message = message_id

        reason = None
        # Sparse overnight pings and a single long waking day are insufficient.
        if (now - self.started >= 36 * 3600
                and sum(self.hours.get(k, 0) >= 10 for k in range(slot - 72, slot)) >= 70
                and sum(self.hours.get(k, 0) for k in range(slot - 72, slot)) >= 720):
            reason = "sustained_activity"
        elif self.regular_count >= 180 and now - self.regular_since >= 2 * 3600:
            reason = "regular_cadence"

        if reason:
            self.strikes = self.strikes + 1 if now - self.last_flag < 30 * DAY else 1
            self.last_flag = now
            self.reason = reason
            self.blocked_until = now + (DAY, 3 * DAY, 7 * DAY)[min(self.strikes, 3) - 1]
            self.reset_evidence()
            return False, {
                "user_id": None, "flagged_at": now, "reason": reason,
                "blocked_until": self.blocked_until,
                # Only remove buckets wholly inside the window. Never take older XP.
                "cutoff": math.ceil((now - DAY) / 60) * 60,
                "end": int(now // 60) * 60,
            }
        return True, None

    def reset_evidence(self):
        self.started = 0
        self.hours = {}
        self.regular_count = 0
        self.regular_since = 0
        self.interval = 0


SCHEMA = (
    "CREATE TABLE IF NOT EXISTS global_xp_activity ("
    "user_id BIGINT UNSIGNED PRIMARY KEY, state MEDIUMTEXT NOT NULL, "
    "updated_at DOUBLE NOT NULL, INDEX global_xp_activity_updated (updated_at)) ENGINE=InnoDB",
    "CREATE TABLE IF NOT EXISTS global_xp_batches ("
    "batch_id CHAR(36) CHARACTER SET ascii PRIMARY KEY, created_at DOUBLE NOT NULL, "
    "INDEX global_xp_batches_created (created_at)) ENGINE=InnoDB",
    "CREATE TABLE IF NOT EXISTS global_xp_flags ("
    "batch_id CHAR(36) CHARACTER SET ascii NOT NULL, user_id BIGINT UNSIGNED NOT NULL, "
    "flagged_at DOUBLE NOT NULL, reason VARCHAR(40) NOT NULL, blocked_until DOUBLE NOT NULL, "
    "removed_xp BIGINT NOT NULL, cutoff DOUBLE NOT NULL, "
    "PRIMARY KEY (batch_id, user_id, flagged_at), INDEX global_xp_flags_user (user_id, flagged_at), "
    "INDEX global_xp_flags_time (flagged_at)) ENGINE=InnoDB",
)


class GlobalXPGuard:
    """One instance per bot process, sharing the existing global XP flush cadence.

    Awards, activity checkpoints, block records and rollbacks commit together.
    Retried batches carry receipts so a lost commit response cannot double XP.
    """

    def __init__(self, bot):
        self.bot = bot
        self.states = {}
        self.access = {}
        self.dirty = set()
        self.awards = {}
        self.flags = []
        self.batch = None
        self.locks = WeakValueDictionary()
        self.flush_lock = asyncio.Lock()
        self.schema_lock = asyncio.Lock()
        self.schema_ready = False

    async def ensure_schema(self):
        async with self.schema_lock:
            if not self.schema_ready:
                async with self.bot.pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        for query in SCHEMA:
                            await cur.execute(query)
                        await cur.execute(
                            "SELECT table_name, engine FROM information_schema.tables "
                            "WHERE table_schema = DATABASE() AND table_name IN ('global_msg', 'global_monthly')"
                        )
                        engines = {name.lower(): engine.lower() for name, engine in await cur.fetchall()}
                        if engines != {"global_msg": "innodb", "global_monthly": "innodb"}:
                            raise RuntimeError("Global XP safeguards require InnoDB global_msg and global_monthly tables")
                self.schema_ready = True

    async def state(self, user_id):
        lock = self.locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            if user_id not in self.states:
                await self.ensure_schema()
                async with self.bot.pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT state FROM global_xp_activity WHERE user_id = %s", (user_id,))
                        row = await cur.fetchone()
                self.states[user_id] = Activity(**json.loads(row[0])) if row else Activity()
            self.access[user_id] = time()
            return self.states[user_id]

    async def observe(self, user_id, message_id, now):
        state = await self.state(user_id)
        # No awaits between evaluation and enqueue: concurrent guild listeners
        # cannot award twice or bypass a newly raised block.
        before = state.last_message
        award, flag = state.observe(now, message_id)
        if state.last_message != before:
            self.dirty.add(user_id)
        if award:
            key = (user_id, int(now // 60) * 60)
            self.awards[key] = self.awards.get(key, 0) + 1
        if flag:
            flag["user_id"] = user_id
            self.flags.append(flag)
        return award

    async def release(self, user_id):
        state = await self.state(user_id)
        state.blocked_until = 0
        state.strikes = 0
        state.reset_evidence()
        self.dirty.add(user_id)
        await self.flush()

    def snapshot(self):
        if not self.dirty and not self.awards and not self.flags:
            return None
        batch = {
            "id": str(uuid4()), "time": time(),
            "states": {user: json.dumps(asdict(self.states[user]), separators=(",", ":")) for user in self.dirty},
            "awards": self.awards, "flags": self.flags,
        }
        self.dirty = set()
        self.awards = {}
        self.flags = []
        return batch

    async def flush(self):
        async with self.flush_lock:
            await self.ensure_schema()
            # Retry the previous snapshot before taking a newer checkpoint.
            # Activity can continue in memory while MySQL is unavailable.
            changed = False
            for _ in range(2):
                if self.batch is None:
                    self.batch = self.snapshot()
                if self.batch is None:
                    break
                await self.write_batch(self.batch)
                changed = True
                self.batch = None
            self.evict()
            return changed

    async def write_batch(self, batch):
        async with self.bot.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT IGNORE INTO global_xp_batches (batch_id, created_at) VALUES (%s, %s)",
                        (batch["id"], batch["time"]),
                    )
                    if cur.rowcount:
                        totals = {}
                        for (user, _slot), xp in batch["awards"].items():
                            totals[user] = totals.get(user, 0) + xp
                        if totals:
                            await cur.executemany(
                                "INSERT INTO global_msg (user_id, xp) VALUES (%s, %s) "
                                "ON DUPLICATE KEY UPDATE xp = global_msg.xp + VALUES(xp)", list(totals.items()),
                            )
                            await cur.executemany(
                                "INSERT INTO global_monthly (user_id, timeframe, xp) VALUES (%s, %s, %s) "
                                "ON DUPLICATE KEY UPDATE xp = global_monthly.xp + VALUES(xp)",
                                [(user, slot, xp) for (user, slot), xp in batch["awards"].items()],
                            )
                        for flag in batch["flags"]:
                            user = flag["user_id"]
                            # Lock the aggregate before the ledger rows; the same order
                            # as awards. Only this account's recent global XP is touched.
                            await cur.execute("SELECT xp FROM global_msg WHERE user_id = %s FOR UPDATE", (user,))
                            row = await cur.fetchone()
                            total = max(0, int(row[0])) if row else 0
                            await cur.execute(
                                "SELECT timeframe, xp FROM global_monthly WHERE user_id = %s "
                                "AND timeframe >= %s AND timeframe <= %s FOR UPDATE",
                                (user, flag["cutoff"], flag["end"]),
                            )
                            removed = sum(max(0, int(row[1])) for row in await cur.fetchall())
                            await cur.execute(
                                "UPDATE global_msg SET xp = GREATEST(0, xp - %s) WHERE user_id = %s",
                                (removed, user),
                            )
                            await cur.execute(
                                "DELETE FROM global_monthly WHERE user_id = %s AND timeframe >= %s AND timeframe <= %s",
                                (user, flag["cutoff"], flag["end"]),
                            )
                            await cur.execute(
                                "INSERT INTO global_xp_flags "
                                "(batch_id, user_id, flagged_at, reason, blocked_until, removed_xp, cutoff) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                                (batch["id"], user, flag["flagged_at"], flag["reason"],
                                 flag["blocked_until"], min(total, removed), flag["cutoff"]),
                            )
                        if batch["states"]:
                            await cur.executemany(
                                "INSERT INTO global_xp_activity (user_id, state, updated_at) VALUES (%s, %s, %s) "
                                "ON DUPLICATE KEY UPDATE state = VALUES(state), updated_at = VALUES(updated_at)",
                                [(user, state, batch["time"]) for user, state in batch["states"].items()],
                            )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    def evict(self):
        cutoff = time() - 7200
        pending = self.batch["states"] if self.batch else {}
        for user, accessed in list(self.access.items()):
            if accessed < cutoff and user not in self.dirty and user not in pending and user not in self.locks:
                self.access.pop(user, None)
                self.states.pop(user, None)

    async def latest_flag(self, user_id):
        await self.ensure_schema()
        async with self.bot.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT removed_xp FROM global_xp_flags WHERE user_id = %s ORDER BY flagged_at DESC LIMIT 1",
                    (user_id,),
                )
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def cleanup(self, now):
        await self.ensure_schema()
        async with self.flush_lock:
            async with self.bot.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    # Never delete the receipt for an unresolved commit response.
                    pending_id = self.batch["id"] if self.batch else ""
                    for query, args in (
                        ("DELETE FROM global_xp_batches WHERE created_at < %s AND batch_id <> %s LIMIT 5000",
                         (now - 90 * DAY, pending_id)),
                        ("DELETE FROM global_xp_flags WHERE flagged_at < %s LIMIT 5000", (now - 90 * DAY,)),
                        ("DELETE FROM global_xp_activity WHERE updated_at < %s LIMIT 5000", (now - 35 * DAY,)),
                    ):
                        await cur.execute(query, args)
                await conn.commit()
