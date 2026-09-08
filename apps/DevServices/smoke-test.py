"""Verify the local Fate test databases accept real application operations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiomysql
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[2]
SECRETS = json.loads(
    (ROOT / ".fate-test-services" / "test-secrets.json").read_text(encoding="utf-8")
)


async def mysql_smoke_test() -> None:
    pool = await aiomysql.create_pool(
        host="127.0.0.1",
        port=3307,
        user=SECRETS["mysql_user"],
        password=SECRETS["mysql_password"],
        db=SECRETS["mysql_database"],
        autocommit=True,
    )
    try:
        async with pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SHOW TABLES")
                tables = {row[0] for row in await cursor.fetchall()}
                required = {"msg", "global_msg", "monthly_msg", "cases", "blocked"}
                missing = required - tables
                if missing:
                    raise RuntimeError(f"Missing MySQL tables: {sorted(missing)}")
                await cursor.execute("DELETE FROM msg WHERE guild_id = 1 AND user_id = 1")
                await cursor.execute(
                    "INSERT INTO msg VALUES (%s, %s, %s)",
                    (1, 1, 1),
                )
                await cursor.execute("DELETE FROM msg WHERE guild_id = 1 AND user_id = 1")
    finally:
        pool.close()
        await pool.wait_closed()


def mongo_smoke_test() -> None:
    client = MongoClient("mongodb://127.0.0.1:27018", serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
        collection = client["fate_test"]["_smoke_test"]
        collection.replace_one({"_id": "health"}, {"ok": True}, upsert=True)
        if collection.find_one({"_id": "health"})["ok"] is not True:
            raise RuntimeError("MongoDB smoke-test document did not round-trip")
        collection.drop()
    finally:
        client.close()


async def main() -> None:
    await mysql_smoke_test()
    await asyncio.to_thread(mongo_smoke_test)
    print("MySQL and MongoDB smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
