"""
cogs.core.tasks
~~~~~~~~~~~~~~~~

Cog for managing the core bot tasks like it's status, and backups

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import json
import os
import random
import time
import traceback
from io import BytesIO

import discord
import psutil
from discord.ext import commands, tasks

from botutils import split, Cooldown
from botutils.backups import BackupManager, BackupSettings
from botutils.log_paths import DISCORD_LOG_PATH

STORAGE_WARNING_PERCENT = 95
GIBIBYTE = 1024 ** 3


def storage_volume(path: str) -> str:
    """Return a human-readable volume root for a monitored path."""
    absolute_path = os.path.abspath(path)
    drive, _tail = os.path.splitdrive(absolute_path)
    return f"{drive}{os.sep}" if drive else os.path.abspath(os.sep)


def storage_warning_message(path: str, usage) -> str:
    volume = storage_volume(path)
    return (
        f"Storage usage on {volume} has reached {usage.percent:.1f}% "
        f"({usage.free / GIBIBYTE:.1f} GiB free of "
        f"{usage.total / GIBIBYTE:.1f} GiB); Fate path: {os.path.abspath(path)}"
    )


class Tasks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._storage_warning_active = False
        self._backup_manager = BackupManager(bot)
        self._backup_next_at = None
        self._backup_schedule_signature = None
        self.enabled_tasks = [
            self.cog_cleanup,
            self.log_queue,
            self.auto_backup,
            self.cleanup_pool,
            self.warn_if_storage_full,
            self.debug_log,
            self.blacklist
        ]
        for task in self.enabled_tasks:
            task.start()
            bot.log(f"Started {task.coro.__name__}")

    async def cog_unload(self):
        for task in self.enabled_tasks:
            if task.is_running():
                task.cancel()
                self.bot.log.info(f"Cancelled {task.coro.__name__}")
        await self._backup_manager.close()

    def ensure_all(self):
        """Start any core tasks that aren't running"""
        for task in self.enabled_tasks:
            if not task.is_running():
                task.start()
                self.bot.log(f"Started task {task.coro.__name__}", color="cyan")

    @tasks.loop(seconds=60)
    async def warn_if_storage_full(self):
        monitored_path = os.getcwd()
        usage = psutil.disk_usage(monitored_path)
        if usage.percent >= STORAGE_WARNING_PERCENT:
            if not self._storage_warning_active:
                self.bot.log.critical(storage_warning_message(monitored_path, usage))
            self._storage_warning_active = True
        elif self._storage_warning_active:
            self.bot.log.info(
                f"Storage usage on {storage_volume(monitored_path)} recovered to "
                f"{usage.percent:.1f}% ({usage.free / GIBIBYTE:.1f} GiB free)"
            )
            self._storage_warning_active = False

    @tasks.loop(minutes=1)
    async def cog_cleanup(self):
        # Clean the filtered messages index by only keeping recent deletes
        if not hasattr(self.bot, "filtered_messages"):
            self.bot.filtered_messages = {}
        objects_removed = 0
        for guild_id, msgs in list(self.bot.filtered_messages.items()):
            await asyncio.sleep(0)
            for msg_id, deleted_at in list(msgs.items()):
                await asyncio.sleep(0)
                if time.time() - 1800 > deleted_at:
                    del self.bot.filtered_messages[guild_id][msg_id]
                    objects_removed += 1
            if not self.bot.filtered_messages[guild_id]:
                del self.bot.filtered_messages[guild_id]
                objects_removed += 1

        for cog in list(self.bot.cogs.keys()):
            for attr_name in dir(cog):
                await asyncio.sleep(0)
                attr = getattr(cog, attr_name)
                if isinstance(attr, Cooldown):
                    count = len(attr.index)
                    attr.cleanup()
                    objects_removed += count

    @tasks.loop(minutes=1)
    async def blacklist(self):
        async with self.bot.utils.cursor() as cur:
            await cur.execute("select user_id from blocked;")
            blocked_ids = {int(row[0]) for row in await cur.fetchall()}

        for guild in self.bot.guilds:
            owner = guild.owner
            owner_id = guild.owner_id
            if owner_id not in blocked_ids and guild.id not in blocked_ids:
                continue

            ids = BytesIO()
            for member in guild.members:
                ids.write(f"{member} - {member.id}\n".encode())
            ids.seek(0)

            channel = self.bot.get_channel(1042772457062404206)
            await channel.send(
                f"`Logging member IDs for server: {guild.name}`\n "
                f"`owner: {owner}`\n `owner ID: {owner_id}`",
                file=discord.File(ids, filename="member_ids.txt")
            )

            self.bot.log(
                f"Leaving server {guild.name} (owner: {owner}, "
                f"owner ID: {owner_id}) due to it being blacklisted"
            )
            await guild.leave()

    @tasks.loop(hours=1)
    async def cleanup_pool(self):
        if self.bot.pool:
            await self.bot.pool.clear()
            self.bot.log.debug("Cleared the pool")

    @tasks.loop(minutes=1)
    async def prefix_cleanup_task(self):
        uncached = 0
        for guild_id, data in list(self.bot.guild_prefixes.items()):
            await asyncio.sleep(0)
            if isinstance(data, float):
                last_used = data
            else:
                last_used = data[1]
            if last_used > time.time() - 60 * 60:
                del self.bot.guild_prefixes[guild_id]
                uncached += 1
        self.bot.log.debug(f"Removed {uncached} unused prefixes from guild cache")
        uncached = 0

        for user_id, data in list(self.bot.user_prefixes.items()):
            await asyncio.sleep(0)
            if isinstance(data, float):
                last_used = data
            else:
                last_used = data["last_used"]
            if last_used > time.time() - 60 * 60:
                del self.bot.user_prefixes[user_id]
                uncached += 1
        self.bot.log.debug(f"Removed {uncached} unused prefixes from guild cache")

    @tasks.loop()
    async def status_task(self):
        await asyncio.sleep(9)
        while True:
            await asyncio.sleep(1)
            motds = [
                "FBI OPEN UP",
                "YEET to DELETE",
                "Pole-Man",
                "♡Juice wrld♡",
                "Mad cuz Bad",
                "Quest for Cake",
                "Gone Sexual"
            ]
            stages = ["Serendipity", "Euphoria", "Singularity", "Epiphany"]
            for i in range(len(stages)):
                try:
                    await self.bot.change_presence(
                        status=discord.Status.online,
                        activity=discord.Game(name="Seeking For The Clock"),
                    )
                    await asyncio.sleep(45)
                    await self.bot.change_presence(
                        status=discord.Status.online,
                        activity=discord.Game(name=f"{stages[i]} | use .help"),
                    )
                    await asyncio.sleep(15)
                    users = 0
                    for guild in list(self.bot.guilds):
                        await asyncio.sleep(0)
                        users += guild.member_count
                    await self.bot.change_presence(
                        status=discord.Status.idle,
                        activity=discord.Game(
                            name=f"SVR: {len(self.bot.guilds)} USR: {users}"
                        ),
                    )
                    await asyncio.sleep(15)
                    await self.bot.change_presence(
                        status=discord.Status.dnd,
                        activity=discord.Game(
                            name=f"{stages[i]} | {random.choice(motds)}"
                        ),
                    )
                except (
                    discord.errors.Forbidden,
                    discord.errors.HTTPException
                ):
                    self.bot.log(
                        "Error changing my status", "DEBUG", traceback.format_exc()
                    )
                await asyncio.sleep(15)

    @tasks.loop()
    async def debug_log(self):
        await asyncio.sleep(1)
        channel = self.bot.get_channel(self.bot.config["debug_channel"])
        log = []
        reads = 0
        while True:
            await asyncio.sleep(1)
            reads += 1
            async with self.bot.utils.open(str(DISCORD_LOG_PATH), "r") as f:
                lines = await f.readlines()
            new_lines = len(lines) - len(log)
            if new_lines > 0:
                added_lines = lines[-new_lines:]
                log = [*log, *added_lines]
                if self.bot.debug_logging:
                    msg = "".join(added_lines)
                    char = "\u0000"
                    for group in [
                        msg[i : i + 1990] for i in range(0, len(msg), 1990)
                    ]:
                        group = group.replace(char, "")
                        if group:
                            while not channel:
                                channel = self.bot.get_channel(
                                    self.bot.config["debug_channel"]
                                )
                                await asyncio.sleep(5)
                            await channel.send(f"```\n{group}```")
            if reads == 1000:
                async with self.bot.utils.open(str(DISCORD_LOG_PATH), "w") as f:
                    await f.write("")
                log = []
                reads = 0

    @tasks.loop(seconds=1)
    async def log_queue(self):
        await asyncio.sleep(1)
        if not self.bot.is_ready():
            await self.bot.wait_until_ready()
        if not self.bot.logs:
            return
        try:
            channel = self.bot.get_channel(self.bot.config["log_channel"])
            if channel is None:
                channel = await self.bot.fetch_channel(self.bot.config["log_channel"])
        except (discord.HTTPException, OSError, asyncio.TimeoutError) as error:
            self.bot.log.debug(
                f"Deferring {len(self.bot.logs)} queued logs because the log "
                f"channel is temporarily unavailable: {error}"
            )
            return

        pending_logs = list(self.bot.logs)
        message = "```"
        mention = ""
        if any("CRITICAL" in log for log in pending_logs):
            owner = self.bot.get_user(self.bot.config["bot_owner_id"])
            mention = (
                f"{owner.mention} something went terribly wrong"
                if owner
                else "Critical Error\n"
            )
        try:
            for log in pending_logs:  # type: str
                await asyncio.sleep(0)
                mention = ""
                if "CRITICAL" in log:
                    owner = self.bot.get_user(self.bot.config["bot_owner_id"])
                    mention = (
                        f"{owner.mention} something went terribly wrong"
                        if owner
                        else "Critical Error\n"
                    )
                if len(log) >= 2000:
                    for group in split(log, 1990):
                        await channel.send(f"{mention}```{group}```")
                    continue
                if len(message) + len(log) >= 1990:
                    message += "```"
                    await channel.send(mention + message)
                    message = "```"
                message += log + "\n"
            message += "```"
            await channel.send(mention + message)
        except (discord.HTTPException, OSError, asyncio.TimeoutError) as error:
            self.bot.log.debug(
                f"Keeping {len(pending_logs)} unsent logs queued after a "
                f"temporary Discord error: {error}"
            )
            return

        for original in pending_logs:
            try:
                self.bot.logs.remove(original)
            except ValueError:
                pass

    @tasks.loop(seconds=30)
    async def auto_backup(self):
        """Run the current backup policy without blocking Discord's event loop."""
        try:
            refreshed = await asyncio.to_thread(self._read_backup_config)
            if refreshed is not None:
                self.bot.config["backups"] = refreshed
            settings = BackupSettings.from_config(self.bot.config)
            signature = (settings.enabled, settings.frequency_hours)
            now = time.monotonic()
            if not settings.enabled:
                self._backup_next_at = None
                self._backup_schedule_signature = signature
                return
            if signature != self._backup_schedule_signature or self._backup_next_at is None:
                self._backup_schedule_signature = signature
                self._backup_next_at = now + settings.frequency_hours * 3600
                return
            if now < self._backup_next_at:
                return
            self._backup_next_at = now + settings.frequency_hours * 3600
            result = await self._backup_manager.run()
            destination = " and Google Drive" if result.drive_file_id else ""
            self.bot.log.info(
                f"Created {result.path.name} locally{destination} in "
                f"{result.duration_seconds:.1f}s ({result.size / (1024 ** 2):.1f} MiB)"
            )
            for name in (*result.removed_local, *result.removed_remote):
                self.bot.log.info(f"Removed oldest backup {name}")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.bot.log.critical(f"Backup task errored:\n{traceback.format_exc()}")

    def _read_backup_config(self):
        """Read only the live backup section committed by FateControl."""
        path = getattr(self.bot, "config_path", None)
        if path is None:
            return None
        with open(path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
        backups = payload.get("backups") if isinstance(payload, dict) else None
        return backups if isinstance(backups, dict) else None


async def setup(bot):
    await bot.add_cog(Tasks(bot), override=True)
