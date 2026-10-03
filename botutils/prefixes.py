"""
Bot Prefixes
~~~~~~~~~~~~~

Contains the functions relative to parsing the bot prefix

:copyright: (C) 2019-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
from weakref import WeakValueDictionary

import discord
from discord.ext import commands


async def save_prefix(bot, collection, key, values):
    """Persist a prefix once, then update its authoritative runtime mapping."""
    values = dict(values) if values is not None else None
    mapping = bot.guild_prefixes if collection == "GuildPrefixes" else bot.user_prefixes
    locks = getattr(bot, "_prefix_write_locks", None)
    if locks is None:
        locks = bot._prefix_write_locks = WeakValueDictionary()
    uncertain = getattr(bot, "_prefix_uncertain_writes", None)
    if uncertain is None:
        uncertain = bot._prefix_uncertain_writes = set()
    identity = (collection, key)
    lock = locks.setdefault(identity, asyncio.Lock())
    async with lock:
        if values is None:
            if key not in mapping and identity not in uncertain:
                return False
            uncertain.add(identity)
            await bot.aio_mongo[collection].delete_one({"_id": key})
            mapping.pop(key, None)
            uncertain.discard(identity)
            return True
        if mapping.get(key) == values and identity not in uncertain:
            return False
        uncertain.add(identity)
        await bot.aio_mongo[collection].update_one(
            {"_id": key}, {"$set": values}, upsert=True,
        )
        mapping[key] = dict(values)
        uncertain.discard(identity)
        return True


def get_prefix(_ctx):
    """Deprecated"""
    return "."


async def get_prefixes_async(bot, msg):
    """Cache the users prefix if not already cached"""
    default_prefix = commands.when_mentioned_or(".")(bot, msg)
    prefixes = []
    override = False

    guild_id = msg.guild.id if msg.guild else None
    user_id = msg.author.id

    if guild_id and guild_id in bot.guild_prefixes:
        prefixes.append(bot.guild_prefixes[guild_id]["prefix"])
        if bot.guild_prefixes[guild_id]["override"]:
            override = True

    if not override and user_id in bot.user_prefixes:
        prefixes.append(bot.user_prefixes[user_id]["prefix"])

    if not isinstance(msg.guild, discord.Guild):
        return prefixes if prefixes else default_prefix

    # Parse the wanted prefixes
    if not prefixes:
        return default_prefix
    return [
        *commands.when_mentioned(bot, msg),
        *prefixes
    ]
