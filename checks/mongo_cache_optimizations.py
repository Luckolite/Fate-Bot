"""Checks for Mongo cache batching, acknowledgement, and concurrent edits."""

import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pymongo import InsertOne
from pymongo.errors import AutoReconnect, BulkWriteError

from botutils.resources import Cache, MONGO_FLUSH_BATCH_SIZE, load_builtin_cache_snapshots
from botutils.cache_rewrite import Cache as QueryCache


class Collection:
    def __init__(self):
        self.documents = {}
        self.batches = []
        self.hook = None
        self.delete_one = AsyncMock(side_effect=self.delete)

    def find(self, query):
        return []

    async def delete(self, query):
        self.documents.pop(query["_id"], None)

    async def bulk_write(self, operations, ordered):
        self.batches.append(operations)
        if self.hook:
            await self.hook()
        errors = []
        for index, operation in enumerate(operations):
            key = operation._doc["_id"] if isinstance(operation, InsertOne) else operation._filter["_id"]
            if isinstance(operation, InsertOne) and key in self.documents:
                errors.append({"index": index, "code": 11000, "errmsg": "duplicate"})
            else:
                self.documents[key] = deepcopy(operation._doc)
                self.documents[key]["_id"] = key
        if errors:
            raise BulkWriteError({"writeErrors": errors, "writeConcernErrors": []})


