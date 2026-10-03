"""Resource limits apply to streamed and decompressed bodies as they arrive."""

import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord.ext import commands

from botutils.http_resources import read_resource


class Stream:
    def __init__(self, data):
        self.data = BytesIO(data)
        self.requests = []

    async def read(self, size):
        self.requests.append(size)
        return self.data.read(size)


class ResourceMemoryChecks(unittest.IsolatedAsyncioTestCase):
    async def test_exact_limit_and_empty_body_return_original_bytes(self):
        for data in (b"", b"a" * 140_000):
            stream = Stream(data)
            response = SimpleNamespace(content=stream, read=AsyncMock())
            self.assertEqual(await read_resource(response, len(data), "file"), data)
            response.read.assert_not_awaited()
            self.assertLessEqual(max(stream.requests), 65_536)

    async def test_oversized_body_stops_after_limit_plus_one_byte(self):
        # content_length may be missing or may describe compressed bytes.
        for length in (None, 100):
            stream = Stream(b"x" * 2_000_000)
            response = SimpleNamespace(content=stream, content_length=length,
                                       read=AsyncMock())
            with self.assertRaisesRegex(commands.BadArgument, "avatar is too large"):
                await read_resource(response, 140_000, "avatar")
            self.assertEqual(stream.data.tell(), 140_001)
            response.read.assert_not_awaited()

    async def test_explicit_unlimited_download_preserves_existing_behavior(self):
        response = SimpleNamespace(read=AsyncMock(return_value=b"video"))
        self.assertEqual(await read_resource(response, None, "video"), b"video")
        response.read.assert_awaited_once()

    async def test_stream_failure_is_not_returned_as_partial_success(self):
        response = SimpleNamespace(content=SimpleNamespace(read=AsyncMock(
            side_effect=ConnectionError("disconnected")
        )))
        with self.assertRaises(ConnectionError):
            await read_resource(response, 100, "file")


if __name__ == "__main__":
    unittest.main()
