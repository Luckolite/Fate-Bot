"""
cogs.fun.fun
~~~~~~~~~~~~~

A cog containing generally fun commands

:copyright: (C) 2019-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import json
import math
import os
import random
import shutil
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor
from contextlib import suppress
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import List, Optional

import discord
from PIL import Image, ImageDraw, ImageFont, ImageOps
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks

from botutils import get_prefix, format_date, colors, sanitize, get_images
from fate import Fate

try:
    from wand.image import Image as WandImage
except ImportError:
    WandImage = None


MAGIK_VIDEO_MAX_SECONDS = 30
MAGIK_VIDEO_FPS = 15
MAGIK_VIDEO_SIZE = 384
SLIME_VIDEO_FPS = 18
SLIME_VIDEO_FRAMES = 58
SLIME_VIDEO_SIZE = (512, 288)


class MagikMediaError(Exception):
    """A media-processing error that is safe to show to a command user."""


tier_damage = {
    "infinite": 69420,
    "high": [41, 75],
    "medium": [21, 40],
    "low": [6, 20],
    "light": [1, 5],
    None: 0
}


def _remove_vertical_seams(pixels, amount):
    """Content-aware shrinking used when ImageMagick is unavailable."""
    import numpy as np

    for _ in range(min(amount, pixels.shape[1] - 2)):
        rgb = pixels[:, :, :3].astype(np.float32)
        gray = rgb.mean(axis=2)
        energy = np.abs(np.gradient(gray, axis=0)) + np.abs(
            np.gradient(gray, axis=1)
        )
        costs = energy.copy()
        paths = np.zeros(costs.shape, dtype=np.int8)
        infinity = np.float32(np.inf)

        for row in range(1, costs.shape[0]):
            previous = costs[row - 1]
            choices = np.stack((
                np.pad(previous[:-1], (1, 0), constant_values=infinity),
                previous,
                np.pad(previous[1:], (0, 1), constant_values=infinity),
            ))
            direction = choices.argmin(axis=0)
            paths[row] = direction - 1
            costs[row] += choices[direction, np.arange(costs.shape[1])]

        seam = np.empty(costs.shape[0], dtype=np.int32)
        seam[-1] = costs[-1].argmin()
        for row in range(costs.shape[0] - 1, 0, -1):
            seam[row - 1] = seam[row] + paths[row, seam[row]]

        keep = np.ones(costs.shape, dtype=bool)
        keep[np.arange(costs.shape[0]), seam] = False
        pixels = pixels[keep].reshape(
            pixels.shape[0], pixels.shape[1] - 1, pixels.shape[2]
        )
    return pixels


def _pillow_magik(data: bytes) -> bytes:
    """Approximate NotSoBot's liquid-rescale sequence without ImageMagick."""
    import numpy as np

    with Image.open(BytesIO(data)) as source:
        if source.width < 3 or source.height < 3:
            raise ValueError("Image is too small")
        if source.width * source.height > 16_000_000:
            raise ValueError("Image has too many pixels")
        image = ImageOps.exif_transpose(source).convert("CMYK").convert("RGBA")
        image.thumbnail((384, 384), Image.Resampling.LANCZOS)
        pixels = np.asarray(image)

    target_width = max(8, int(pixels.shape[1] * 0.5))
    target_height = max(8, int(pixels.shape[0] * 0.5))
    pixels = _remove_vertical_seams(pixels, pixels.shape[1] - target_width)
    pixels = _remove_vertical_seams(
        pixels.transpose(1, 0, 2), pixels.shape[0] - target_height
    ).transpose(1, 0, 2)

    output_size = (
        max(2, int(pixels.shape[1] * 1.5)),
        max(2, int(pixels.shape[0] * 1.5)),
    )
    distorted = Image.fromarray(pixels, "RGBA").resize(
        output_size, Image.Resampling.BICUBIC
    )
    output = BytesIO()
    distorted.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _magik_image(data: bytes) -> bytes:
    """Apply NotSoBot's default Magik liquid-rescale sequence."""
    # Processing sequence adapted from NotSoSuper and TrustyJAID's MIT-licensed
    # NotSoBot rewrite: github.com/TrustyJAID/Trusty-cogs/tree/master/notsobot
    if WandImage:
        try:
            with WandImage(blob=data) as source:
                if source.width < 3 or source.height < 3:
                    raise ValueError("Image is too small")
                if source.width * source.height > 16_000_000:
                    raise ValueError("Image has too many pixels")
                source.auto_orient()
                with WandImage(image=source.sequence[0]) as image:
                    image.transform_colorspace("cmyk")
                    image.format = "png"
                    image.transform(resize="800x800")
                    image.liquid_rescale(
                        max(2, int(image.width * 0.5)),
                        max(2, int(image.height * 0.5)),
                        delta_x=1,
                        rigidity=0,
                    )
                    image.liquid_rescale(
                        max(2, int(image.width * 1.5)),
                        max(2, int(image.height * 1.5)),
                        delta_x=2,
                        rigidity=0,
                    )
                    return image.make_blob()
        except Exception:
            return _pillow_magik(data)
    return _pillow_magik(data)


def _is_supported_image(data: bytes) -> bool:
    """Identify formats Pillow can decode without loading all image pixels."""
    try:
        with Image.open(BytesIO(data)) as image:
            return image.format is not None
    except Exception:
        return False


def _magik_frame_file(path: str) -> str:
    """Distort one extracted video frame in a worker process."""
    frame = Path(path)
    frame.write_bytes(_magik_image(frame.read_bytes()))
    return path


def _slime_avatar(data: bytes, size: int = 128) -> Image.Image:
    """Return a circular, bordered avatar suitable for the slime animation."""
    with Image.open(BytesIO(data)) as source:
        if source.width < 2 or source.height < 2:
            raise ValueError("Avatar is too small")
        if source.width * source.height > 16_000_000:
            raise ValueError("Avatar has too many pixels")
        avatar = ImageOps.fit(
            ImageOps.exif_transpose(source).convert("RGBA"),
            (size, size),
            method=Image.Resampling.LANCZOS,
        )

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((1, 1, size - 2, size - 2), fill=255)
    bordered = Image.new("RGBA", (size + 10, size + 10), (0, 0, 0, 0))
    border = Image.new("RGBA", bordered.size, (0, 0, 0, 0))
    ImageDraw.Draw(border).ellipse(
        (1, 1, bordered.width - 2, bordered.height - 2),
        fill=(103, 232, 126, 255),
        outline=(20, 63, 38, 255),
        width=4,
    )
    bordered.alpha_composite(border)
    bordered.paste(avatar, (5, 5), mask)
    return bordered


