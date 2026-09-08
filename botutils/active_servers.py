"""Shared rolling active-server lookup backed by monthly XP."""

from time import time


ACTIVE_WINDOW_SECONDS = 60 * 60 * 24 * 30


def _pending_active_guild_ids(ranking, cutoff: int) -> set[str]:
    return {
        str(key[0])
        for key in getattr(ranking, "pending_monthly", {})
        if len(key) >= 3 and key[2] > cutoff
    }


async def active_guild_ids(bot, *, now: float | None = None) -> set[str]:
    """Return guilds that recorded monthly XP during the last 30 days."""
    cutoff = int((time() if now is None else now) - ACTIVE_WINDOW_SECONDS)
    ranking = bot.get_cog("Ranking")
    # Snapshot before and after the query so a concurrent XP flush cannot move
    # a guild out of memory just after MySQL selected its rows.
    pending_before = _pending_active_guild_ids(ranking, cutoff)
    async with bot.utils.cursor() as cur:
        await cur.execute(
            "select distinct guild_id from monthly_msg where msg_time > %s;",
            (cutoff,),
        )
        rows = await cur.fetchall()

    guild_ids = {str(row[0]) for row in rows if row}
    return guild_ids | pending_before | _pending_active_guild_ids(ranking, cutoff)
