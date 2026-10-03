import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from cogs.fun.fun import Fun, _snipe_embed


class SnipeEmbedTests(unittest.TestCase):
    def test_video_preview_without_description_becomes_rich_link(self):
        payload = {
            "type": "video", "url": "https://example.com/watch",
            "video": {"url": "https://example.com/video.mp4", "width": 640},
            "provider": {"name": "Example"}, "flags": 16,
            "thumbnail": {
                "url": "https://example.com/preview.png", "width": 640,
                "height": 480, "proxy_url": "https://proxy.example.com/image",
            },
        }
        original = discord.Embed.from_dict(payload)
        result = _snipe_embed(original).to_dict()

        self.assertEqual(result["type"], "rich")
        self.assertEqual(result["description"], payload["url"])
        self.assertEqual(result["thumbnail"], {"url": payload["thumbnail"]["url"]})
        for key in ("video", "provider"):
            self.assertNotIn(key, result)
        self.assertEqual(original.to_dict()["type"], "video")
        self.assertEqual(original.to_dict()["thumbnail"], payload["thumbnail"])

    def test_rich_embed_preserves_text_fields_and_images(self):
        original = discord.Embed(title="Title", description="Description", color=123)
        original.add_field(name="Field", value="Value")
        original.set_image(url="https://example.com/image.png")
        original.set_author(name="Author")
        original.set_footer(text="Footer")
        self.assertEqual(_snipe_embed(original).to_dict(), original.to_dict())

    def test_missing_text_has_nonempty_fallback(self):
        for payload, expected in (
            ({"type": "video", "video": {"url": "https://example.com/video.mp4"}},
             "https://example.com/video.mp4"),
            ({"type": "rich"}, "This embed had no text."),
            ({"type": "image", "image": {"url": "https://example.com/image.png"}},
             "This embed had no text."),
        ):
            with self.subTest(payload=payload):
                result = _snipe_embed(discord.Embed.from_dict(payload))
                self.assertEqual(result.description, expected)
                self.assertEqual(result.type, "rich")


class SnipeCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_snipe_sends_normalized_preview(self):
        cog = object.__new__(Fun)
        cursor = SimpleNamespace(execute=AsyncMock(), rowcount=1)
        context_manager = MagicMock()
        context_manager.__aenter__ = AsyncMock(return_value=cursor)
        context_manager.__aexit__ = AsyncMock(return_value=False)
        cog.bot = SimpleNamespace(utils=SimpleNamespace(cursor=lambda: context_manager))
        message = SimpleNamespace(
            author="User", content="https://example.com/watch",
            embeds=[discord.Embed.from_dict({
                "type": "video", "url": "https://example.com/watch",
                "video": {"url": "https://example.com/video.mp4"},
            })],
        )
        cog.dat = {123: {"last": (message, datetime.now())}}
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=456), channel=SimpleNamespace(id=123),
            message=SimpleNamespace(content=".snipe", mentions=[]),
            author=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False)),
            send=AsyncMock(),
        )

        await Fun.snipe.callback(cog, ctx)

        ctx.send.assert_awaited_once()
        result = ctx.send.await_args.kwargs["embed"]
        self.assertEqual(result.type, "rich")
        self.assertEqual(result.description, "https://example.com/watch")
        self.assertNotIn("video", result.to_dict())


if __name__ == "__main__":
    unittest.main()