def _draw_slime_car(draw: ImageDraw.ImageDraw, x: int, ground: int, bounce: int) -> None:
    """Draw the car over the avatar without requiring a bundled media asset."""
    y = ground - 88 + bounce
    draw.rounded_rectangle(
        (x + 18, y + 30, x + 224, y + 77),
        radius=18,
        fill=(220, 45, 58),
        outline=(74, 20, 29),
        width=4,
    )
    draw.polygon(
        ((x + 65, y + 32), (x + 100, y + 2), (x + 173, y + 2), (x + 204, y + 32)),
        fill=(197, 35, 50),
        outline=(74, 20, 29),
    )
    draw.polygon(
        ((x + 103, y + 8), (x + 132, y + 8), (x + 132, y + 29), (x + 80, y + 29)),
        fill=(119, 205, 235),
    )
    draw.polygon(
        ((x + 139, y + 8), (x + 169, y + 8), (x + 192, y + 29), (x + 139, y + 29)),
        fill=(91, 176, 213),
    )
    draw.rounded_rectangle(
        (x + 204, y + 42, x + 233, y + 61),
        radius=6,
        fill=(255, 230, 121),
    )
    draw.rectangle((x + 14, y + 48, x + 28, y + 62), fill=(255, 100, 82))
    for wheel_x in (x + 66, x + 188):
        draw.ellipse(
            (wheel_x - 24, y + 57, wheel_x + 24, y + 105),
            fill=(24, 25, 31),
            outline=(7, 8, 10),
            width=3,
        )
        draw.ellipse(
            (wheel_x - 10, y + 71, wheel_x + 10, y + 91),
            fill=(145, 150, 158),
        )


def _render_slime_animation(data: bytes, directory: str) -> int:
    """Render PNG frames and a GIF fallback for the `.slime` command."""
    width, height = SLIME_VIDEO_SIZE
    frame_dir = Path(directory)
    avatar = _slime_avatar(data)
    ground = 244
    avatar_center = 362
    normal_width, normal_height = avatar.size
    frames = []
    splat_font = ImageFont.load_default(size=38)

    for index in range(SLIME_VIDEO_FRAMES):
        seconds = index / SLIME_VIDEO_FPS
        frame = Image.new("RGB", (width, height), (144, 211, 245))
        draw = ImageDraw.Draw(frame)

        # Skyline and road give the short animation depth while remaining cheap
        # enough to render for every command invocation.
        draw.rectangle((0, 174, width, ground), fill=(87, 153, 86))
        draw.rectangle((0, ground, width, height), fill=(50, 53, 61))
        for building_x, building_w, building_h, color in (
            (18, 58, 90, (205, 188, 166)),
            (92, 72, 118, (189, 205, 211)),
            (181, 54, 78, (226, 190, 151)),
            (254, 82, 104, (182, 190, 216)),
            (354, 66, 84, (215, 201, 177)),
            (438, 56, 112, (187, 211, 192)),
        ):
            top = 174 - building_h
            draw.rectangle((building_x, top, building_x + building_w, 174), fill=color)
            for window_y in range(top + 12, 165, 22):
                for window_x in range(building_x + 10, building_x + building_w - 8, 18):
                    draw.rectangle(
                        (window_x, window_y, window_x + 7, window_y + 9),
                        fill=(255, 235, 153),
                    )
        draw.rectangle((0, ground - 5, width, ground), fill=(220, 220, 212))
        stripe_offset = int(seconds * 170) % 86
        for stripe_x in range(-90 + stripe_offset, width + 90, 86):
            draw.rounded_rectangle(
                (stripe_x, ground + 20, stripe_x + 48, ground + 26),
                radius=3,
                fill=(242, 210, 70),
            )

        if seconds < 1.02:
            squash = 0.0
        elif seconds < 1.18:
            squash = (seconds - 1.02) / 0.16
        elif seconds < 2.12:
            squash = 1.0
        elif seconds < 2.82:
            recovery = (seconds - 2.12) / 0.70
            squash = max(0.0, 1.0 - recovery)
        else:
            squash = 0.0

        avatar_width = round(normal_width * (1.0 + 0.62 * squash))
        avatar_height = max(18, round(normal_height * (1.0 - 0.84 * squash)))
        idle_bounce = 0 if squash else round(math.sin(seconds * 8) * 3)
        avatar_frame = avatar.resize(
            (avatar_width, avatar_height), Image.Resampling.LANCZOS
        )
        avatar_x = avatar_center - avatar_width // 2
        avatar_y = ground - avatar_height + idle_bounce
        frame.paste(avatar_frame, (avatar_x, avatar_y), avatar_frame)

        if 1.02 <= seconds <= 1.55:
            burst = max(0.0, 1.0 - abs(seconds - 1.20) / 0.35)
            radius = round(15 + 34 * burst)
            for angle in range(0, 360, 45):
                radians = math.radians(angle)
                dot_x = avatar_center + round(math.cos(radians) * radius)
                dot_y = ground - 20 + round(math.sin(radians) * radius * 0.45)
                dot_size = 3 + round(5 * burst)
                draw.ellipse(
                    (dot_x - dot_size, dot_y - dot_size, dot_x + dot_size, dot_y + dot_size),
                    fill=(103, 232, 126),
                    outline=(20, 63, 38),
                )
            draw.text(
                (avatar_center, 35),
                "SPLAT!",
                font=splat_font,
                fill=(255, 246, 92),
                stroke_width=4,
                stroke_fill=(83, 24, 28),
                anchor="mm",
            )

        car_progress = min(1.0, max(0.0, (seconds - 0.20) / 2.35))
        car_progress = car_progress * car_progress * (3 - 2 * car_progress)
        car_x = round(-270 + car_progress * 850)
        car_bounce = round(math.sin(seconds * 25) * 2)
        for trail in range(3):
            trail_x = car_x - 25 - trail * 30
            draw.line(
                (trail_x, ground - 54 - trail * 11, trail_x - 35, ground - 54 - trail * 11),
                fill=(235, 244, 247),
                width=4,
            )
        _draw_slime_car(draw, car_x, ground, car_bounce)

        frame_path = frame_dir / f"{index:04d}.png"
        frame.save(frame_path, format="PNG", optimize=True)
        frames.append(frame)

    frames[0].save(
        frame_dir / "slime.gif",
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=round(1000 / SLIME_VIDEO_FPS),
        loop=0,
        disposal=2,
        optimize=False,
    )
    return len(frames)


class Personalities:
    types: List[str] = [
        "psychopath", "depressed", "cheerful", "bright", "dark", "god", "deceiver", "funny", "fishy", "cool",
        "insecure", "lonely", "optimistic", "brave", "brilliant", "dreamer", "Nurturer", "Peaceful", "Overthinker",
        "Idealist", "Pussy", "Pick-me girl", "Lovable", "Edgy"
    ]
    statuses: List[str] = [
        "Ho", "Slut", "Loser", "The nice guy", "The dick", "Dank memer", "Annoying", "Parties hard", "Cool guy",
        "The chad", "Popular", "Unpopular", "Shut-in", "You need to leave the house to have a social status", "Eternal Virgin"
    ]
    hobbies: List[str] = [
        "Art", "Drawing", "Painting", "Singing", "Writing", "Anime", "Memes", "Minecraft", "Sucking dick",
        "Gaming", "Programming", "Work", "Swimming","Crying"
    ]
    genres: List[str] = [
        "Nightcore", "Heavy Metal", "Alternative", "Electronic", "Classical", "Dubstep", "Jazz", "Pop", "Rap"
    ]


