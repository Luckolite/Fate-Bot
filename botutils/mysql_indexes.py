"""Indexes supporting the Discord bot's global XP reads."""

import re

from pymysql.err import OperationalError


def _has_xp_order_index(rows):
    return any(
        row[3] == 1 and row[4] == "xp" and row[7] is None
        and row[10].upper() == "BTREE"
        and (len(row) <= 13 or row[13] != "NO")
        for row in rows
    )


async def ensure_xp_order_index(pool, table="global_msg"):
    """Add a missing leading-XP index without duplicating an existing one.

    Online DDL is required: unsupported engines/permissions must fail instead
    of silently falling back to an exclusive table rebuild. The optional table
    name allows isolated integration checks without touching bot data.
    """
    if not re.fullmatch(r"[a-zA-Z0-9_]+", table):
        raise ValueError("Invalid XP table name")
    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(f"SHOW INDEX FROM `{table}`")
            if _has_xp_order_index(await cursor.fetchall()):
                return False
            try:
                await cursor.execute(
                    f"ALTER TABLE `{table}` ADD INDEX fate_xp_order_idx (xp), "
                    "ALGORITHM=INPLACE, LOCK=NONE"
                )
            except OperationalError as error:
                if error.args[0] != 1061:
                    raise
                # Another bot instance may have installed it after our check.
                await cursor.execute(f"SHOW INDEX FROM `{table}`")
                if not _has_xp_order_index(await cursor.fetchall()):
                    raise
                return False
    return True