class LegacyCacheChecks(unittest.IsolatedAsyncioTestCase):
    def test_hydration_builds_one_independent_baseline_per_collection(self):
        collection = Mock()
        collection.find.return_value = [{"_id": 1, "nested": ["original"]}]
        snapshots = load_builtin_cache_snapshots(SimpleNamespace(mongo={"settings": collection}), {"settings"})
        self.assertEqual(len(snapshots["settings"]), 1)
        current, baseline = snapshots["settings"][0]
        current[1]["nested"].append("changed")
        self.assertEqual(baseline, {1: {"nested": ["original"]}})

    def make_cache(self):
        collection = Collection()
        bot = SimpleNamespace(
            loop=asyncio.get_running_loop(), mongo={"test": collection},
            aio_mongo={"test": collection},
        )
        return Cache(bot, "test"), collection

    async def test_many_documents_use_bounded_batches_and_clean_flush_is_free(self):
        cache, collection = self.make_cache()
        for key in range(300):
            cache[key] = {"value": key}
        await cache.flush()
        self.assertEqual([len(batch) for batch in collection.batches], [128, 128, 44])
        self.assertTrue(all(len(batch) <= MONGO_FLUSH_BATCH_SIZE for batch in collection.batches))
        self.assertEqual(len(collection.documents), 300)
        await cache.flush()
        self.assertEqual(len(collection.batches), 3)
        cache[1]["value"] = 77
        await cache.flush()
        self.assertEqual(len(collection.batches[-1]), 1)
        self.assertEqual(collection.documents[1]["value"], 77)

    async def test_nested_edit_during_write_remains_dirty(self):
        cache, collection = self.make_cache()
        cache[1] = {"nested": {"value": 1}}

        async def mutate():
            cache[1]["nested"]["value"] = 2
        collection.hook = mutate
        await cache.flush()
        self.assertEqual(collection.documents[1]["nested"]["value"], 1)
        self.assertEqual(cache._db_state[1]["nested"]["value"], 1)
        collection.hook = None
        await cache.flush()
        self.assertEqual(collection.documents[1]["nested"]["value"], 2)

    async def test_auto_sync_reschedules_edits_received_during_write(self):
        cache, collection = self.make_cache()
        cache.auto_sync = True

        async def mutate():
            cache[1] = {"value": 2}
            collection.hook = None

        collection.hook = mutate
        release = asyncio.Event()
        sleeps = 0

        async def delay(seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                await release.wait()

        with patch("botutils.resources.asyncio.sleep", delay):
            cache[1] = {"value": 1}
            first = cache.task
            await first
            self.assertIsNotNone(cache.task)
            self.assertIsNot(first, cache.task)
            release.set()
            await cache.task
        self.assertIsNone(cache.task)
        self.assertEqual(collection.documents[1]["value"], 2)
        self.assertEqual(len(collection.batches), 2)

    async def test_auto_sync_failure_keeps_dirty_data_without_retry_loop(self):
        cache, collection = self.make_cache()
        cache.auto_sync = True
        collection.bulk_write = AsyncMock(side_effect=AutoReconnect("disconnected"))
        with patch("botutils.resources.asyncio.sleep", AsyncMock()):
            cache[1] = {"value": 1}
            with self.assertRaises(AutoReconnect):
                await cache.task
        self.assertIsNone(cache.task)
        self.assertEqual(cache._db_state, {})
        self.assertEqual(cache[1], {"value": 1})

    async def test_duplicate_insert_keeps_existing_document_and_other_writes(self):
        cache, collection = self.make_cache()
        collection.documents[1] = {"_id": 1, "value": "external"}
        cache[1] = {"value": "local"}
        cache[2] = {"value": "second"}
        await cache.flush()
        self.assertEqual(collection.documents[1]["value"], "external")
        self.assertEqual(collection.documents[2]["value"], "second")
        self.assertEqual(cache._db_state, {1: {"value": "local"}, 2: {"value": "second"}})

    async def test_partial_failure_acknowledges_only_successful_documents(self):
        cache, collection = self.make_cache()
        cache[1], cache[2] = {"value": 1}, {"value": 2}
        collection.bulk_write = AsyncMock(side_effect=BulkWriteError({
            "writeErrors": [{"index": 1, "code": 121, "errmsg": "validation"}],
            "writeConcernErrors": [],
        }))
        with self.assertRaises(BulkWriteError):
            await cache.flush()
        self.assertEqual(cache._db_state, {1: {"value": 1}})
        collection.bulk_write = AsyncMock()
        await cache.flush()
        self.assertEqual(len(collection.bulk_write.call_args.args[0]), 1)
        self.assertEqual(cache._db_state[2], {"value": 2})

    async def test_unacknowledged_or_disconnected_batch_stays_dirty(self):
        for error in (AutoReconnect("disconnected"), BulkWriteError({
            "writeErrors": [], "writeConcernErrors": [{"code": 64, "errmsg": "timeout"}],
        })):
            cache, collection = self.make_cache()
            cache[1] = {"value": 1}
            collection.bulk_write = AsyncMock(side_effect=error)
            with self.assertRaises(type(error)):
                await cache.flush()
            self.assertEqual(cache._db_state, {})

    async def test_remove_waits_for_inflight_insert_then_deletes_it(self):
        cache, collection = self.make_cache()
        cache[1] = {"value": 1}
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait():
            entered.set()
            await release.wait()
        collection.hook = wait
        flushing = asyncio.create_task(cache.flush())
        await entered.wait()
        removing = cache.remove(1)
        await asyncio.sleep(0)
        self.assertFalse(removing.done())
        release.set()
        await asyncio.gather(flushing, removing)
        self.assertNotIn(1, cache)
        self.assertNotIn(1, cache._db_state)
        self.assertNotIn(1, collection.documents)

    async def test_remove_unpersisted_document_needs_no_database_delete(self):
        cache, collection = self.make_cache()
        cache[1] = {"value": 1}
        await cache.remove(1)
        collection.delete_one.assert_not_awaited()
        self.assertNotIn(1, cache)


class QueryCacheChecks(unittest.IsolatedAsyncioTestCase):
    def make_cache(self):
        collection = Collection()
        bot = SimpleNamespace(loop=asyncio.get_running_loop(), aio_mongo={"test": collection})
        return QueryCache(bot, "test"), collection

    async def test_key_iteration_requests_only_document_ids(self):
        cache, collection = self.make_cache()

        async def documents():
            yield {"_id": 1}
            yield {"_id": "two"}
        collection.find = Mock(return_value=documents())
        self.assertEqual([key async for key in cache.keys()], [1, "two"])
        collection.find.assert_called_once_with({}, {"_id": 1})

    async def test_pending_documents_use_bounded_batches(self):
        cache, collection = self.make_cache()
        cache.changes = {key: {"value": key} for key in range(300)}
        await cache.flush()
        self.assertEqual([len(batch) for batch in collection.batches], [128, 128, 44])
        self.assertEqual(cache.changes, {})
        self.assertEqual(len(collection.documents), 300)

    async def test_newer_edit_during_batch_is_flushed_after_snapshot(self):
        cache, collection = self.make_cache()
        cache.changes[1] = {"nested": {"value": 1}}

        async def mutate():
            cache.changes[1] = {"nested": {"value": 2}}
            collection.hook = None
        collection.hook = mutate
        await cache.flush()
        self.assertEqual(len(collection.batches), 2)
        self.assertEqual(collection.batches[0][0]._doc["nested"]["value"], 1)
        self.assertEqual(collection.documents[1]["nested"]["value"], 2)
        self.assertEqual(cache.changes, {})

    async def test_failed_batch_remains_pending_for_retry(self):
        cache, collection = self.make_cache()
        cache.changes[1] = {"value": 1}

        async def fail():
            raise AutoReconnect("uncertain write")
        collection.hook = fail
        with self.assertRaises(AutoReconnect):
            await cache.flush()
        self.assertEqual(cache.changes, {1: {"value": 1}})
        collection.hook = None
        await cache.flush()
        self.assertEqual(cache.changes, {})
        self.assertEqual(collection.documents[1]["value"], 1)

    async def test_read_only_context_does_not_write(self):
        cache, collection = self.make_cache()
        collection.find_one = AsyncMock(return_value={"_id": 1, "nested": {"value": 1}})
        async with cache[1] as config:
            self.assertEqual(config["nested"]["value"], 1)
        self.assertEqual(collection.batches, [])
        collection.find_one.assert_awaited_once()

    async def test_save_uses_acknowledged_snapshot_without_readback(self):
        cache, collection = self.make_cache()
        collection.find_one = AsyncMock(return_value={"_id": 1, "nested": {"value": 1}})
        config = await cache[1]
        config["nested"]["value"] = 2
        await config.save()
        self.assertEqual(config.copy, {"nested": {"value": 2}})
        collection.find_one.assert_awaited_once()
        await config.save(manual=False)
        self.assertEqual(len(collection.batches), 1)
        # Explicit saves retain their force-write behavior.
        await config.save()
        self.assertEqual(len(collection.batches), 2)

    async def test_edit_during_save_is_not_marked_persisted(self):
        cache, collection = self.make_cache()
        collection.find_one = AsyncMock(return_value={"_id": 1, "nested": {"value": 1}})
        config = await cache[1]
        config["nested"]["value"] = 2

        async def mutate():
            config["nested"]["value"] = 3
            collection.hook = None
        collection.hook = mutate
        await config.save()
        self.assertEqual(config.copy, {"nested": {"value": 2}})
        await config.save(manual=False)
        self.assertEqual(collection.documents[1]["nested"]["value"], 3)

    async def test_failed_save_does_not_advance_baseline(self):
        cache, collection = self.make_cache()
        collection.find_one = AsyncMock(return_value={"_id": 1, "value": 1})
        config = await cache[1]
        config["value"] = 2

        async def fail():
            raise AutoReconnect("disconnected")
        collection.hook = fail
        with self.assertRaises(AutoReconnect):
            await config.save()
        self.assertEqual(config.copy, {"value": 1})
        collection.hook = None
        await config.save(manual=False)
        self.assertEqual(collection.documents[1]["value"], 2)


if __name__ == "__main__":
    unittest.main()