class Fun(
    commands.Cog,
    command_attrs=dict(
        cooldown=commands.CooldownMapping(commands.Cooldown(2, 5), commands.BucketType.channel)
    )
):
    magik_usage = (
        "`.magik` with up to four attached or replied-to images/videos\n"
        "`.magik @user @user` for avatars\n"
        "`.magik media_url media_url` for direct links"
    )

    def __init__(self, bot: Fate):
        self.bot = bot
        self.dat = {}
        self.bullying = []
        self.gay = bot.utils.cache("sexuality")
        for sexuality in self.sexuality.aliases:
            if sexuality not in self.gay:
                self.gay[sexuality] = {}

        # Data for .fight
        with open("./data/moves.json", encoding="utf-8") as file:
            dat = json.load(file)  # type: dict
        self.attacks = dat["attacks"]
        self.dodges = dat["dodges"]

        # Data for .personality
        with open("./data/personalities.json", encoding="utf-8") as file:
            self.personalities = json.load(file)  # type: dict

        # Liquid rescaling is CPU-heavy and can hold the GIL in its Pillow
        # fallback. Processes keep that work away from Discord's event loop;
        # retaining two CPUs keeps the loop and the rest of the bot responsive.
        cpu_count = os.cpu_count() or 2
        self._magik_executor = ProcessPoolExecutor(
            max_workers=max(1, cpu_count - 2)
        )
        self._ffmpeg = shutil.which("ffmpeg")
        self._ffprobe = shutil.which("ffprobe")
        self.clear_old_messages_task.start()

    async def cog_unload(self):
        self.clear_old_messages_task.stop()
        self._magik_executor.shutdown(wait=False, cancel_futures=True)

    async def cog_check(self, ctx):
        for user in ctx.message.mentions:
            if not await self.bot.get_privacy(user, "fun_commands"):
                raise commands.CheckFailure(f"{user} has fun commands disabled in {ctx.prefix}privacy")
        return True

    def store_snipe(self, message: discord.Message) -> None:
        deleted = (message, datetime.now())
        channel = self.dat.setdefault(message.channel.id, {})
        channel["last"] = deleted
        channel[message.author.id] = deleted

    # @slash_command(name="snipe", guild_ids=[397415086295089155])
    # async def _snipe(
    #     self, ctx,
    #     user: Option(discord.User, required=False),
    #     new_toggle: Option(
    #         str, "Toggle",
    #         required=False,
    #         choices=["Enable", "Disable"]
    #     )
    # ):
    #     """ Enables, or disables sniping """
    #     guild_id = ctx.guild.id
    #     async with self.bot.utils.cursor() as cur:
    #         if new_toggle:
    #             if not ctx.author.guild_permissions.administrator:
    #                 return await ctx.send("Only administrators can enable this")
    #             await cur.execute(f"select * from snipe where guild_id = {guild_id};")
    #             if cur.rowcount:
    #                 if new_toggle == "Enable":
    #                     return await ctx.send("Sniping is already enabled")
    #                 await cur.execute(f"delete from snipe where guild_id = {guild_id};")
    #                 await ctx.send("Disabled sniping", ephemeral=True)
    #             else:
    #                 if new_toggle == "Disable":
    #                     return await ctx.send("Sniping isn't enabled")
    #                 await cur.execute(f"insert into snipe values ({guild_id});")
    #                 await ctx.respond("Enabled sniping", ephemeral=True)
    #             return
#
    #         await cur.execute(f"select * from snipe where guild_id = {guild_id};")
    #         if not cur.rowcount:
    #             return await ctx.respond(
    #                 f"Snipe requires being enabled by an administrator. Use `{ctx.prefix}snipe enable`",
    #                 ephemeral=True
    #             )
#
    #         channel_id = ctx.channel.id
    #         if channel_id not in self.dat:
    #             return await ctx.respond("Nothing to snipe", ephemeral=True)
#
    #         # Snipe a specific user
    #         if user:
    #             if user.id not in self.dat[channel_id]:
    #                 return await ctx.respond("Nothing to snipe", ephemeral=True)
    #             msg, time = self.dat[channel_id][user.id]
    #             del self.dat[channel_id][user.id]
#
    #         # Snipe the last message in the channel
    #         else:
    #             msg, time = self.dat[channel_id]["last"]
    #             del self.dat[channel_id]
#
    #         is_admin = ctx.author.guild_permissions.administrator
    #         if msg.embeds:
    #             return await ctx.respond(f"{msg.author}s message was | deleted {format_date(time)} ago",
    #                                   embed=msg.embeds[0])
    #         if len(msg.content) > 256 and not is_admin:
    #             return await ctx.respond("And **wHy** would I snipe a message *that*  big", ephemeral=True)
