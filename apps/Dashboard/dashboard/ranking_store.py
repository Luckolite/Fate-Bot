"""MySQL-backed profile and leaderboard queries matching Fate's ranking cog."""

from __future__ import annotations

import asyncio
from time import time
from typing import Any

import aiomysql

from .settings_store import RANKING_DEFAULTS

BOARD_LABELS = {
    "guild": "Server — All time",
    "global": "Global — All time",
    "monthly": "Server — Last 30 days",
    "global_monthly": "Global — Last 30 days",
    "commands": "Commands — Last 30 days",
}


def calculate_level(xp: int, config: dict[str, Any]) -> dict[str, int]:
    level = 0
    remaining = max(0, int(xp))
    base_requirement = int(config["first_lvl_xp_req"])
    multiplier = 1.0
    increase_by = 0.125
    reduce_at = 3
    current_requirement = float(base_requirement)
    while True:
        current_requirement = base_requirement * multiplier
        if remaining < current_requirement:
            break
        remaining -= current_requirement
        level += 1
        if level >= 500:
            remaining = current_requirement
            break
        if multiplier >= reduce_at:
            increase_by /= 2
            reduce_at += 3
        multiplier += increase_by
    maximum = max(1, round(current_requirement))
    progress = max(0, min(maximum, round(remaining)))
    return {
        "level": level,
        "xp": int(xp),
        "progress": progress,
        "next_level_xp": maximum,
        "required": max(0, maximum - progress),
        "percent": round((progress / maximum) * 100),
    }


class MySQLRankingStore:
    def __init__(self, **config: Any):
        self.config = config
        self.pool = None
        self._pool_lock = asyncio.Lock()

    async def _pool(self):
        async with self._pool_lock:
            if self.pool is None:
                options = dict(minsize=1, maxsize=5, autocommit=True,
                               connect_timeout=4, pool_recycle=1800)
                options.update(self.config)
                self.pool = await aiomysql.create_pool(**options)
            return self.pool

    async def close(self):
        async with self._pool_lock:
            if self.pool is not None:
                self.pool.close()
                await self.pool.wait_closed()
                self.pool = None

    async def _fetchall(self, query: str, args: tuple = ()):
        pool = await self._pool()
        async with pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(query, args)
                return await cursor.fetchall()

    async def profile(
        self,
        user_id: int,
        guild_id: int,
        ranking_config: dict[str, Any],
    ) -> dict[str, Any]:
        rows = await self._fetchall(
            "select 'server', target.xp, (select count(*) + 1 from msg ranked "
            "where ranked.guild_id = target.guild_id and ranked.xp > target.xp) "
            "from (select guild_id, xp from msg where guild_id = %s "
            "and user_id = %s limit 1) target union all "
            "select 'global', target.xp, (select count(*) + 1 from global_msg ranked "
            "where ranked.xp > target.xp) from "
            "(select xp from global_msg where user_id = %s limit 1) target",
            (guild_id, user_id, user_id),
        )

        scores = {board: (xp, rank) for board, xp, rank in rows}
        server_xp, server_rank = scores.get("server", (0, None))
        global_xp, global_rank = scores.get("global", (0, None))
        return {
            "server": {**calculate_level(server_xp, ranking_config), "rank": server_rank},
            "global": {**calculate_level(global_xp, RANKING_DEFAULTS), "rank": global_rank},
        }

    async def leaderboard(self, guild_id: int, board: str, limit: int = 25):
        cutoff = int(time() - 60 * 60 * 24 * 30)
        queries = {
            "guild": (
                "select user_id, xp from msg where guild_id = %s order by xp desc limit %s",
                (guild_id, limit),
            ),
            "global": (
                "select user_id, xp from global_msg order by xp desc limit %s",
                (limit,),
            ),
            "monthly": (
                "select user_id, sum(xp) from monthly_msg where guild_id = %s "
                "and msg_time > %s group by user_id order by sum(xp) desc limit %s",
                (guild_id, cutoff, limit),
            ),
            "global_monthly": (
                "select user_id, sum(xp) from global_monthly where timeframe > %s "
                "group by user_id order by sum(xp) desc limit %s",
                (cutoff, limit),
            ),
            "commands": (
                "select command, sum(total) from commands where ran_at >= %s "
                "group by command order by sum(total) desc limit %s",
                (cutoff, limit),
            ),
        }
        query, args = queries[board]
        rows = await self._fetchall(query, args)
        return [
            {"id": str(row[0]), "score": int(row[1]), "rank": index + 1}
            for index, row in enumerate(rows)
        ]


class MemoryRankingStore:
    USERS = [
        ("457210410819649536", "Nova", 18420),
        ("243233669148442624", "Andromeda", 15980),
        ("1083921045574217728", "Cosmo", 12730),
        ("876423190855442432", "Luna", 11040),
        ("691244730275004487", "Orion", 9870),
        ("591122875414233110", "Astra", 8410),
        ("715920448153649202", "Comet", 7240),
    ]

    async def close(self):
        return None

    async def profile(self, _user_id: int, _guild_id: int, ranking_config: dict[str, Any]):
        return {
            "server": {**calculate_level(4820, ranking_config), "rank": 7},
            "global": {**calculate_level(12840, RANKING_DEFAULTS), "rank": 42},
        }

    async def leaderboard(self, _guild_id: int, board: str, limit: int = 25):
        multiplier = 1 if board in {"guild", "global"} else 0.32
        if board == "commands":
            commands = [("help", 48291), ("profile", 38742), ("leaderboard", 29214), ("rank", 24002), ("purge", 18401)]
            return [
                {"id": name, "name": name, "score": score, "rank": index + 1}
                for index, (name, score) in enumerate(commands)
            ]
        return [
            {"id": user_id, "name": name, "score": int(xp * multiplier), "rank": index + 1}
            for index, (user_id, name, xp) in enumerate(self.USERS[:limit])
        ]


class BotRankingStore(MySQLRankingStore):
    """Reuse Fate's existing MySQL pool when mounted inside the bot."""

    def __init__(self, bot):
        self.bot = bot
        self.pool = None

    async def close(self):
        return None

    async def _fetchall(self, query: str, args: tuple = ()):
        await self.bot.wait_for_pool()
        async with self.bot.utils.cursor() as cursor:
            await cursor.execute(query, args)
            return await cursor.fetchall()
