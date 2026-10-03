"""
External Resources
~~~~~~~~~~~~~~~~~~~

Utility module to make managing files & dbs easier

Classes:
    Cache
    TempDownload

Functions:
    save_json
    download

:copyright: (C) 2020-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import json
import os
import shutil
from contextlib import suppress
from copy import deepcopy
from pathlib import Path

import aiofiles
import aiohttp
import pymongo.errors
from pymongo import InsertOne, ReplaceOne
import pymysql
from discord.ext import tasks

# These legacy mapping caches are populated synchronously by their consumers.
# Hydrate them together before extensions load so a slow MongoDB connection
# cannot freeze the event loop once each cog starts constructing its cache.
BUILTIN_CACHE_COLLECTIONS = frozenset(
    {
        "AntiSpam",
        "Test",
        "anti_raid",
        "autorole_timers",
        "chatbridges",
        "chatfilter",
        "cookies",
        "disabled",
        "invites",
        "limiter",
        "locks",
        "messages",
        "modmail",
        "responses",
        "restricted",
        "role_timers",
        "rules",
        "selfroles",
        "server_stats",
        "settings",
        "sexuality",
        "starboard",
        "starboard_posts",
        "suggestions",
        "translations",
        "vclog",
        "verification",
        "welcome",
    }
)
MONGO_FLUSH_BATCH_SIZE = 128


def load_builtin_cache_snapshots(
    bot,
    collections=None,
) -> dict[str, list[tuple[dict, dict]]]:
    """Read selected legacy Mongo mapping caches from a worker thread."""
    snapshots = {}
    database = bot.mongo
    collection_names = (
        BUILTIN_CACHE_COLLECTIONS
        if collections is None
        else frozenset(collections)
    )
    for collection_name in sorted(collection_names):
        documents = {}
        for document in database[collection_name].find({}):
            documents[document["_id"]] = {
                key: value for key, value in document.items() if key != "_id"
            }
        snapshots[collection_name] = [(documents, deepcopy(documents))]
    return snapshots


async def _replace_file(source: str, destination: str) -> None:
    """Replace a file, falling back when Windows denies replacing an open target."""
    try:
        os.replace(source, destination)
    except PermissionError:
        await asyncio.to_thread(shutil.copyfile, source, destination)
        with suppress(FileNotFoundError):
            os.remove(source)


class Cache:
    """Object for syncing a dict to MongoDB"""
    def __init__(self, bot, collection, auto_sync=False):
        self.bot = bot
        self.collection = collection
        snapshots = getattr(bot, "_resource_cache_snapshots", None)
        seeds = snapshots.get(collection) if snapshots is not None else None
        if seeds:
            self._cache, self._db_state = seeds.pop()
        else:
            self._cache = {}
            for config in bot.mongo[collection].find({}):
                self._cache[config["_id"]] = {
                    key: value for key, value in config.items() if key != "_id"
                }
            self._db_state = deepcopy(self._cache)
        self.auto_sync = auto_sync
        self.task = None
        self._flush_lock = asyncio.Lock()

    async def sync_task(self):
        try:
            await asyncio.sleep(10)
            await self.flush()
        finally:
            self.task = None
        # An edit can arrive while MongoDB acknowledges the first snapshot.
        # Its setter saw an active task, so queue another delayed flush now.
        if self.auto_sync and any(
            key not in self._db_state or value != self._db_state[key]
            for key, value in self._cache.items()
        ):
            self.task = self.bot.loop.create_task(self.sync_task())

    async def flush(self):
        async with self._flush_lock:
            collection = self.bot.aio_mongo[self.collection]
            batch = []
            for key in list(self._cache):
                if key in self._cache and (
                    key not in self._db_state or self._cache[key] != self._db_state[key]
                ):
                    # A cog can mutate nested values while the database yields.
                    # Acknowledge only this snapshot, never the later live value.
                    batch.append((key, deepcopy(self._cache[key])))
                if len(batch) == MONGO_FLUSH_BATCH_SIZE:
                    await self._flush_batch(collection, batch)
                    batch = []
            if batch:
                await self._flush_batch(collection, batch)

    async def _flush_batch(self, collection, batch):
        operations = [
            InsertOne({"_id": key, **value}) if key not in self._db_state
            else ReplaceOne({"_id": key}, value, upsert=True)
            for key, value in batch
        ]
        error = None
        failed = set()
        try:
            await collection.bulk_write(operations, ordered=False)
        except pymongo.errors.BulkWriteError as exc:
            # Without write-concern acknowledgement, retain the whole batch.
            if exc.details.get("writeConcernErrors"):
                raise
            for failure in exc.details["writeErrors"]:
                index = failure["index"]
                # Preserve the legacy insert behavior for documents already
                # present in MongoDB; replacement failures must still retry.
                if failure["code"] != 11000 or not isinstance(operations[index], InsertOne):
                    failed.add(index)
                    error = exc
        for index, (key, value) in enumerate(batch):
            if index not in failed:
                self._db_state[key] = value
        if error is not None:
            raise error

    def keys(self):
        return self._cache.keys()

    def items(self):
        return self._cache.items()

    def values(self):
        return self._cache.values()

    def get(self, *args, **kwargs):
        return self._cache.get(*args, **kwargs)

    def __len__(self):
        return len(self._cache)

    def __contains__(self, item):
        return item in self._cache

    def __iter__(self):
        return iter(self._cache)

    def __getitem__(self, item):
        return self._cache[item]

    def __setitem__(self, key, value):
        self._cache[key] = value
        if self.auto_sync and not self.task:
            self.task = self.bot.loop.create_task(self.sync_task())

    def remove(self, key):
        return self.bot.loop.create_task(self._remove_from_db(key))

    def remove_sub(self, key, sub_key):
        return self.bot.loop.create_task(self._remove_from_db(key, sub_key))

    async def _remove_from_db(self, key, sub_key=None):
        async with self._flush_lock:
            await self._remove_locked(key, sub_key)

    async def _remove_locked(self, key, sub_key=None):
        collection = self.bot.aio_mongo[self.collection]
        if sub_key:
            await collection.update_one(
                filter={"_id": key},
                update={"$unset": {sub_key: 1}}
            )
            with suppress(KeyError):
                del self._cache[key][sub_key]
            with suppress(KeyError):
                if sub_key in self._db_state[key]:
                    del self._db_state[key][sub_key]
        else:
            if key in self._db_state:
                await collection.delete_one({"_id": key})
            self._cache.pop(key, None)
            if key in self._db_state:
                del self._db_state[key]


class TempDownload:
    """ContextManager for saving a file and removing it after use"""
    def __init__(self, filename: str, url: str):
        self.filename = filename
        self.url = url
        temp_dir = Path(".temp")
        temp_dir.mkdir(exist_ok=True)
        files = {path.name for path in temp_dir.iterdir()}
        if filename:
            stem, suffix = os.path.splitext(filename)
            candidate = filename
            counter = 1
            while candidate in files:
                candidate = f"{stem}-{counter}{suffix}"
                counter += 1
            self.filename = candidate
            self.fp = str(temp_dir.resolve() / candidate)

    async def __aenter__(self):
        if not self.filename:
            return None
        async with aiohttp.ClientSession() as sess:
            async with sess.get(self.url) as resp:
                resp.raise_for_status()
                raw_dat = await resp.read()
        async with aiofiles.open(self.fp, "wb") as f:
            await f.write(raw_dat)
        return self.fp

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb):
        if not self.filename:
            return None
        if os.path.isfile(self.fp):
            os.remove(self.fp)


class _CacheWriter:
    def __init__(self, cache, filepath):
        self.cache = cache
        self.filepath = filepath

    async def write(self, *args, **kwargs):
        await self.cache.write(self.filepath, *args, **kwargs)


class FileCache:
    def __init__(self, bot):
        self.bot = bot
        self.data = {}  # Filepath: {"args": list, "kwargs": dict}
        self._closed = False
        self.dump_task.start()

    def __del__(self):
        self.dump_task.stop()

    @tasks.loop(minutes=15)
    async def dump_task(self):
        await self.flush()

    async def flush(self):
        for filepath, data in list(self.data.items()):
            args = data["args"]
            kwargs = data["kwargs"]
            async with self.bot.utils.open(filepath, "w+") as f:
                await f.write(*args, **kwargs)
            if self.data.get(filepath) is data:
                del self.data[filepath]
            self.bot.log.debug(f"Wrote {filepath} from cache")

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self.dump_task.cancel()
        task = self.dump_task.get_task()
        if task:
            with suppress(asyncio.CancelledError):
                await task
        await self.flush()

    async def write(self, filepath, *args, **kwargs):
        self.data[filepath] = {
            "args": args,
            "kwargs": kwargs
        }


class AsyncFileManager:
    def __init__(self, bot, file: str, mode: str = "r", lock: bool = True, cache=False):
        self.bot = bot
        self.file = self.temp_file = file
        if "w" in mode:
            self.temp_file += ".tmp"
        self.mode = mode
        self.fp_manager = None
        self.lock = lock if not cache else False
        self.cache = cache
        self._file_lock = None
        if lock and not self.cache:
            self._file_lock = self.bot.file_locks.get(file)
            if self._file_lock is None:
                self._file_lock = asyncio.Lock()
                self.bot.file_locks[file] = self._file_lock
        self.writer = None

    async def __aenter__(self):
        if self.cache:
            self.writer = _CacheWriter(self.bot.cache, self.file)
            return self.writer
        if self.lock:
            await self._file_lock.acquire()
        try:
            self.fp_manager = await aiofiles.open(
                file=self.temp_file, mode=self.mode
            )
        except BaseException:
            if self.lock:
                self._file_lock.release()
            raise
        return self.fp_manager

    async def __aexit__(self, _exc_type, _exc_value, _exc_traceback):
        if self.cache:
            del self.writer
            return None
        try:
            await self.fp_manager.close()
            if self.file != self.temp_file and _exc_type is None:
                await _replace_file(self.temp_file, self.file)
            elif self.file != self.temp_file:
                with suppress(FileNotFoundError):
                    os.remove(self.temp_file)
        finally:
            if self.lock:
                self._file_lock.release()
        return None


async def delete_in_batches(bot, query, args=None):
    """Run a DELETE with LIMIT 5000, returning the connection between batches."""
    removed = 0
    while True:
        async with bot.utils.cursor() as cur:
            await cur.execute(query, args)
            affected = cur.rowcount
        removed += affected
        if affected < 5000:
            return removed
        await asyncio.sleep(0.1)


class Cursor:
    def __init__(self, bot, max_retries: int = 10):
        self.bot = bot
        self.conn = None
        self.cursor = None
        self.retries = max_retries

    async def __aenter__(self):
        while not self.bot.pool:
            await self.bot.wait_for_pool()
        for _ in range(self.retries):
            try:
                self.conn = await self.bot.pool.acquire()
            except (pymysql.OperationalError, RuntimeError):
                await asyncio.sleep(1.21)
                continue
            try:
                self.cursor = await self.conn.cursor()
            except BaseException:
                self.bot.pool.release(self.conn)
                self.conn = None
                raise
            break
        else:
            raise pymysql.OperationalError("Can't connect to db")
        return self.cursor

    async def __aexit__(self, _type, _value, _tb):
        with suppress(RuntimeError):
            self.bot.pool.release(self.conn)


async def save_json(bot, fp, data, mode="w+", **json_kwargs) -> None:
    def dump():
        return json.dumps(data, **json_kwargs)

    async with aiofiles.open(fp + ".tmp", mode) as f:
        await f.write(await bot.loop.run_in_executor(None, dump))
    try:
        await _replace_file(fp + ".tmp", fp)
    except FileNotFoundError:
        pass


async def download(url: str, timeout: int = 10):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(str(url), timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
        except (asyncio.TimeoutError, aiohttp.ClientError):
            return None