#
    #         e = discord.Embed(color=msg.author.color)
    #         e.set_author(name=msg.author, icon_url=msg.author.display_avatar.url)
    #         if not is_admin:
    #             msg.content = await sanitize(msg.content[:4096], ctx)
    #         e.description = msg.content
    #         e.set_footer(text=f"🗑 {format_date(time).replace('.0', '')} ago")
    #         await ctx.respond(embed=e, ephemeral=True)

    @commands.command(name="snipe", description="Shows the last deleted message")
    @commands.cooldown(2, 10, commands.BucketType.channel)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def snipe(self, ctx):
        guild_id = ctx.guild.id
        content = ctx.message.content if hasattr(ctx, "message") else ""
        async with self.bot.utils.cursor() as cur:
            if "snipe enable" in content or "snipe disable" in content:
                if not ctx.author.guild_permissions.administrator:
                    return await ctx.send("Only administrators can enable this")
                await cur.execute(
                    "select * from snipe where guild_id = %s;", (guild_id,)
                )
                if cur.rowcount:
                    if "enable" in content:
                        return await ctx.send("Sniping is already enabled")
                    await cur.execute(
                        "delete from snipe where guild_id = %s;", (guild_id,)
                    )
                    await ctx.send("Disabled sniping")
                else:
                    if "disable" in content:
                        return await ctx.send("Sniping isn't enabled")
                    await cur.execute(
                        "insert into snipe values (%s);", (guild_id,)
                    )
                    await ctx.send("Enabled sniping")
                return

            await cur.execute(
                "select * from snipe where guild_id = %s;", (guild_id,)
            )
            if not cur.rowcount:
                return await ctx.send(
                    f"Snipe requires being enabled by an administrator. Use `{ctx.prefix}snipe enable`"
                )

        channel_id = ctx.channel.id
        if channel_id not in self.dat:
            return await ctx.send("Nothing to snipe")

        # Snipe a specific user
        if ctx.message.mentions:
            user_id = ctx.message.mentions[0].id
            if user_id not in self.dat[channel_id]:
                return await ctx.send("Nothing to snipe")
            msg, time = self.dat[channel_id][user_id]
            del self.dat[channel_id][user_id]

        # Snipe the last message in the channel
        else:
            msg, time = self.dat[channel_id]["last"]
            del self.dat[channel_id]

        is_admin = ctx.author.guild_permissions.administrator
        if msg.embeds:
            return await ctx.send(f"{msg.author}s message was | deleted {format_date(time)} ago", embed=msg.embeds[0])
        if len(msg.content) > 256 and not is_admin:
            return await ctx.send("And **wHy** would I snipe a message *that*  big")

        e = discord.Embed(color=msg.author.color)
        e.set_author(name=msg.author, icon_url=msg.author.display_avatar.url)
        if not is_admin:
            msg.content = await sanitize(msg.content[:4096], ctx)
        e.description = msg.content
        e.set_footer(text=f"🗑 {format_date(time).replace('.0', '')} ago")
        await ctx.send(embed=e)

    @commands.Cog.listener()
    async def on_message_delete(self, m: discord.Message):
        if not m.guild or not (m.content or m.embeds):
            return
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select * from snipe where guild_id = %s;", (m.guild.id,)
            )
            if cur.rowcount:
                self.store_snipe(m)

    @tasks.loop(minutes=25)
    async def clear_old_messages_task(self):
        expiration = datetime.now() - timedelta(hours=1)
        for channel_id, data in list(self.dat.items()):
            if data["last"][1] < expiration:
                del self.dat[channel_id]
                continue
            for key, value in list(data.items()):
                if key != "last":
                    if value[1] < expiration:
                        with suppress(KeyError, ValueError):
                            del self.dat[channel_id][key]

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if after.guild and before.embeds and not after.embeds:
            async with self.bot.utils.cursor() as cur:
                await cur.execute(
                    "select * from snipe where guild_id = %s;", (after.guild.id,)
                )
                if cur.rowcount:
                    self.store_snipe(before)

    @commands.command(name="battle", aliases=["fight"], description="Battles another user")
    @commands.max_concurrency(1, commands.BucketType.user)
    @commands.max_concurrency(2, commands.BucketType.guild)
    @commands.cooldown(2, 5, commands.BucketType.user)
    @commands.guild_only()
    @commands.bot_has_permissions(attach_files=True, embed_links=True)
    async def battle(self, ctx, user1 = None, user2: discord.User = None):
        # Fetch user1 if the authors not getting stats info
        if user1 and user1 != "stats":
            user1 = await self.bot.utils.get_user(ctx, user1)
            if not user1:
                return await ctx.send("User not found")

        # Display battle statistics
        if user1 == "stats":
            user_id = ctx.author.id
            if user2:
                user_id = user2.id

            async with self.bot.utils.cursor() as cur:
                await cur.execute(
                    "select * from battles where winner = %s;", (user_id,)
                )
                wins = cur.rowcount
                await cur.execute(
                    "select * from battles where loser = %s;", (user_id,)
                )
                losses = cur.rowcount

                # Create the basic version of the embed
                e = discord.Embed(color=self.bot.config["theme_color"])
                s = "s" if wins != 1 else ""
                e.description = f"**{wins}** win{s} and **{losses}** losses"

                # Add the authors stats against the user
                if user2:
                    await cur.execute(
                        "select * from battles where winner = %s and loser = %s",
                        (ctx.author.id, user2.id),
                    )
                    wins = cur.rowcount
                    await cur.execute(
                        "select * from battles where winner = %s and loser = %s",
                        (user2.id, ctx.author.id),
                    )
                    losses = cur.rowcount
                    s = "s" if wins != 1 else ""
                    e.description += f". You've won {wins} time{s} against them and lost {losses} times"

                return await ctx.send(embed=e)

        # Create a battle card
        if not user1:
            return await ctx.send("You need to specify who to battle")
        if not user2:
            user2 = user1
            user1 = ctx.author
        large_font = ImageFont.truetype("./botutils/fonts/pdark.ttf", 70)
        W, H = 350, 125
        border_color = "black"
        background_url = "https://cdn.discordapp.com/attachments/632084935506788385/834605220302553108/battle.jpg"
        frame_url = "https://cdn.discordapp.com/attachments/632084935506788385/834609401855213598/1619056781596.png"

        background, frame, av1, av2 = await asyncio.gather(
            self.bot.get_resource(background_url),
            self.bot.get_resource(frame_url),
            self.bot.get_resource(str(user1.display_avatar.url)),
            self.bot.get_resource(str(user2.display_avatar.url)),
        )

        def generate_card(frame, av1, av2):
            card = Image.new("RGBA", (W, H), (0, 0, 0, 100))
            im = Image.open(BytesIO(background)).convert("RGBA").resize((W, H))
            card.paste(im, (0, 0), im)

            draw = ImageDraw.Draw(card)
            left, top, right, bottom = draw.textbbox((0, 0), "VS", font=large_font)
            w, h = right - left, bottom - top
            draw.text(((W - w) / 2, (H - h) / 2), text="VS", fill="white", font=large_font)

            frame = Image.open(BytesIO(frame)).convert("RGBA").resize((70, 70), Image.BICUBIC)
            av1 = Image.open(BytesIO(av1)).convert("RGBA").resize((70, 70), Image.BICUBIC)
            av2 = Image.open(BytesIO(av2)).convert("RGBA").resize((70, 70), Image.BICUBIC)
            av1.paste(frame, (0, 0), frame)
            av2.paste(frame, (0, 0), frame)
            card.paste(av1, (25, 30), av1)
            card.paste(av2, (255, 30), av2)

            draw.line((0, 0, W, 0), border_color, 5)
            draw.line((W, 0, W, H), border_color, 5)
            draw.line((0, 0, 0, H), border_color, 5)
            draw.line((0, H, W, H), border_color, 5)

            mem_file = BytesIO()
            card.save(mem_file, format="PNG")
            mem_file.seek(0)
            return mem_file

        e = discord.Embed(color=discord.Color.red())
        e.title = f"{user1.display_name} Vs. {user2.display_name}"
        mem_file = await self.bot.loop.run_in_executor(
            None, generate_card, frame, av1, av2
        )
        msg = await ctx.send(
            embed=e,
            file=discord.File(mem_file, filename="card.png")
        )

        e.description = ""
        attacks = dict(self.attacks)
        attacks[None] = list(self.dodges)
        health1 = 200
        health2 = 200
        attacker = 1

        last_tier_used = {1: None, 2: None}
        attacks_used = {k: [] for k in self.attacks}
        attacks_used[None] = []

        while True:
            if not msg:
                msg = await ctx.send(embed=e)
            if health1 <= 0:
                async with self.bot.utils.cursor() as cur:
                    await cur.execute(
                        "insert into battles values (%s, %s);", (user2.id, user1.id)
                    )
                await msg.edit(content=f"🏆 **{user2.display_name} won** 🏆")
                return await ctx.send(f"⚔ **{user2.display_name}** won against **{user1.display_name}**")
            if health2 <= 0:
                async with self.bot.utils.cursor() as cur:
                    await cur.execute(
                        "insert into battles values (%s, %s);", (user1.id, user2.id)
                    )
                await msg.edit(content=f"🏆 **{user1.display_name} won** 🏆")
                return await ctx.send(f"⚔ **{user1.display_name}** won against **{user2.display_name}**")

            # Set an attack tier
            choices = [None, "light", "low", "low", "medium", "medium", "high"]
            choices.remove(last_tier_used[attacker])

            tier = random.choice(choices)
            last_tier_used[attacker] = tier

            if random.randint(1, 100) == 16:
                tier = "infinite"

            # Ensure we don't get an attack that was already used
            available_attacks = [
                attack for attack in attacks[tier] if attack not in attacks_used[tier]
            ]
            if not available_attacks:
                attacks_used[tier].clear()
                available_attacks = attacks[tier]
            attack = random.choice(available_attacks)
            attacks_used[tier].append(attack)

            # Subtract the damage from the targets health and format the attack with their names
            dmg = tier_damage[tier]
            if isinstance(dmg, list):
                dmg = random.randint(*dmg)
            if attacker == 1:
                formatted = attack.replace('!user', user1.display_name).replace('!target', user2.display_name)
                health2 -= dmg
            else:
                formatted = attack.replace('!user', user2.display_name).replace('!target', user1.display_name)
                health1 -= dmg

            # Reformatting
            if dmg and tier != "infinite":
                formatted += f" `-{dmg}HP`"
            if health1 < 0:
                health1 = 0
            if health2 < 0:
                health2 = 0
            if tier == "infinite":
                if attacker == 1:
                    health2 = -dmg
                else:
                    health1 = -dmg

            e.description += f"\n{formatted}"
            e.description = e.description[-4000:]
            e.set_footer(text=f"{user1.name} {health1}HP | {user2.name} {health2}HP")
            attacker = 2 if attacker == 1 else 1
            await msg.edit(embed=e)
            await asyncio.sleep(3)

    @commands.command(name="seggs", aliases=["sexdupe"], description="Sends totally legitimate instructions")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def seggs(self, ctx, user: discord.User):
        await ctx.send(f"Sent instructions on the {user.name} sex dupe to dms")
        choices = [
            "There isn't one for *you*",
            "Err.. maybe try being more attractive",
            "Sike! You're nobodys type",
            "eWwW, get out of ma face",
            "I can't dupe your micro penis. zero times 2 is still zero"
        ]
        try:
            await ctx.author.send(random.choice(choices))
        except discord.HTTPException:
            pass

    @staticmethod
    def _message_media_urls(message: discord.Message) -> List[str]:
        urls = [attachment.url for attachment in message.attachments]
        urls.extend(
            embed.image.url
            for embed in message.embeds
            if embed.image and embed.image.url
        )
        return urls

    async def _magik_sources(self, ctx, sources):
        urls = self._message_media_urls(ctx.message)
        invalid = []

        if reference := ctx.message.reference:
            message = reference.cached_message
            if not message:
                with suppress(discord.NotFound, discord.HTTPException):
                    message = await ctx.channel.fetch_message(reference.message_id)
            if message:
                urls.extend(self._message_media_urls(message))

        for source in sources.split() if sources else []:
            if source.startswith(("https://", "http://")):
                urls.append(source)
                continue
            try:
                user = await commands.UserConverter().convert(ctx, source)
            except commands.BadArgument:
                invalid.append(source)
            else:
                urls.append(str(user.display_avatar.with_size(512)))

        if not urls and not sources:
            urls.extend(await get_images(ctx))

        return list(dict.fromkeys(urls)), invalid

    @staticmethod
    async def _run_magik_process(*args, timeout=90):
        """Run an FFmpeg-family process without blocking the event loop."""
        process_kwargs = {}
        if os.name == "nt":
            process_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_kwargs,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise MagikMediaError("Video processing took too long.")
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail or "The media process failed")
        return stdout

    async def _probe_magik_video(self, input_path: Path):
        if not self._ffmpeg or not self._ffprobe:
            raise MagikMediaError(
                "Video magik needs FFmpeg and FFprobe installed on the bot host."
            )
        try:
            output = await self._run_magik_process(
                self._ffprobe,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration,r_frame_rate:format=duration",
                "-of", "json",
                str(input_path),
                timeout=30,
            )
            metadata = json.loads(output)
            stream = metadata["streams"][0]
        except MagikMediaError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, RuntimeError):
            raise MagikMediaError("I couldn't read that as an image or video.")

        duration_value = (
            stream.get("duration")
            or metadata.get("format", {}).get("duration")
        )
        try:
            duration = float(duration_value)
        except (TypeError, ValueError):
            raise MagikMediaError("I couldn't determine that video's duration.")
        if not math.isfinite(duration) or duration <= 0:
            raise MagikMediaError("I couldn't determine that video's duration.")
        if duration > MAGIK_VIDEO_MAX_SECONDS + 0.05:
            raise MagikMediaError(
                f"Videos must be {MAGIK_VIDEO_MAX_SECONDS} seconds or shorter."
            )

        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        if width < 2 or height < 2:
            raise MagikMediaError("That video is too small to distort.")
        if width * height > 16_000_000:
            raise MagikMediaError("That video has too many pixels per frame.")

        try:
            numerator, denominator = stream.get("r_frame_rate", "0/1").split("/", 1)
            source_fps = float(numerator) / float(denominator)
        except (AttributeError, ValueError, ZeroDivisionError):
            source_fps = MAGIK_VIDEO_FPS
        fps = min(MAGIK_VIDEO_FPS, max(1.0, source_fps))
        return duration, fps

    async def _magik_video(self, data: bytes, progress=None):
        """Extract, distort in parallel, and re-encode a short video."""
        if progress:
            progress(1, "Inspecting video")
        with tempfile.TemporaryDirectory(prefix="fate-magik-") as temp_name:
            temp_dir = Path(temp_name)
            input_path = temp_dir / "input-media"
            output_path = temp_dir / "magik.mp4"
            frame_dir = temp_dir / "frames"
            frame_dir.mkdir()
            await asyncio.to_thread(input_path.write_bytes, data)

            duration, fps = await self._probe_magik_video(input_path)
            if progress:
                progress(5, "Extracting frames")
            frame_pattern = str(frame_dir / "%08d.png")
            video_filter = (
                f"fps={fps:.6f},"
                f"scale='min({MAGIK_VIDEO_SIZE},iw)':"
                f"'min({MAGIK_VIDEO_SIZE},ih)':"
                "force_original_aspect_ratio=decrease:force_divisible_by=2"
            )
            try:
                await self._run_magik_process(
                    self._ffmpeg,
                    "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-i", str(input_path),
                    "-t", f"{duration:.6f}",
                    "-vf", video_filter,
                    "-start_number", "0",
                    frame_pattern,
                    timeout=90,
                )
            except RuntimeError as error:
                self.bot.log.warning(
                    f"Magik video decode failed: {str(error)[-2000:]}"
                )
                raise MagikMediaError("I couldn't decode that video.")

            frames = await asyncio.to_thread(
                lambda: sorted(frame_dir.glob("*.png"))
            )
            if not frames:
                raise MagikMediaError("I couldn't find any frames in that video.")

            if progress:
                progress(15, f"Distorting {len(frames)} frames")
            loop = asyncio.get_running_loop()
            frame_jobs = [
                loop.run_in_executor(
                    self._magik_executor, _magik_frame_file, str(frame)
                )
                for frame in frames
            ]
            frame_errors = []
            for completed, job in enumerate(asyncio.as_completed(frame_jobs), start=1):
                try:
                    await job
                except Exception as error:
                    frame_errors.append(error)
                if progress:
                    percent = 15 + round(70 * completed / len(frame_jobs))
                    progress(percent, f"Distorting frames ({completed}/{len(frame_jobs)})")
            if frame_errors:
                self.bot.log.warning(
                    f"Magik video frame distortion failed: {str(frame_errors[0])[-2000:]}"
                )
                raise MagikMediaError("I couldn't distort that video's frames.")

            if progress:
                progress(90, "Encoding video")
            try:
                await self._run_magik_process(
                    self._ffmpeg,
                    "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-framerate", f"{fps:.6f}",
                    "-start_number", "0",
                    "-i", frame_pattern,
                    "-i", str(input_path),
                    "-map", "0:v:0",
                    "-map", "1:a:0?",
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "28",
                    "-maxrate", "1500k",
                    "-bufsize", "3000k",
                    "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    "-t", f"{duration:.6f}",
                    "-movflags", "+faststart",
                    "-map_metadata", "-1",
                    str(output_path),
                    timeout=120,
                )
            except RuntimeError as error:
                self.bot.log.warning(
                    f"Magik video encode failed: {str(error)[-2000:]}"
                )
                raise MagikMediaError("I couldn't encode the distorted video.")
            if progress:
                progress(100, "Complete")
            return await asyncio.to_thread(output_path.read_bytes)

    async def _magik_media(self, data: bytes, is_image=None, progress=None):
        """Dispatch one downloaded item to the image or video pipeline."""
        if is_image is None:
            is_image = await asyncio.to_thread(_is_supported_image, data)
        if is_image:
            loop = asyncio.get_running_loop()
            distorted = await loop.run_in_executor(
                self._magik_executor, _magik_image, data
            )
            return distorted, "png"
        return await self._magik_video(data, progress), "mp4"

    async def _magik_results(self, urls, progress_callback=None):
        """Download and distort media for prefix and application commands."""
        downloads = await asyncio.gather(*(
            self.bot.get_resource(url, label=f"media {index}", timeout=30)
            for index, url in enumerate(urls, start=1)
        ), return_exceptions=True)
        payloads = [data for data in downloads if isinstance(data, bytes)]
        image_flags = await asyncio.gather(*(
            asyncio.to_thread(_is_supported_image, data) for data in payloads
        ))
        video_count = sum(not is_image for is_image in image_flags)
        progress_values = [0] * video_count
        progress_stage = {"text": "Preparing video"}

        def update_video_progress(index, percent, stage):
            progress_values[index] = percent
            progress_stage["text"] = stage

        async def publish_progress(percent=None, stage=None):
            if not progress_callback:
                return
            if percent is None:
                percent = round(sum(progress_values) / max(1, video_count))
            try:
                await progress_callback(
                    percent,
                    stage or progress_stage["text"],
                    video_count,
                )
            except discord.HTTPException:
                pass

        async def progress_reporter():
            while True:
                await asyncio.sleep(5)
                await publish_progress()

        reporter = None
        if video_count and progress_callback:
            await publish_progress()
            reporter = asyncio.create_task(progress_reporter())

        media_jobs = []
        video_index = 0
        for data, is_image in zip(payloads, image_flags):
            item_progress = None
            if not is_image:
                index = video_index
                video_index += 1

                def item_progress(percent, stage, index=index):
                    update_video_progress(index, percent, stage)

            media_jobs.append(self._magik_media(data, is_image, item_progress))

        try:
            processed = await asyncio.gather(
                *media_jobs, return_exceptions=True
            )
        finally:
            if reporter:
                reporter.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await reporter

        media = [result for result in processed if isinstance(result, tuple)]
        video_successes = sum(
            extension == "mp4" for _, extension in media
        )
        if video_count and progress_callback:
            if video_successes == video_count:
                await publish_progress(100, "Complete")
            elif video_successes:
                await publish_progress(100, "Finished with errors")
            else:
                await publish_progress(stage="Failed")
        failed = len(downloads) - len(media)
        errors = [
            str(result) for result in processed
            if isinstance(result, MagikMediaError)
        ]
        if not media:
            return None, errors[0] if errors else (
                "I couldn't process that media. Try a PNG, JPEG, GIF, WebP, "
                f"or a video up to {MAGIK_VIDEO_MAX_SECONDS} seconds long."
            )

        files = [
            discord.File(BytesIO(data), filename=f"magik-{index}.{extension}")
            for index, (data, extension) in enumerate(media, start=1)
        ]
        content = (
            f"Skipped {failed} file{'s' if failed != 1 else ''}."
            if failed else None
        )
        if content and errors:
            content += f" {errors[0]}"
        return files, content

    @staticmethod
    def _magik_progress_content(percent, stage, video_count):
        label = "Magik video" if video_count == 1 else f"Magik videos ({video_count})"
        if stage == "Complete":
            return f"✅ {label}: **100%** — Complete"
        if stage == "Finished with errors":
            return f"⚠️ {label}: **100%** — Finished with errors"
        if stage == "Failed":
            return f"❌ {label} — Failed"
        return f"🎞️ {label}: **{percent}%** — {stage}"

    @commands.command(
        name="magik",
        aliases=["magic", "magick", "imagemagic", "imagemagick", "liquid"],
        description="Funnily distorts up to four images or videos",
    )
    @commands.cooldown(1, 6, commands.BucketType.user)
    @commands.max_concurrency(2, commands.BucketType.guild)
    @commands.bot_has_permissions(attach_files=True)
    async def magik(self, ctx, *, sources: str = None):
        """Apply liquid-rescale distortion to images, videos, or avatars."""
        urls, invalid = await self._magik_sources(ctx, sources)
        if invalid:
            return await ctx.send(
                f"I couldn't find media or a user for: `{', '.join(invalid)}`"
            )
        if not urls:
            return
        if len(urls) > 4:
            return await ctx.send("You can magik up to four media files at once.")

        progress_message = None

        async def update_progress(percent, stage, video_count):
            nonlocal progress_message
            progress_content = self._magik_progress_content(
                percent, stage, video_count
            )
            if progress_message is None:
                progress_message = await ctx.send(progress_content)
            else:
                await progress_message.edit(content=progress_content)

        async with ctx.typing():
            files, content = await self._magik_results(urls, update_progress)
        if files is None:
            return await ctx.send(content)
        await ctx.send(content, files=files)

    @app_commands.command(
        name="magik",
        description="Funnily distorts up to four uploaded images/videos, links, or avatars",
    )
    @app_commands.describe(
        media_1="An image or video to distort",
        media_2="A second image or video",
        media_3="A third image or video",
        media_4="A fourth image or video",
        user_1="Use this user's avatar",
        user_2="Use another user's avatar",
        user_3="Use another user's avatar",
        user_4="Use another user's avatar",
        urls="One or more direct media links separated by spaces",
    )
    @app_commands.checks.cooldown(1, 6.0, key=lambda interaction: interaction.user.id)
    async def magik_slash(
        self,
        interaction: discord.Interaction,
        media_1: Optional[discord.Attachment] = None,
        media_2: Optional[discord.Attachment] = None,
        media_3: Optional[discord.Attachment] = None,
        media_4: Optional[discord.Attachment] = None,
        user_1: Optional[discord.User] = None,
        user_2: Optional[discord.User] = None,
        user_3: Optional[discord.User] = None,
        user_4: Optional[discord.User] = None,
        urls: Optional[str] = None,
    ):
        """Slash-command entry point that also works from user installations."""
        attachments = tuple(filter(None, (media_1, media_2, media_3, media_4)))
        users = tuple(filter(None, (user_1, user_2, user_3, user_4)))
        links = urls.split() if urls else []
        invalid = [link for link in links if not link.startswith(("https://", "http://"))]
        if invalid:
            return await interaction.response.send_message(
                f"These aren't valid media links: `{', '.join(invalid)}`",
                ephemeral=True,
            )

        selected = [attachment.url for attachment in attachments]
        for user in users:
            if not await self.bot.get_privacy(user, "fun_commands"):
                return await interaction.response.send_message(
                    f"{user} has fun commands disabled in their privacy settings.",
                    ephemeral=True,
                )
            selected.append(str(user.display_avatar.with_size(512)))
        selected.extend(links)
        selected = list(dict.fromkeys(selected))

        if not selected:
            return await interaction.response.send_message(
                "Add at least one image, video, user, or direct media link.",
                ephemeral=True,
            )
        if len(selected) > 4:
            return await interaction.response.send_message(
                "You can magik up to four media files at once.",
                ephemeral=True,
            )

        await interaction.response.defer(thinking=True)
        progress_message = None

        async def update_progress(percent, stage, video_count):
            nonlocal progress_message
            progress_content = self._magik_progress_content(
                percent, stage, video_count
            )
            if progress_message is None:
                progress_message = await interaction.followup.send(
                    progress_content, wait=True
                )
            else:
                await progress_message.edit(content=progress_content)

        files, content = await self._magik_results(selected, update_progress)
        if files is None:
            return await interaction.followup.send(content)
        await interaction.followup.send(content=content, files=files)

    async def _slime_media(self, data: bytes):
        """Render the avatar animation away from Discord's event loop."""
        with tempfile.TemporaryDirectory(prefix="fate-slime-") as temp_name:
            temp_dir = Path(temp_name)
            loop = asyncio.get_running_loop()
            frame_count = await loop.run_in_executor(
                self._magik_executor, _render_slime_animation, data, temp_name
            )
            gif_path = temp_dir / "slime.gif"
            if not self._ffmpeg:
                return await asyncio.to_thread(gif_path.read_bytes), "gif"

            output_path = temp_dir / "slime.mp4"
            try:
                await self._run_magik_process(
                    self._ffmpeg,
                    "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-framerate", str(SLIME_VIDEO_FPS),
                    "-start_number", "0",
                    "-i", str(temp_dir / "%04d.png"),
                    "-frames:v", str(frame_count),
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "27",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    "-an",
                    str(output_path),
                    timeout=90,
                )
            except RuntimeError as error:
                self.bot.log.warning(
                    f"Slime video encode failed; using GIF fallback: {str(error)[-2000:]}"
                )
                return await asyncio.to_thread(gif_path.read_bytes), "gif"
            return await asyncio.to_thread(output_path.read_bytes), "mp4"

    @commands.command(
        name="slime",
        description="Runs an avatar over with a cartoon car",
    )
    @commands.cooldown(1, 8, commands.BucketType.user)
    @commands.max_concurrency(2, commands.BucketType.default, wait=False)
    @commands.bot_has_permissions(attach_files=True)
    async def slime(self, ctx, user: Optional[discord.User] = None):
        """Turn a user's avatar into a short slapstick car animation."""
        target = user or ctx.author
        if target.id != ctx.author.id and not await self.bot.get_privacy(
            target, "fun_commands"
        ):
            return await ctx.send(
                f"{target} has fun commands disabled in {ctx.prefix}privacy"
            )

        try:
            avatar = await self.bot.get_resource(
                str(target.display_avatar.with_size(256)),
                label=f"{target} avatar",
                timeout=30,
            )
        except Exception as error:
            self.bot.log.warning(
                f"Slime avatar download failed for {target.id}: {str(error)[-1000:]}"
            )
            return await ctx.send("I couldn't download that avatar. Try again in a moment.")
        if not isinstance(avatar, bytes):
            return await ctx.send("I couldn't download that avatar. Try again in a moment.")

        try:
            async with ctx.typing():
                animation, extension = await self._slime_media(avatar)
        except (OSError, ValueError, MagikMediaError) as error:
            self.bot.log.warning(
                f"Slime animation failed for {target.id}: {str(error)[-2000:]}"
            )
            return await ctx.send("I couldn't render that slime video.")
        except Exception as error:
            self.bot.log.exception(
                f"Unexpected slime animation failure for {target.id}: {error}"
            )
            return await ctx.send("I couldn't render that slime video.")

        display_name = discord.utils.escape_markdown(
            getattr(target, "display_name", target.name)
        )
        await ctx.send(
            f"ðŸš— **{display_name}** got slimed.",
            file=discord.File(
                BytesIO(animation), filename=f"slime-{target.id}.{extension}"
            ),
        )

    @commands.command(name="liedetector", aliases=["ld"], description="Analyzes how truthful a member is")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def liedetector(self, ctx, *, member: discord.Member = None):
        if member is None:
            member = ctx.author
        e = discord.Embed(color=0x0000FF)
        e.set_author(name=f"{member.display_name}'s msg analysis", icon_url=member.display_avatar.url)
        percentage = random.randint(50, 100)
        choices = ["truth", "the truth", "a lie", "lie"]
        e.description = f"{percentage}% {random.choice(choices)}"
        await ctx.send(embed=e)
        if ctx.guild and ctx.channel.permissions_for(ctx.guild.me).manage_messages:
            await ctx.message.delete()

    @commands.command(name="personality", description="Generates a personality profile")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def personality(self, ctx, *, member: discord.Member = None):
        if member is None:
            member = ctx.author
        e = discord.Embed(color=colors.random())
        e.set_author(name=f"{member.display_name}'s Personality", icon_url=member.display_avatar.url)
        e.add_field(name="Type", value=f'{random.choice(Personalities.types)}', inline=False)
        e.add_field(name="Social Status", value=f'{random.choice(Personalities.statuses)}', inline=False)
        e.add_field(name="Hobby", value=f'{random.choice(Personalities.hobbies)}', inline=False)
        e.add_field(name="Music Genre", value=f'{random.choice(Personalities.genres)}', inline=False)
        await ctx.send(embed=e)
        if ctx.guild and ctx.channel.permissions_for(ctx.guild.me).manage_messages:
            await ctx.message.delete()

    @commands.command(name="pain", description="Spain without the s")
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def pain(self, ctx):
        await ctx.send("Spain but the s is silent")

    @commands.command(name="spain", description="Pain with an s")
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def spain(self, ctx):
        await ctx.send("Pain but with an s")

    @commands.command(description="Measures a member's soul")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def soul(self, ctx, *, member: discord.Member = None):
        if member is None:
            member = ctx.author
        r = random.randint(0, 1000)
        e = discord.Embed(color=0xFFFF00)
        e.set_author(name=f"{member.name}'s Soul Analysis", icon_url=member.display_avatar.url)
        e.description = f"{r} grams of soul"
        await ctx.send(embed=e)

    @commands.command(description="Rolls a six-sided die")
    @commands.cooldown(2, 5, commands.BucketType.channel)
    async def roll(self, ctx):
        await ctx.send(random.choice(["1", "2", "3", "4", "5", "6"]))

    @commands.command(name="ask", aliases=["8ball"], description="Asks the magic 8-ball")
    @commands.cooldown(2, 5, commands.BucketType.channel)
    async def ask(self, ctx):
        choices = [
            "Yes",
            "No",
            "It's certain",
            "110% no",
            "It's uncertain",
            "Ofc",
            "I think not m8",
            "Ig",
            "Why not ¯\\_(ツ)_/¯",
            "Ye",
            "Yep",
            "Yup",
            "tHe AnSwEr LiEs WiThIn",
            "Basically yes^",
            "Not really",
            "Well duh",
            "hell yeah",
            "hell no",
            "silence fatty",
            "no, so cope",
            "fuck no XDDDDDD",
            "imagine <:you:841098144536068106>"
        ]
        await ctx.send(random.choice(choices))

    @commands.command(
        name="gay",
        aliases=["straight", "bi", "ace", "pan"],
        description="Generates or sets a sexuality percentage",
    )
    @commands.cooldown(2, 10, commands.BucketType.user)
    @commands.cooldown(3, 6, commands.BucketType.channel)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def sexuality(self, ctx, percentage=None):
        usage = (
            f"Usage: `{get_prefix(ctx)}{ctx.invoked_with} percentage/reset/help`"
            f"\nExample Usage: `{get_prefix(ctx)}{ctx.invoked_with} 75%`"
            f"\n\nThe available sexualities are gay, {', '.join(self.sexuality.aliases)}."
        )
        invoked_with = str(ctx.invoked_with).lower()
        if invoked_with == "sexuality":
            return await ctx.send(usage)
        user = ctx.author
        if ctx.message.mentions:
            user = ctx.message.mentions[0]
        user_id = str(user.id)
        if percentage and not ctx.message.mentions:
            if percentage.lower() == "reset":
                if user_id not in self.gay[invoked_with]:
                    return await ctx.send("You don't have a custom percentage set")
                self.gay.remove_sub(invoked_with, user_id)
                await ctx.send(f"Removed your custom {invoked_with} percentage")
            elif percentage.lower() == "help":
                return await ctx.send(usage)
            else:
                stripped = percentage.strip("%")
                try:
                    custom_percentage = int(stripped)
                    if custom_percentage > 100:
                        return await ctx.send("That's too high of a percentage")
                    if custom_percentage < 0:
                        return await ctx.send("Yikes, must suck")
                except ValueError:
                    return await ctx.send("The percentage needs to be an integer")
                self.gay[invoked_with][user_id] = custom_percentage
                await ctx.send(
                    f"Use `{get_prefix(ctx)}{invoked_with} reset` to go back to random results"
                )
            await self.gay.flush()
        e = discord.Embed(color=user.color)
        e.set_author(name=str(user), icon_url=user.display_avatar.url)
        percentage = random.randint(0, 100)
        if user_id in self.gay[invoked_with]:
            percentage = self.gay[invoked_with][user_id]
        e.description = f"{percentage}% {invoked_with}"
        await ctx.send(embed=e)

    @commands.command(
        name="cringe",
        description="Generates a percentage for the chosen trait",
        aliases=[
            "based",
            "bruh",
            "buff",
            "chad",
            "correct",
            "crazy",
            "drunk",
            "dumb",
            "ego",
            "epic",
            "exotic",
            "fake",
            "fat",
            "fucked",
            "high",
            "hitler",
            "horny",
            "hot",
            "karen",
            "kind",
            "lucky",
            "penis",
            "sexy",
            "shit",
            "smart",
            "stupid",
            "swag",
            "ugly",
            "unlucky",
            "bald"
        ],
    )
    @commands.cooldown(3, 5, commands.BucketType.user)
    @commands.bot_has_permissions(embed_links=True)
    async def cringe(self, ctx):
        user = ctx.author
        if ctx.message.mentions:
            user = ctx.message.mentions[0]
        e = discord.Embed(color=user.color)
        e.set_author(name=str(user), icon_url=user.display_avatar.url)
        percentage = random.randint(0, 100)
        if ctx.invoked_with == "hitler":
            if random.randint(1, 4) == 1:
                ctx.invoked_with = f"worse than {ctx.invoked_with}"
        e.description = f"{percentage}% {ctx.invoked_with}"
        await ctx.send(embed=e)

    @commands.command(name="sue", description="Files a fictional lawsuit against a member")
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def sue(self, ctx, user: discord.Member):
        r = random.randint(1, 1000)
        if user.id in self.bot.owner_ids:
            r = 0
        if ctx.author.id in self.bot.owner_ids:
            r = random.randint(1000000, 1000000000)
        e = discord.Embed(color=0xAAF200)
        e.set_author(
            name=str(ctx.author),
            icon_url=ctx.author.display_avatar.url,
        )
        e.set_thumbnail(
            url="https://cdn.discordapp.com/attachments/501871950260469790/511997534181392424/money-png-12.png"
        )
        e.description = f"Filed a lawsuit against {user}\nAmount: ${r}"
        await ctx.send(embed=e)
        await ctx.message.delete()

    @commands.command(name="roulette", aliases=["rr"], description="Plays a round of Russian roulette")
    @commands.cooldown(2, 5, commands.BucketType.channel)
    async def roulette(self, ctx):
        async with self.bot.utils.open("data/users") as f:
            users = await self.bot.load(await f.read())
        if ctx.author.id in users:
            return await ctx.send("You lived")
        await ctx.send(random.choice([*["You lived"] * 6, "You died"]))


async def setup(bot: Fate) -> None:
    await bot.add_cog(Fun(bot), override=True)
