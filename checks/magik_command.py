import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord.ext import commands

from cogs.fun.fun import (
    Fun,
    MAGIK_MAX_WORKERS,
    MAGIK_VIDEO_MAX_SECONDS,
    MagikMediaError,
    _magik_worker_count,
)


class MagikCommandTests(unittest.TestCase):
    def test_worker_pool_stays_small_on_high_core_hosts(self):
        self.assertEqual(_magik_worker_count(20), MAGIK_MAX_WORKERS)
        self.assertEqual(_magik_worker_count(2), 1)


class MagikCommandAsyncTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def video_probe(duration):
        return json.dumps({
            "streams": [{
                "width": 1280,
                "height": 720,
                "duration": str(duration),
                "r_frame_rate": "30/1",
            }],
            "format": {"duration": str(duration)},
        }).encode()

    def make_fun(self, duration):
        fun = object.__new__(Fun)
        fun._ffmpeg = "ffmpeg"
        fun._ffprobe = "ffprobe"
        fun._run_magik_process = AsyncMock(
            return_value=self.video_probe(duration)
        )
        return fun

    async def test_regular_user_video_length_limit_is_preserved(self):
        fun = self.make_fun(MAGIK_VIDEO_MAX_SECONDS + 1)

        with self.assertRaises(MagikMediaError):
            await fun._probe_magik_video(Path("unused"))

    async def test_owner_video_can_bypass_length_limit(self):
        duration = MAGIK_VIDEO_MAX_SECONDS + 120
        fun = self.make_fun(duration)

        actual_duration, fps = await fun._probe_magik_video(
            Path("unused"), enforce_duration_limit=False
        )

        self.assertEqual(actual_duration, duration)
        self.assertEqual(fps, 15)

    async def test_video_jobs_are_serialized(self):
        fun = object.__new__(Fun)
        fun._magik_video_slots = asyncio.Semaphore(1)
        active = 0
        peak = 0

        async def render(data, progress, enforce_duration_limit=True):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return data

        fun._magik_video = render
        results = await asyncio.gather(
            fun._magik_media(b"first", is_image=False),
            fun._magik_media(
                b"second", is_image=False, enforce_duration_limit=False
            ),
        )

        self.assertEqual(peak, 1)
        self.assertEqual(results, [(b"first", "mp4"), (b"second", "mp4")])

    async def test_owner_download_has_no_magik_size_cap(self):
        bot = SimpleNamespace(
            get_resource=AsyncMock(side_effect=commands.BadArgument("bad media"))
        )
        fun = object.__new__(Fun)
        fun.bot = bot

        files, error = await fun._magik_results(
            ["https://example.com/video.mp4"], allow_long_video=True
        )

        self.assertIsNone(files)
        self.assertEqual(error, "bad media")
        self.assertIsNone(bot.get_resource.await_args.kwargs["max_size"])


if __name__ == "__main__":
    unittest.main()
