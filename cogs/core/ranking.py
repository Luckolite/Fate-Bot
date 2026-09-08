"""
cogs.core.ranking
~~~~~~~~~~~~~~~~~~

A customizable xp ranking cog

:copyright: (C) 2019-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from random import choice, randint
from time import time, monotonic
from typing import Union
from unicodedata import normalize
from weakref import WeakValueDictionary

import aiohttp
import discord
from PIL import Image, ImageFont, ImageDraw, ImageSequence, UnidentifiedImageError
from discord import NotFound, Forbidden
from discord.ext import commands, tasks
from pymysql.err import DataError, InternalError, OperationalError

from botutils import colors, get_prefix, url_from, Menu, cache_rewrite, GetConfirmation
from botutils.pillow import add_corners
from checks.exceptions import IgnoredExit


def profile_help():
    e = discord.Embed(color=colors.purple)
    e.add_field(
        name=".set title your_new_title",
        value="Changes the title field in your profile card",
        inline=False,
    )
    e.add_field(
        name=".set background [optional-url]",
        value="You can attach a file while using the cmd, or put a link where it says optional-url. "
        "If you don't do either, i'll reset your background to default (transparent)",
    )
    return e


leaderboard_icon = "https://cdn.discordapp.com/attachments/501871950260469790/505198377412067328/20181025_215740.png"
ROOT_DIR = Path(__file__).resolve().parents[2]
RANKING_ASSETS = ROOT_DIR / "assets" / "ranking"
FONT_DIR = ROOT_DIR / "botutils" / "fonts"
MODERN_FONT = FONT_DIR / "Modern_Sans_Light.otf"
ROBOTO_FONT = FONT_DIR / "Roboto-Bold.ttf"
RESAMPLE = Image.Resampling.LANCZOS


def get_font(cache, path, size):
    size = max(1, int(size))
    key = (path, size)
    if key not in cache:
        cache[key] = ImageFont.truetype(str(path), size=size)
    return cache[key]


def fit_font(draw, text, cache, path, max_size, max_width, min_size=12):
    """Return the largest font that keeps text within the supplied width."""
    low = max(1, min_size)
    high = max(low, max_size)
    best = get_font(cache, path, low)

    while low <= high:
        size = (low + high) // 2
        candidate = get_font(cache, path, size)
        left, _top, right, _bottom = draw.textbbox((0, 0), text, font=candidate)
        if right - left <= max_width:
            best = candidate
            low = size + 1
        else:
            high = size - 1
    return best


class RankCardAssets:
    def __init__(self):
        self.rank_card = self.open(RANKING_ASSETS / "rank-card.png")
        data = []
        for red, green, blue, alpha in self.rank_card.getdata():
            if alpha == 0:
                data.append((red, green, blue, alpha))
            elif (red, green, blue) == (0, 174, 239):
                data.append((red, green, blue, 100))
            elif (red, green, blue) == (48, 48, 48):
                data.append((red, green, blue, 225))
            elif (red, green, blue) == (218, 218, 218):
                data.append((red, green, blue, 150))
            else:
                data.append((red, green, blue, alpha))
        self.rank_card.putdata(data)

        self.online = self.status_icon((59, 165, 93))
        self.idle = self.status_icon((250, 168, 26))
        self.dnd = self.status_icon((237, 66, 69))
        self.offline = self.status_icon((116, 127, 141))
        self.ribbons = []
        for index in range(1, 4):
            ribbon = self.open(RANKING_ASSETS / f"ribbon_{index}.png")
            self.ribbons.append(ribbon.resize((90, 90), RESAMPLE))

    @staticmethod
    def open(path: Path) -> Image.Image:
        with Image.open(path) as image:
            return image.convert("RGBA")

    @staticmethod
    def status_icon(color) -> Image.Image:
        icon = Image.new("RGBA", (75, 75), (0, 0, 0, 0))
        draw = ImageDraw.Draw(icon)
        draw.ellipse((5, 5, 70, 70), fill=color, outline=(48, 48, 48), width=8)
        return icon


class Ranking(commands.Cog):
    default_config = {
        "min_xp_per_msg": 1,
        "max_xp_per_msg": 1,
        "first_lvl_xp_req": 250,
        "timeframe": 10,
        "msgs_within_timeframe": 1,
        "disabled_channels": [],
    }

    flush_interval = 30
    leaderboard_cache_ttl = 30
    xp_cache_ttl = 60 * 60 * 2

    def __init__(self, bot):
        self.bot = bot
        self.channels: bot.utils.cache = bot.cogs["Messages"].config
        self.xp_cache = {}
        self.xp_cache_access = {}
        self.xp_cache_locks = WeakValueDictionary()
        self.role_reward_cache = {}
        self.role_reward_locks = WeakValueDictionary()
        self.leaderboard_cache = {}
        self.leaderboard_locks = WeakValueDictionary()

        self.cd = {}
        self.global_cd = {}
        self.spam_cd = {}
        self.macro_cd = {}

        self.pending_global = {}
        self.pending_global_monthly = {}
        self.pending_guild = {}
        self.pending_monthly = {}
        self.pending_commands = {}
        self.flush_lock = asyncio.Lock()

        # Configs
        self.config = cache_rewrite.Cache(bot, "ranking", default=self.default_config)
        self.profile = cache_rewrite.Cache(bot, "profiles")

        # Help menus
        self.set_usage = self.set
        self.role_rewards_usage = self.role_rewards
        self.profile_usage = f"`.profile` your global rank\n" \
                             f"`.rank` your rank in the server"
        self.leaderboard_usage = "`.lb` server leaderboard\n" \
                                 "`.glb` global leaderboard\n" \
                                 "`.mlb` monthly server leaderboard\n" \
                                 "`.gmlb` global monthly server leaderboard"
        self.clb_usage = "`.clb` displays the top used commands on the bot"
        self.top_help = "shows the top 10 ranked users in the server"

        self.cmd_cleanup_task.start()
        self.monthly_cleanup_task.start()
        self.cooldown_cleanup_task.start()
        self.xp_flush_task.start()

        self.active = {}
        self.assets = None

    async def cog_load(self):
        # Decoding and recoloring the rank-card templates walks every pixel.
        # Discord waits for cog_load before exposing the cog, so this can be
        # prepared off-loop without a partially initialized command surface.
        self.assets = await asyncio.to_thread(RankCardAssets)

    async def cog_unload(self):
        self.cmd_cleanup_task.cancel()
        self.monthly_cleanup_task.cancel()
        self.cooldown_cleanup_task.cancel()
        with suppress(Exception):
            await self.flush_pending()
        self.xp_flush_task.cancel()

    @staticmethod
    def add_pending(mapping, key, amount) -> None:
        mapping[key] = mapping.get(key, 0) + amount

    @staticmethod
    def xp_disabled_in_channel(config, channel) -> bool:
        """Return whether messages in this channel are excluded from XP."""
        disabled = {
            int(channel_id)
            for channel_id in config.get("disabled_channels", [])
            if not isinstance(channel_id, bool) and str(channel_id).isdigit()
        }
        return channel.id in disabled or getattr(channel, "parent_id", None) in disabled

    @staticmethod
    def merge_pending(mapping, failed) -> None:
        for key, amount in failed.items():
            mapping[key] = mapping.get(key, 0) + amount

    def restore_batches(self, batches) -> None:
        self.merge_pending(self.pending_global, batches["global"])
        self.merge_pending(self.pending_global_monthly, batches["global_monthly"])
        self.merge_pending(self.pending_guild, batches["guild"])
        self.merge_pending(self.pending_monthly, batches["monthly"])
        self.merge_pending(self.pending_commands, batches["commands"])

    @tasks.loop(seconds=flush_interval)
    async def xp_flush_task(self):
        await self.flush_pending()

    async def flush_pending(self):
        """Flush aggregated XP and command usage to MySQL."""
        if not self.bot.pool:
            return

        async with self.flush_lock:
            batches = {
                "global": self.pending_global,
                "global_monthly": self.pending_global_monthly,
                "guild": self.pending_guild,
                "monthly": self.pending_monthly,
                "commands": self.pending_commands,
            }
            self.pending_global = {}
            self.pending_global_monthly = {}
            self.pending_guild = {}
            self.pending_monthly = {}
            self.pending_commands = {}

            if not any(batches.values()):
                return

            try:
                async with self.bot.utils.cursor() as cur:
                    if batches["global"]:
                        await cur.executemany(
                            "insert into global_msg (user_id, xp) values (%s, %s) "
                            "on duplicate key update xp = global_msg.xp + values(xp);",
                            [(user_id, amount)
                             for user_id, amount in batches["global"].items()]
                        )
                        batches["global"] = {}
                    if batches["global_monthly"]:
                        await cur.executemany(
                            "insert into global_monthly (user_id, timeframe, xp) "
                            "values (%s, %s, %s) "
                            "on duplicate key update xp = global_monthly.xp + values(xp);",
                            [(*key, amount) for key, amount in batches["global_monthly"].items()]
                        )
                        batches["global_monthly"] = {}
                    if batches["guild"]:
                        await cur.executemany(
                            "insert into msg (guild_id, user_id, xp) values (%s, %s, %s) "
                            "on duplicate key update xp = msg.xp + values(xp);",
                            [(*key, amount) for key, amount in batches["guild"].items()]
                        )
                        batches["guild"] = {}
                    if batches["monthly"]:
                        await cur.executemany(
                            "insert into monthly_msg (guild_id, user_id, msg_time, xp) "
                            "values (%s, %s, %s, %s) "
                            "on duplicate key update xp = monthly_msg.xp + values(xp);",
                            [(*key, amount) for key, amount in batches["monthly"].items()]
                        )
                        batches["monthly"] = {}
                    if batches["commands"]:
                        await cur.executemany(
                            "insert into commands (command, total, ran_at) "
                            "values (%s, %s, %s) "
                            "on duplicate key update total = commands.total + values(total);",
                            [(command, amount, ran_at)
                             for (command, ran_at), amount in batches["commands"].items()]
                        )
                        batches["commands"] = {}
                self.leaderboard_cache.clear()
            except asyncio.CancelledError:
                self.restore_batches(batches)
                raise
            except Exception as error:
                self.restore_batches(batches)
                self.bot.log(f"Error flushing ranking updates\n{error}")

    async def get_role_rewards(self, guild_id):
        lock = self.role_reward_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            if guild_id in self.role_reward_cache:
                return self.role_reward_cache[guild_id]
            async with self.bot.utils.cursor() as cur:
                await cur.execute(
                    f"select role_id, lvl, stack from role_rewards "
                    f"where guild_id = {guild_id} order by lvl desc;"
                )
                self.role_reward_cache[guild_id] = list(await cur.fetchall())
            return self.role_reward_cache[guild_id]

    async def get_cached_xp(self, guild_id, user_id):
        key = (guild_id, user_id)
        lock = self.xp_cache_locks.setdefault(key, asyncio.Lock())
        async with lock:
            self.xp_cache_access[key] = monotonic()
            if key in self.xp_cache:
                return self.xp_cache[key]
            async with self.bot.utils.cursor() as cur:
                await cur.execute(
                    f"select xp from msg where guild_id = {guild_id} "
                    f"and user_id = {user_id} limit 1;"
                )
                result = await cur.fetchone()
            self.xp_cache[key] = result[0] if result else 0
            return self.xp_cache[key]

    async def handle_level_roles(self, msg, level):
        guild_id = msg.guild.id
        rewards = await self.get_role_rewards(guild_id)
        available = [reward for reward in rewards if reward[1] <= level]

        for role_id, _lvl, stack, *_args in available:
            role = msg.guild.get_role(role_id)
            if not role:
                await self.bot.execute(
                    f"delete from role_rewards where role_id = {role_id} limit 1;"
                )
                self.role_reward_cache.pop(guild_id, None)
                continue
            if role in msg.author.roles:
                if not stack:
                    return
                continue
            try:
                await msg.author.add_roles(role)
                e = discord.Embed(color=role.color)
                e.description = f"You leveled up and earned {role.mention}"
                with suppress(Forbidden):
                    await msg.channel.send(embed=e)
                if not stack:
                    reward_ids = {reward[0] for reward in rewards}
                    old_roles = [
                        old_role for old_role in msg.author.roles
                        if old_role.id in reward_ids and old_role.id != role_id
                    ]
                    if old_roles:
                        await msg.author.remove_roles(*old_roles)
                    return
            except (NotFound, Forbidden, AttributeError):
                await self.bot.execute(
                    f"delete from role_rewards where role_id = {role_id} limit 1;"
                )
                self.role_reward_cache.pop(guild_id, None)
            except Exception:
                pass
            if not stack:
                return

    @tasks.loop(hours=1)
    async def cmd_cleanup_task(self):
        cutoff = int((
            datetime.now(tz=timezone.utc) - timedelta(days=30)
        ).timestamp())
        removed = 0
        try:
            async with self.bot.utils.cursor() as cur:
                while True:
                    await cur.execute(
                        "delete from commands where ran_at not regexp "
                        "'^[0-9]+([.][0-9]+)?$' limit 5000;"
                    )
                    removed += cur.rowcount
                    if cur.rowcount < 5000:
                        break
                    await asyncio.sleep(0.1)
                while True:
                    await cur.execute(
                        "delete from commands where ran_at < %s limit 5000;",
                        (cutoff,)
                    )
                    removed += cur.rowcount
                    if cur.rowcount < 5000:
                        break
                    await asyncio.sleep(0.1)
        except Exception as error:
            self.bot.log(f"Error cleaning up 30-day command usage\n{error}")
        else:
            self.bot.log.debug(
                f"Removed {removed:,} command usage rows older than 30 days"
            )

    @tasks.loop(minutes=1)
    async def cooldown_cleanup_task(self):
        before = monotonic()
        now = time()
        expired = [
            user_id for user_id, timestamp in self.global_cd.items()
            if timestamp < now
        ]
        for user_id in expired:
            del self.global_cd[user_id]
        self.active = {
            user_id: activity
            for user_id, activity in self.active.items()
            if activity[1] > now - 7200
        }
        for guild_id, users in list(self.cd.items()):
            active_users = {
                user_id: [stamp for stamp in stamps if stamp > now - 3600]
                for user_id, stamps in users.items()
            }
            self.cd[guild_id] = {
                user_id: stamps
                for user_id, stamps in active_users.items()
                if stamps
            }
            if not self.cd[guild_id]:
                del self.cd[guild_id]
        cache_now = monotonic()
        self.leaderboard_cache = {
            guild_id: cached
            for guild_id, cached in self.leaderboard_cache.items()
            if cached[0] > cache_now
        }
        xp_cutoff = cache_now - self.xp_cache_ttl
        expired_xp = [
            key for key, accessed_at in self.xp_cache_access.items()
            if accessed_at < xp_cutoff
            and key not in self.xp_cache_locks
        ]
        for key in expired_xp:
            self.xp_cache.pop(key, None)
            self.xp_cache_access.pop(key, None)

        current_guild_ids = {guild.id for guild in self.bot.guilds}
        self.role_reward_cache = {
            guild_id: rewards
            for guild_id, rewards in self.role_reward_cache.items()
            if guild_id in current_guild_ids
        }
        ping = str(round((monotonic() - before) * 1000)) + "ms"
        self.bot.log.debug(f"Removed {len(expired)} cooldowns in {ping}")
        self.spam_cd = {}
        self.macro_cd = {}

    @tasks.loop(hours=1)
    async def monthly_cleanup_task(self):
        await asyncio.sleep(1)
        self.bot.log.debug("Started xp cleanup task")
        if not self.bot.is_ready():
            await self.bot.wait_until_ready()
        while self.bot.pool is None:
            await asyncio.sleep(5)
        async with self.bot.utils.cursor() as cur:
            limit = int(time() - 60 * 60 * 24 * 30)  # One month
            removed = 0
            for table, column in (
                ("monthly_msg", "msg_time"),
                ("global_monthly", "timeframe"),
            ):
                while True:
                    await cur.execute(
                        f"delete from {table} where {column} not regexp "
                        "'^[0-9]+([.][0-9]+)?$' limit 5000;"
                    )
                    removed += cur.rowcount
                    if cur.rowcount < 5000:
                        break
                    await asyncio.sleep(0.1)
                while True:
                    await cur.execute(
                        f"delete from {table} where {column} < %s limit 5000;",
                        (limit,)
                    )
                    removed += cur.rowcount
                    if cur.rowcount < 5000:
                        break
                    await asyncio.sleep(0.1)
            self.bot.log.debug(
                f"Removed {removed} expired rows from monthly leaderboards"
            )

    def calc_lvl_info(self, xp, config):
        level = 0
        remaining_xp = xp
        base_requirement = config["first_lvl_xp_req"]
        multiplier = 1
        increase_by = 0.125
        reduce_at = 3

        while True:
            current_req = base_requirement * multiplier
            if remaining_xp < current_req:
                break
            remaining_xp -= current_req
            level += 1
            if level >= 500:
                break
            if multiplier >= reduce_at:
                increase_by /= 2
                reduce_at += 3
            multiplier += increase_by

        if level >= 500:
            remaining_xp = current_req

        data = {
            "level": level,
            "xp": xp,
            "level_start": (xp + (current_req - remaining_xp)) - current_req,
            "level_end": xp + (current_req - remaining_xp),
            "start_to_end": current_req,
            "progress": remaining_xp
        }
        return {
            key: round(value) for key, value in data.items()
        }

    @commands.Cog.listener()
    async def on_message(self, msg):
        if msg.guild and not msg.author.bot and self.bot.pool:
            guild_id = msg.guild.id
            user_id = msg.author.id
            conf = await self.config[guild_id] or self.default_config
            if self.xp_disabled_in_channel(conf, msg.channel):
                return

            current_time = time()

            def punish():
                self.global_cd[user_id] = current_time + 60

            # anti spam
            now = int(current_time / 5)
            if guild_id not in self.spam_cd:
                self.spam_cd[guild_id] = {}
            if user_id not in self.spam_cd[guild_id]:
                self.spam_cd[guild_id][user_id] = [now, 0]
            if self.spam_cd[guild_id][user_id][0] == now:
                self.spam_cd[guild_id][user_id][1] += 1
            else:
                self.spam_cd[guild_id][user_id] = [now, 0]
            if self.spam_cd[guild_id][user_id][1] > 3:
                punish()
                return

            # anti macro
            if user_id not in self.macro_cd:
                self.macro_cd[user_id] = {}
                self.macro_cd[user_id]["intervals"] = []
            if "last" not in self.macro_cd[user_id]:
                self.macro_cd[user_id]["last"] = datetime.now(tz=timezone.utc)
            else:
                last = self.macro_cd[user_id]["last"]
                current = datetime.now(tz=timezone.utc)
                self.macro_cd[user_id]["last"] = current
                self.macro_cd[user_id]["intervals"].append((current - last).seconds)
                intervals = self.macro_cd[user_id]["intervals"]
                self.macro_cd[user_id]["intervals"] = intervals[-3:]
                if len(intervals) > 2:
                    if all(interval == intervals[0] for interval in intervals):
                        punish()
                        return

            set_time = int(datetime.timestamp(
                datetime.now(tz=timezone.utc).replace(
                    microsecond=0, second=0, minute=0, hour=0
                )
            ))
            if user_id not in self.global_cd:
                self.global_cd[user_id] = 0
            if self.global_cd[user_id] < current_time:
                self.global_cd[user_id] = current_time + 10

                if user_id not in self.active:
                    self.active[user_id] = [current_time, current_time]
                started_at, latest = self.active[user_id]

                if latest < current_time - 7200:  # At least 2h passed since last msg
                    del self.active[user_id]

                elif started_at < current_time - 60 * 60 * 12:  # Ignore past 12 hours of activity
                    return

                else:  # Update the latest msg time
                    self.active[user_id][1] = current_time

                self.add_pending(self.pending_global, user_id, 1)
                self.add_pending(
                    self.pending_global_monthly,
                    (user_id, set_time),
                    1
                )

            # per-server leveling
            if conf["min_xp_per_msg"] >= conf["max_xp_per_msg"]:
                new_xp = conf["min_xp_per_msg"]
            else:
                new_xp = randint(conf["min_xp_per_msg"], conf["max_xp_per_msg"])
            if guild_id not in self.cd:
                self.cd[guild_id] = {}
            if user_id not in self.cd[guild_id]:
                self.cd[guild_id][user_id] = []
            msgs = [
                x
                for x in self.cd[guild_id][user_id]
                if x > current_time - conf["timeframe"]
            ]
            self.cd[guild_id][user_id] = msgs
            if len(msgs) < conf["msgs_within_timeframe"]:
                self.cd[guild_id][user_id].append(current_time)
                try:
                    xp = await self.get_cached_xp(guild_id, user_id)
                    previous_level = self.calc_lvl_info(xp, conf)["level"]
                    xp += new_xp
                    self.xp_cache[(guild_id, user_id)] = xp
                    self.xp_cache_access[(guild_id, user_id)] = monotonic()
                    dat = self.calc_lvl_info(xp, conf)

                    self.add_pending(
                        self.pending_guild,
                        (guild_id, user_id),
                        new_xp
                    )
                    self.add_pending(
                        self.pending_monthly,
                        (guild_id, user_id, set_time),
                        new_xp
                    )

                    leveled_up = dat["level"] > previous_level

                    if leveled_up:
                        if config := self.channels.get(guild_id, None):
                            if location := config.get("level_up_messages", None):
                                channel = self.bot.get_channel(location) or msg.channel
                                if channel and channel.permissions_for(msg.guild.me).send_messages:
                                    with suppress(Forbidden):
                                        await channel.send(
                                            content=f"{msg.author.mention}, **you're now level {dat['level']}**",
                                            allowed_mentions=discord.AllowedMentions(
                                                users=True, roles=False, everyone=False
                                            )
                                        )

                    await self.handle_level_roles(msg, dat["level"])

                except DataError as error:
                    self.bot.log(f"Error updating guild xp\n{error}")
                except (InternalError, OperationalError, RuntimeError):
                    pass

    @commands.Cog.listener()
    async def on_command(self, ctx):
        if ctx.author.id in self.bot.ignored_locations:
            return
        if ctx.guild and ctx.guild.id in self.bot.ignored_locations:
            return
        if ctx.author.id in self.bot.blocked:
            return
        cmd = "gay" if ctx.command.name == "sexuality" else ctx.command.name
        set_time = int(datetime.timestamp(
            datetime.now(tz=timezone.utc).replace(microsecond=0, second=0, minute=0, hour=0)
        ))
        self.add_pending(self.pending_commands, (cmd, set_time), 1)

    @commands.group(
        name="role-rewards",
        aliases=["level-rewards", "level-roles", "lr"],
        description="Grant roles for leveling up"
    )
    @commands.bot_has_permissions(embed_links=True, manage_roles=True)
    async def role_rewards(self, ctx):
        if not ctx.invoked_subcommand:
            e = discord.Embed(color=self.bot.config["theme_color"])
            e.set_author(name="Level Roles", icon_url=self.bot.user.display_avatar.url)
            e.set_thumbnail(url=url_from(ctx.guild.icon))
            e.description = self.role_rewards.description
            p = get_prefix(ctx)  # type: str
            cmd = ctx.invoked_with
            e.add_field(
                name="◈ Usage",
                value=f"{p}{cmd} add [level] @role\n"
                      f"`adds a level reward`\n"
                      f"{p}{cmd} remove @role\n"
                      f"`removes a level reward`\n"
                      f"{p}{cmd} limit-all\n"
                      f"`only lets users keep the highest role reward`\n"
                      f"{p}{cmd} unlimit-all\n"
                      f"`lets users maintain every role reward they earn`",
                inline=False
            )
            e.add_field(
                name="◈ Note",
                value="Replace `@role` with the mention for the role you want to give, "
                      "and replace `[level]` with the level you want the role to be given at",
                inline=False
            )
            async with self.bot.utils.cursor() as cur:
                await cur.execute(
                    f"select role_id, lvl from role_rewards "
                    f"where guild_id = {ctx.guild.id} "
                    f"order by lvl desc;"
                )
                results = await cur.fetchall()
            if results:
                value = ""
                removed_stale = False
                for role_id, level in results:
                    if role := ctx.guild.get_role(int(role_id)):
                        value += f"\nLvl {level} - {role.mention}"
                    else:
                        removed_stale = True
                        await self.bot.execute(
                            f"delete from role_rewards "
                            f"where guild_id = {ctx.guild.id} "
                            f"and role_id = {role_id} "
                            f"limit 1;"
                        )
                if removed_stale:
                    self.role_reward_cache.pop(ctx.guild.id, None)
                if value:
                    e.add_field(
                        name="◈ Active Roles",
                        value=value,
                        inline=False
                    )
            await ctx.send(embed=e)

    @role_rewards.command(name="add", description="Adds a role reward for a level")
    @commands.has_permissions(manage_roles=True)
    async def _add(self, ctx, level: int, *, role: Union[discord.Role, str]):
        if level <= 0:
            return await ctx.send("The level requirement can't be less than 1")
        if isinstance(role, str):
            role = await self.bot.utils.get_role(ctx, role)
            if not role:
                return await ctx.send("I wasn't able to find that role")
        if role.position >= ctx.author.top_role.position:
            return await ctx.send("That role's above your paygrade. Take a seat")
        if role.position >= ctx.guild.me.top_role.position:
            return await ctx.send("That role's too high for me to manage")

        # Check the number of role rewards
        async with self.bot.utils.cursor() as cur:
            await cur.execute(
                "select count(*) from role_rewards where guild_id = %s;",
                (ctx.guild.id,)
            )
            entries, = await cur.fetchone()
        if entries >= 10:
            return await ctx.send("You can't have more than 10 level rewards")

        stack = True
        await ctx.send(
            "Should I remove this role when the user gets a higher role reward? "
            "Reply with `yes` or `no`"
        )
        reply = await self.bot.utils.get_message(ctx)
        if "yes" in reply.content.lower():
            stack = False

        await self.bot.execute(
            f"insert into role_rewards "
            f"values ({ctx.guild.id}, {role.id}, {level}, {stack}) "
            f"on duplicate key update lvl = {level}, stack = {stack};"
        )
        self.role_reward_cache.pop(ctx.guild.id, None)
        await ctx.send(f"Added {role.mention}")

    @role_rewards.command(name="remove", description="Removes a role reward")
    @commands.has_permissions(manage_roles=True)
    async def _remove(self, ctx, role: Union[discord.Role, str]):
        if isinstance(role, str):
            role = await self.bot.utils.get_role(ctx, role)
            if not role:
                return await ctx.send("I wasn't able to find that role")
        await self.bot.execute(f"delete from role_rewards where role_id = {role.id};")
        self.role_reward_cache.pop(ctx.guild.id, None)
        await ctx.send(f"Removed the level-role for {role.mention} if it existed")

    @role_rewards.command(name="limit-all", description="Limits members to their highest reward role")
    @commands.has_permissions(manage_roles=True)
    async def _limit_all(self, ctx):
        await self.bot.execute(f"update role_rewards set stack = False where guild_id = {ctx.guild.id};")
        self.role_reward_cache.pop(ctx.guild.id, None)
        await ctx.send("Set the limit to only keep the top role reward")

    @role_rewards.command(name="unlimit-all", description="Allows members to keep every reward role")
    @commands.has_permissions(manage_roles=True)
    async def _unlimit_all(self, ctx):
        await self.bot.execute(f"update role_rewards set stack = True where guild_id = {ctx.guild.id};")
        self.role_reward_cache.pop(ctx.guild.id, None)
        await ctx.send("Set the limit to allow keeping all role rewards")

    @commands.command(name="xp-config", description="Shows the current xp configuration")
    @commands.cooldown(2, 5, commands.BucketType.user)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def xp_config(self, ctx):
        """ Sends an overview for the current config """
        e = discord.Embed(color=0x4A0E50)
        e.set_author(name="XP Configuration", icon_url=ctx.guild.owner.display_avatar.url)
        e.set_thumbnail(url=self.bot.user.display_avatar.url)
        conf = await self.config[ctx.guild.id]
        disabled_channels = conf.get("disabled_channels", [])
        disabled_text = ", ".join(f"<#{channel_id}>" for channel_id in disabled_channels)
        e.description = (
            f"• Min XP Per Msg: {conf['min_xp_per_msg']}"
            f"\n• Max XP Per Msg: {conf['max_xp_per_msg']}"
            f"\n• Timeframe: {conf['timeframe']}"
            f"\n• Msgs Within Timeframe: {conf['msgs_within_timeframe']}"
            f"\n• First Lvl XP Req: {conf['first_lvl_xp_req']}"
            f"\n• XP Disabled In: {disabled_text or 'No channels'}"
        )
        p = get_prefix(ctx)
        e.set_footer(text=f"Use {p}set to adjust these settings")
        await ctx.send(embed=e)

    @commands.group(name="set", description="Shows usage on how to use the set command")
    @commands.cooldown(2, 5, commands.BucketType.user)
    @commands.guild_only()
    async def set(self, ctx):
        if not ctx.invoked_subcommand:
            e = discord.Embed(color=colors.fate)
            e.set_author(name="Set Usage", icon_url=ctx.author.display_avatar.url)
            e.set_thumbnail(url=self.bot.user.display_avatar.url)
            p: str = ctx.prefix
            e.description = "`[]` = your arguments / setting"
            e.add_field(
                name="◈ Profile Stuff",
                value=f"{p}set title [new title]"
                f"\n• `sets the title in your profile`"
                f"\n{p}set background [optional_url]"
                f"\n• `sets your profiles background img`",
                inline=False,
            )
            e.add_field(
                name="◈ XP Stuff",
                value=f"{p}set min-xp-per-msg [amount]"
                f"\n• `sets the minimum gained xp per msg`"
                f"\n{p}set max-xp-per-msg [amount]"
                f"\n• `sets the maximum gained xp per msg`"
                f"\n{p}set timeframe [amount]"
                f"\n• `sets the timeframe to allow x messages`"
                f"\n{p}set msgs-within-timeframe [amount]"
                f"\n• `sets the limit of msgs within the timeframe`"
                f"\n{p}set first-lvl-xp-req [amount]"
                f"\n• `required xp to level up your first time`"
                f"\n{p}set disable-xp [optional-channel]"
                f"\n• `stops message xp in a channel`"
                f"\n{p}set enable-xp [optional-channel]"
                f"\n• `allows message xp in a channel again`",
                inline=False,
            )
            e.set_footer(text=f"Use {p}xp-config to see xp settings")
            await ctx.send(embed=e)

    @set.command(name="title", description="Sets the title in your profile card")
    async def _set_title(self, ctx, *, title):
        if len(title) > 32:
            return await ctx.send("Titles can't be greater than 32 characters")
        await self.profile[ctx.author.id].set("title", title)
        await ctx.send("Set your title")

    @set.command(name="background", description="Sets the background in your profile card")
    async def _set_background(self, ctx, url=None):
        profile = await self.profile[ctx.author.id]
        if not url and not ctx.message.attachments:
            if "background" not in profile:
                return await ctx.send("You don't have a custom background")
            del profile["background"]
            await ctx.send("Reset your background")
            return await profile.save()
        if not url:
            url = ctx.message.attachments[0].url
        profile["background"] = url
        await ctx.send("Set your background image")
        await profile.save()

    @set.command(name="min-xp-per-msg", description="Sets the minimum xp users get from a message")
    @commands.has_permissions(administrator=True)
    async def _min_xp_per_msg(self, ctx, amount: int):
        """ sets the minimum gained xp per msg """
        if amount > 100:
            return await ctx.send("biTcH nO, those heels are too high")
        if amount < 0:
            return await ctx.send("XP per message can't be negative")
        conf = await self.config[ctx.guild.id]
        conf["min_xp_per_msg"] = amount
        msg = f"Set the minimum xp gained per msg to {amount}"
        if amount > conf["max_xp_per_msg"]:
            conf["max_xp_per_msg"] = amount
            msg += f". I also upped the maximum xp per msg to {amount}"
        await ctx.send(msg)
        await conf.save()

    @set.command(name="max-xp-per-msg", description="Sets the maximum xp users get from a message")
    @commands.has_permissions(administrator=True)
    async def _max_xp_per_msg(self, ctx, amount: int):
        """ sets the minimum gained xp per msg """
        if amount > 100:
            return await ctx.send("biTcH nO, those heels are too high")
        if amount < 0:
            return await ctx.send("XP per message can't be negative")
        conf = await self.config[ctx.guild.id]
        conf["max_xp_per_msg"] = amount
        msg = f"Set the maximum xp gained per msg to {amount}"
        if amount < conf["min_xp_per_msg"]:
            conf["min_xp_per_msg"] = amount
            msg += f". I also lowered the minimum xp per msg to {amount}"
        await ctx.send(msg)
        await conf.save()

    @set.command(name="timeframe", description="Sets the timeframe to allow x messages")
    @commands.has_permissions(administrator=True)
    async def _timeframe(self, ctx, amount: int):
        """ sets the timeframe to allow x messages """
        if amount > 3600:
            return await ctx.send("The timeframe can't be longer than an hour")
        if amount < 1:
            return await ctx.send("The timeframe must be at least one second")
        await self.config[ctx.guild.id].set("timeframe", amount)
        await ctx.send(f"Set the timeframe that allows x messages to {amount}")

    @set.command(name="msgs-within-timeframe", description="the limit of msgs within the timeframe")
    @commands.has_permissions(administrator=True)
    async def _msgs_within_timeframe(self, ctx, amount: int):
        """ sets the limit of msgs within the timeframe """
        if amount > 3600:
            return await ctx.send("That number's too high")
        if amount < 1:
            return await ctx.send("The message limit must be at least one")
        await self.config[ctx.guild.id].set("msgs_within_timeframe", amount)
        await ctx.send(f"Set msgs within timeframe limit to {amount}")

    @set.command(name="first-lvl-xp-req", description="Sets the required xp to level up your first time")
    @commands.has_permissions(administrator=True)
    async def _first_level_xp_req(self, ctx, amount: int):
        """ sets the required xp to level up your first time """
        if amount > 2500:
            return await ctx.send("You can't set an amount greater than 2,500")
        elif amount < 100:
            return await ctx.send("You can't set an amount smaller than 100")
        await self.config[ctx.guild.id].set("first_lvl_xp_req", amount)
        await ctx.send(f"Set the required xp to level up your first time to {amount}")

    @set.command(name="disable-xp", description="Disables message XP in a channel")
    @commands.has_permissions(administrator=True)
    async def _disable_xp(
        self,
        ctx,
        channel: Union[discord.TextChannel, discord.Thread] = None,
    ):
        channel = channel or ctx.channel
        conf = await self.config[ctx.guild.id]
        disabled = {
            int(channel_id)
            for channel_id in conf.get("disabled_channels", [])
            if not isinstance(channel_id, bool) and str(channel_id).isdigit()
        }
        if channel.id in disabled:
            return await ctx.send(f"XP gain is already disabled in {channel.mention}")
        if len(disabled) >= 25:
            return await ctx.send("XP gain can be disabled in at most 25 channels")
        conf["disabled_channels"] = sorted((*disabled, channel.id))
        await conf.save()
        await ctx.send(f"Disabled XP gain in {channel.mention}")

    @set.command(name="enable-xp", description="Enables message XP in a channel")
    @commands.has_permissions(administrator=True)
    async def _enable_xp(
        self,
        ctx,
        channel: Union[discord.TextChannel, discord.Thread] = None,
    ):
        channel = channel or ctx.channel
        conf = await self.config[ctx.guild.id]
        disabled = {
            int(channel_id)
            for channel_id in conf.get("disabled_channels", [])
            if not isinstance(channel_id, bool) and str(channel_id).isdigit()
        }
        if channel.id not in disabled:
            return await ctx.send(f"XP gain is already enabled in {channel.mention}")
        conf["disabled_channels"] = sorted(disabled - {channel.id})
        await conf.save()
        await ctx.send(f"Enabled XP gain in {channel.mention}")

    @commands.command(name="reset-xp", description="Resets the servers xp")
    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.guild_only()
    async def reset_xp(self, ctx):
        if ctx.author.id != ctx.guild.owner.id:
            return await ctx.send("Only the server owner can run this")
        confirmed = await GetConfirmation(
            ctx, "Are you sure you want to reset the xp of everyone in the server?"
        )
        if confirmed:
            await self.flush_pending()
            async with self.bot.utils.cursor() as cur:
                await cur.execute(
                    "delete from msg where guild_id = %s;",
                    (ctx.guild.id,)
                )
            self.pending_guild = {
                key: xp for key, xp in self.pending_guild.items()
                if key[0] != ctx.guild.id
            }
            self.xp_cache = {
                key: xp for key, xp in self.xp_cache.items()
                if key[0] != ctx.guild.id
            }
            self.xp_cache_access = {
                key: accessed_at for key, accessed_at in self.xp_cache_access.items()
                if key[0] != ctx.guild.id
            }
            self.leaderboard_cache.pop(ctx.guild.id, None)

    @commands.command(name="give-xp", description="Gives a user additional xp")
    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.cooldown(2, 10, commands.BucketType.channel)
    @commands.guild_only()
    async def give_xp(self, ctx, users: commands.Greedy[discord.Member], amount: int):
        if ctx.author.id != ctx.guild.owner.id:
            return await ctx.send("Only the server owner can run this")
        if not users:
            return await ctx.send("You need to specify at least one user")
        if len(users) > 3:
            return await ctx.send("You can't alter the xp of more than 3 users at a time")
        if amount > 1000000:
            return await ctx.send("You can't give more than 1 million xp at a time")
        if amount <= 0:
            return await ctx.send("The amount must be greater than 0")
        await self.flush_pending()
        async with self.bot.utils.cursor() as cur:
            await cur.executemany(
                "insert into msg (guild_id, user_id, xp) values (%s, %s, %s) "
                "on duplicate key update xp = msg.xp + values(xp);",
                [(ctx.guild.id, user.id, amount) for user in users]
            )
            for user in users:
                key = (ctx.guild.id, user.id)
                self.xp_cache.pop(key, None)
                self.xp_cache_access.pop(key, None)
        self.leaderboard_cache.pop(ctx.guild.id, None)
        await ctx.send(
            f"Gave {', '.join(u.mention for u in users)} {amount} xp",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
        )

    @commands.command(name="remove-xp", description="Removes some of a users xp")
    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.cooldown(2, 10, commands.BucketType.channel)
    @commands.guild_only()
    async def remove_xp(self, ctx, users: commands.Greedy[discord.Member], amount: int):
        if ctx.author.id != ctx.guild.owner.id:
            return await ctx.send("Only the server owner can run this")
        if not users:
            return await ctx.send("You need to specify at least one user")
        if len(users) > 3:
            return await ctx.send("You can't alter the xp of more than 3 users at a time")
        if amount <= 0:
            return await ctx.send("The amount must be greater than 0")
        await self.flush_pending()
        async with self.bot.utils.cursor() as cur:
            for user in users:
                await cur.execute(
                    "update msg set xp = xp - %s "
                    "where guild_id = %s and user_id = %s and xp >= %s;",
                    (amount, ctx.guild.id, user.id, amount)
                )
                if not cur.rowcount:
                    await cur.execute(
                        "delete from msg where guild_id = %s and user_id = %s;",
                        (ctx.guild.id, user.id)
                    )
                key = (ctx.guild.id, user.id)
                self.xp_cache.pop(key, None)
                self.xp_cache_access.pop(key, None)
        self.leaderboard_cache.pop(ctx.guild.id, None)
        await ctx.send(
            f"Removed {amount} xp from {', '.join(u.mention for u in users)}",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
        )

    @commands.command(
        name="profile",
        aliases=["rank"],
        description="Shows your profile or rank card",
        usage=profile_help()
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.guild_only()
    @commands.bot_has_permissions(attach_files=True)
    async def profile(self, ctx):
        """ Profile / Rank Image Card """
        user = ctx.author
        if ctx.message.mentions:
            user = ctx.message.mentions[0]
        user_id = user.id
        guild_id = ctx.guild.id
        conf = await self.config[guild_id] or self.default_config
        global_profile = (
            "global" in ctx.message.content.lower()
            or "profile" in ctx.message.content.lower()
        )

        # config
        title = "Use .set to customize"
        backgrounds = [
            "https://cdn.discordapp.com/attachments/632084935506788385/670258618750337024/unknown.png",  # gold
            "https://media.giphy.com/media/26n6FdRZBIjOCHpJK/giphy.gif",  # spinning blade effect
        ]
        background_url = choice(backgrounds)
        if ctx.guild.splash:
            background_url = str(ctx.guild.splash.url)
        if ctx.guild.banner:
            background_url = str(ctx.guild.banner.url)
        profile = await self.profile[user_id] or {}
        if "title" in profile:
            title = profile["title"]
        if "background" in profile:
            background_url = profile["background"]

        # xp variables
        who = 'You' if user.id == ctx.author.id else 'They'
        async with self.bot.utils.cursor() as cur:
            if global_profile:
                await cur.execute(
                    "select target.xp, "
                    "(select count(*) + 1 from global_msg ranked "
                    "where ranked.xp > target.xp) as user_rank "
                    "from global_msg target where target.user_id = %s limit 1;",
                    (user_id,)
                )
                rank_config = self.default_config
            else:
                await cur.execute(
                    "select target.xp, "
                    "(select count(*) + 1 from msg ranked "
                    "where ranked.guild_id = target.guild_id "
                    "and ranked.xp > target.xp) as user_rank "
                    "from msg target where target.guild_id = %s "
                    "and target.user_id = %s limit 1;",
                    (guild_id, user_id)
                )
                rank_config = conf
            results = await cur.fetchone()

        if not results:
            location = "globally" if global_profile else "in this server"
            return await ctx.send(
                f"{who} currently have no xp {location}, try rerunning this command now"
            )

        total_xp, user_rank = results
        dat = self.calc_lvl_info(total_xp, rank_config)

        base_req = rank_config["first_lvl_xp_req"]
        level = dat["level"]
        xp = dat["progress"]
        max_xp = max(1, base_req if level == 0 else dat["start_to_end"])
        length = max(0, min(1000, 1000 * (xp / max_xp)))
        required = f"Required: {max_xp - xp}"
        progress = f"{xp} / {max_xp} xp"
        misc = f"{progress} | {required}"

        # Prepare the profile card
        status = getattr(self.assets, user.status.name, self.assets.offline)
        try:
            raw_avatar, raw_background = await asyncio.gather(
                self.bot.get_resource(str(user.display_avatar.url)),
                self.bot.get_resource(background_url, label="background")
            )
        except (aiohttp.ClientError, aiohttp.InvalidURL, UnidentifiedImageError, ValueError):
            return await ctx.send(
                "Sorry, but I seem to be having issues using your current background"
                "\nYou can use `.set background` to reset it, or attach a file while "
                "using that command to change it"
            )

        def create_card():
            def process_frame(card, frame):
                frame = frame.convert("RGBA")
                frame = frame.resize((1000, 500), RESAMPLE)
                frame.paste(card, (0, 0), card)
                return frame

            file = BytesIO()
            card = self.assets.rank_card.copy()
            draw = ImageDraw.Draw(card)
            fonts = {}

            # user vanity
            with Image.open(BytesIO(raw_avatar)) as source_avatar:
                avatar = source_avatar.convert("RGBA").resize((175, 175), RESAMPLE)
            avatar = add_corners(avatar, 87)
            card.paste(avatar, (75, 85), avatar)
            draw.ellipse((75, 85, 251, 261), outline="black", width=6)

            card.paste(status, (190, 190), status)

            # leveling / ranking
            rank_text = f"Rank #{user_rank:,}"
            rank_font = fit_font(
                draw, rank_text, fonts, MODERN_FONT,
                max_size=30, max_width=155, min_size=12
            )
            draw.text(
                (985, 85),
                rank_text,
                (255, 255, 255),
                font=rank_font,
                anchor="rt"
            )

            level_text = f"Lvl. {level:,}"
            level_font = fit_font(
                draw, level_text, fonts, MODERN_FONT,
                max_size=100, max_width=430, min_size=35
            )
            draw.text((985, 145), level_text, (0, 0, 0), font=level_font, anchor="rt")

            title_text = str(title)
            title_font = fit_font(
                draw, title_text, fonts, MODERN_FONT,
                max_size=50, max_width=690, min_size=20
            )
            misc_font = fit_font(
                draw, misc, fonts, MODERN_FONT,
                max_size=50, max_width=950, min_size=20
            )
            draw.text((10, 320), title_text, (0, 0, 0), font=title_font)
            draw.text((25, 415), misc, (255, 255, 255), font=misc_font)
            draw.line((0, 495, length, 495), fill=user.color.to_rgb(), width=10)

            # backgrounds and saving
            if not background_url:
                card.save(file, format="PNG")
                return file, "png"

            try:
                background = Image.open(BytesIO(raw_background))
            except UnidentifiedImageError:
                raise IgnoredExit

            # Ordinary image
            if not getattr(background, "is_animated", False):
                background = background.convert("RGBA")
                background = background.resize((1000, 500), RESAMPLE)
                background.paste(card, (0, 0), card)
                background.save(file, format="PNG")
                return file, "png"

            # Keep the size of gifs low
            frame_count = getattr(background, "n_frames", 1)
            frame_step = max(1, (frame_count + 49) // 50)
            duration = int(background.info.get("duration", 100)) * frame_step
            frames = [
                process_frame(card, frame)
                for index, frame in enumerate(ImageSequence.Iterator(background))
                if index % frame_step == 0
            ]

            frames[0].save(
                fp=file,
                save_all=True,
                append_images=frames[1:],
                loop=0,
                duration=duration,
                optimize=False,
                format="GIF"
            )
            return file, "gif"

        try:
            file, ext = await self.bot.loop.run_in_executor(None, create_card)
        except IgnoredExit:
            return await ctx.send("Invalid background. Change it via .set background")
        file.seek(0)
        ty = "Profile" if "profile" in ctx.message.content.lower() else "Rank"
        await ctx.send(f"> **{ty} card for {user}**", file=discord.File(file, filename=f"card.{ext.lower()}"))

    @commands.command(name="top", description="Shows the top 9 ranking users in the server", aliases=["levels"])
    @commands.cooldown(1, 25, commands.BucketType.user)
    @commands.cooldown(1, 25, commands.BucketType.guild)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True, attach_files=True)
    async def top(self, ctx):
        r = await self.bot.fetch(
            f"select user_id, xp from msg "
            f"where guild_id = {ctx.guild.id} order by xp desc limit 50;"
        )
        members = []
        for user_id, xp in r:
            member = ctx.guild.get_member(user_id)
            if member:
                members.append([member, xp])
            if len(members) == 9:
                break

        if not members:
            return await ctx.send("I couldn't find any ranked members in this server")

        conf = await self.config[ctx.guild.id] or self.default_config
        levels = [
            self.calc_lvl_info(xp, conf) for _member, xp in members
        ]
        try:
            avatars = await asyncio.wait_for(
                asyncio.gather(*[
                    self.bot.get_resource(str(member.display_avatar.url))
                    for member, _xp in members
                ], return_exceptions=True),
                timeout=10
            )
        except asyncio.TimeoutError:
            return await ctx.send("Couldn't get all of the avatars for the top 9 users")

        assets = [
            (member, xp, level_data, avatar)
            for (member, xp), level_data, avatar in zip(members, levels, avatars)
            if isinstance(avatar, (bytes, bytearray))
        ]
        if not assets:
            return await ctx.send("Failed to fetch the ranked users' avatars")

        def create_card():
            card = Image.new("RGBA", (750, 900), color=(0, 0, 0, 0))
            fonts = {}
            for i, (member, xp, lvl_dat, asset) in enumerate(assets):
                with Image.open(BytesIO(asset)) as source_avatar:
                    av = source_avatar.convert("RGBA").resize((90, 90), RESAMPLE)
                im = Image.new(
                    mode="RGBA", size=(740, 90), color=(114, 137, 218, 150)
                )
                im.paste(av, (0, 0), av)
                im = add_corners(im, 25)

                if i < 3:
                    ribbon = self.assets.ribbons[i]
                    im.paste(ribbon, (515, 0), ribbon)

                draw = ImageDraw.Draw(im)
                name = member.name[:32]
                name_font = fit_font(
                    draw, name, fonts, ROBOTO_FONT,
                    max_size=45, max_width=390, min_size=20
                )
                draw.text((110, 22), name, (255, 255, 255), font=name_font)

                lvl = str(lvl_dat['level']).rjust(3, " ")
                progress = f"{lvl_dat['progress']}/{lvl_dat['start_to_end']}"

                draw.text(
                    (615, 15), f"Lvl {lvl}", (255, 255, 255),
                    font=get_font(fonts, ROBOTO_FONT, 30)
                )
                draw.text(
                    (620, 55), progress, (255, 255, 255),
                    font=get_font(fonts, ROBOTO_FONT, 20)
                )

                card.paste(im, (5, 100 * i), im)

            card.save(file, format="png")

        file = BytesIO()
        try:
            await self.bot.loop.run_in_executor(None, create_card)
        except (OSError, UnidentifiedImageError):
            return await ctx.send("Failed to fetch one of the top users avatars. Rerunning the command might fix")

        file.seek(0)
        await ctx.send(file=discord.File(file, filename="top.png"))

    def generate_embeds(self, name: str, rankings: list, limit_per_page: int):
        """ Gen a list of embed leaderboards """
        embeds = []

        e = discord.Embed(color=0x4A0E50)
        e.set_author(name=name)
        e.set_thumbnail(url=leaderboard_icon)
        e.description = ""

        rank = 1
        for i, (user_id, xp) in enumerate(rankings):
            user = self.bot.get_user(int(user_id))
            if user:
                normal = normalize('NFKD', user.name).encode('ascii', 'ignore').decode()
                username = normal.replace("`", "'").strip("\\")
            else:
                username = "Unknown-User"

            if i == 0 and user:
                e.set_author(name=name, icon_url=user.display_avatar.url)

            if i and i % limit_per_page == 0:
                embeds.append(e)
                e = discord.Embed(color=0x4A0E50)
                icon_url = user.display_avatar.url if user else None
                e.set_author(name=name, icon_url=icon_url)
                e.set_thumbnail(url=leaderboard_icon)
                e.description = ""

            e.description += f"#{rank}. `{username}` - {int(xp):,}\n"
            rank += 1

        embeds.append(e)
        return embeds

    async def get_leaderboard_data(self, guild_id):
        cached = self.leaderboard_cache.get(guild_id)
        if cached and cached[0] > monotonic():
            return cached[1]

        lock = self.leaderboard_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            cached = self.leaderboard_cache.get(guild_id)
            if cached and cached[0] > monotonic():
                return cached[1]

            cutoff = int(time() - 60 * 60 * 24 * 30)
            query = (
                "select 'guild' as board, user_id, xp from ("
                "select user_id, xp from msg where guild_id = %s "
                "order by xp desc limit 256) guild_rows "
                "union all "
                "select 'global', user_id, xp from ("
                "select user_id, xp from global_msg "
                "order by xp desc limit 256) global_rows "
                "union all "
                "select 'monthly', user_id, total_xp from ("
                "select user_id, sum(xp) as total_xp from monthly_msg "
                "where guild_id = %s and msg_time > %s group by user_id "
                "order by total_xp desc limit 256) monthly_rows "
                "union all "
                "select 'global_monthly', user_id, total_xp from ("
                "select user_id, sum(xp) as total_xp from global_monthly "
                "where timeframe > %s group by user_id "
                "order by total_xp desc limit 256) global_monthly_rows;"
            )
            started_at = monotonic()
            async with self.bot.utils.cursor() as cur:
                await cur.execute(query, (guild_id, guild_id, cutoff, cutoff))
                results = await cur.fetchall()
            self.bot.log.debug(
                f"Fetched ranking leaderboards in "
                f"{round((monotonic() - started_at) * 1000)}ms"
            )

            leaderboards = {
                "Msg Leaderboard": [],
                "Global Msg Leaderboard": [],
                "Monthly Msg Leaderboard": [],
                "Global Monthly Leaderboard": [],
            }
            names = {
                "guild": "Msg Leaderboard",
                "global": "Global Msg Leaderboard",
                "monthly": "Monthly Msg Leaderboard",
                "global_monthly": "Global Monthly Leaderboard",
            }
            for board, user_id, xp in results:
                if isinstance(board, bytes):
                    board = board.decode()
                if name := names.get(board):
                    leaderboards[name].append((user_id, xp))

            self.leaderboard_cache[guild_id] = (
                monotonic() + self.leaderboard_cache_ttl,
                leaderboards
            )
            return leaderboards

    @commands.command(
        name="leaderboard",
        aliases=["lb"],
        description="Ranks everyone in the server"
    )
    @commands.cooldown(2, 5, commands.BucketType.user)
    @commands.cooldown(1, 2, commands.BucketType.channel)
    @commands.cooldown(6, 60, commands.BucketType.guild)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True, manage_messages=True, add_reactions=True)
    async def leaderboard(self, ctx):
        """ Refined leaderboard command """
        async with ctx.typing():
            leaderboards = await self.get_leaderboard_data(ctx.guild.id)

        mapping = {}
        guild_boards = {"Msg Leaderboard", "Monthly Msg Leaderboard"}
        for name, data in leaderboards.items():
            sorted_data = [
                (user_id, xp)
                for user_id, xp in data
                if (
                    ctx.guild.get_member(int(user_id))
                    if name in guild_boards
                    else self.bot.get_user(int(user_id))
                )
            ]
            if not sorted_data:
                continue
            ems = self.generate_embeds(
                name=name,
                rankings=sorted_data,
                limit_per_page=15
            )
            for i, embed in enumerate(ems):
                embed.set_footer(text=f"Page {i + 1}/{len(ems)}")
            mapping[name] = ems

        if not mapping:
            return await ctx.send(
                "Insufficient leaderboard data. Try again after earning some xp"
            )
        await Menu(ctx, mapping)

    @commands.command(name="clb", description="Shows the command leaderboard")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def clb(self, ctx):
        # Command leaderboard
        e = discord.Embed(color=colors.fate)
        e.set_author(name="Command Leaderboard", icon_url=self.bot.user.display_avatar.url)
        e.description = ""
        rank = 1
        async with self.bot.utils.cursor() as cur:
            cutoff = int((
                datetime.now(tz=timezone.utc) - timedelta(days=30)
            ).timestamp())
            await cur.execute(
                "select command, sum(total) as uses "
                "from commands where ran_at >= %s "
                "group by command "
                "order by uses desc "
                "limit 12;",
                (cutoff,)
            )
            results = await cur.fetchall()
        for cmd, uses in results:
            e.description += f"**#{rank}.** `{cmd}` - {uses}\n"
            rank += 1
        await ctx.send(embed=e)

    @commands.command(name="gen-lb", description="Regenerates the cached command leaderboard")
    @commands.is_owner()
    async def generate_leaderboard(self, ctx):
        async def update_embed(m, data):
            """ gen and update the leaderboard embed """
            e = discord.Embed(color=0x4A0E50)
            e.title = "Msg Leaderboard"
            e.description = ""
            rank = 1
            for user_id, xp in sorted(data.items(), reverse=True, key=lambda kv: kv[1]):
                user = self.bot.get_user(int(user_id))
                if not isinstance(user, discord.User):
                    continue
                e.description += f"#{rank}. `{user}` - {xp}\n"
                rank += 1
                if rank == 10:
                    break
            e.set_footer(text=footer)
            await m.edit(embed=e)

        e = discord.Embed()
        e.description = "Starting.."
        m = await ctx.send(embed=e)

        xp = {}
        last_update = time() + 5

        for i, channel in enumerate(ctx.guild.text_channels):
            footer = f"Reading #{channel.name} ({i + 1}/{len(ctx.guild.text_channels)})"
            await update_embed(m, xp)
            last_gain = {}
            bot_counter = 0

            async for msg in channel.history(oldest_first=True, limit=None):
                # skip channels where every msg is a bot
                if msg.author.bot:
                    bot_counter += 1
                    if bot_counter == 1024:
                        break
                    continue
                else:
                    bot_counter = 0

                # init
                user_id = str(msg.author.id)
                if user_id not in xp:
                    xp[user_id] = 0
                if user_id not in last_gain:
                    last_gain[user_id] = None
                if last_gain[user_id]:
                    if (msg.created_at - last_gain[user_id]).total_seconds() < 10:
                        continue

                # update stuff
                last_gain[user_id] = msg.created_at
                xp[user_id] += 1
                if last_update < time():
                    await update_embed(m, xp)
                    last_update = time() + 5

        footer = f"Gen Complete ({len(ctx.guild.text_channels)}/{len(ctx.guild.text_channels)})"
        await update_embed(m, xp)


async def setup(bot):
    await bot.add_cog(Ranking(bot), override=True)
