"""Mount Fate's web dashboard and provide its owner runtime dashboard."""

import asyncio
import os
import platform
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from time import monotonic

import discord
import psutil
from discord.ext import commands

from apps.Dashboard.server import mount_on_bot
from botutils import AuthorView, Cooldown
from botutils.dashboard_image import (
    DashboardCard,
    DashboardPage,
    DashboardRow,
    render_dashboard_pages,
)

DASHBOARD_ASSET_DIR = Path(__file__).resolve().parents[2] / "assets" / "dashboard"
DASHBOARD_BACKGROUND = DASHBOARD_ASSET_DIR / "command-center-background.png"
DASHBOARD_ASSETS = (
    DASHBOARD_ASSET_DIR / "overview.png",
    DASHBOARD_ASSET_DIR / "compute.png",
    DASHBOARD_ASSET_DIR / "resources.png",
    DASHBOARD_ASSET_DIR / "health.png",
)


class DashboardSelect(discord.ui.Select):
    """Switch the attachment shown by an image dashboard."""

    def __init__(self, menu: "DashboardMenu"):
        self.menu = menu
        options = []
        for label in menu.pages:
            emoji, description = menu.option_details[label]
            options.append(
                discord.SelectOption(
                    label=label,
                    emoji=emoji,
                    description=description,
                    default=label == menu.current,
                )
            )
        super().__init__(
            placeholder="Choose a dashboard page",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        self.menu.current = self.values[0]
        self.menu.sync_selection()
        await self.menu.update_message(interaction)


class DashboardMenu(AuthorView):
    """An attachment-only dashboard selector with no embed dependency."""

    option_details = {
        "Overview": ("📊", "Servers, users, and command activity"),
        "System & CPU": ("⚙️", "Host, processor, and sensor details"),
        "Resources": ("💾", "Memory, storage, and network usage"),
        "Runtime Health": ("✅", "Services, extensions, and process status"),
    }

    def __init__(self, ctx, pages: dict[str, bytes]):
        if not pages:
            raise ValueError("DashboardMenu requires at least one image page")
        self.ctx = ctx
        self.pages = pages
        self.current = next(iter(pages))
        self.message = None
        self.cd = Cooldown(3, 5)
        super().__init__(timeout=120)
        self.selector = DashboardSelect(self)
        self.add_item(self.selector)

    def page_file(self):
        slug = self.current.lower().replace(" & ", "-").replace(" ", "-")
        return discord.File(
            BytesIO(self.pages[self.current]),
            filename=f"fate-dashboard-{slug}.png",
        )

    def sync_selection(self):
        for option in self.selector.options:
            option.default = option.label == self.current

    def __await__(self):
        return self._init_message().__await__()

    async def _init_message(self):
        self.message = await self.ctx.send(view=self, file=self.page_file())
        return self.message

    async def update_message(self, interaction):
        await interaction.response.edit_message(
            view=self,
            attachments=[self.page_file()],
        )

    async def on_timeout(self):
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(view=None)


def cpu_model_name() -> str:
    """Return the most descriptive CPU name available on this host."""
    if platform.system() == "Windows":
        try:
            import winreg

            path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                value, _kind = winreg.QueryValueEx(key, "ProcessorNameString")
            if value.strip():
                return " ".join(value.split())
        except (OSError, ImportError):
            pass
    elif platform.system() == "Linux":
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith(("model name", "hardware")):
                    value = line.partition(":")[2].strip()
                    if value:
                        return value
        except OSError:
            pass
    return platform.processor() or platform.machine() or "Unknown CPU"


def human_bytes(value: int) -> str:
    """Format a byte count without depending on platform-specific units."""
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if amount < 1024 or unit == "PiB":
            precision = 0 if unit == "B" else 1
            return f"{amount:.{precision}f} {unit}"
        amount /= 1024


def human_duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def command_count(commands_) -> int:
    """Count top-level and nested application commands."""
    total = 0
    pending = list(commands_)
    while pending:
        command = pending.pop()
        total += 1
        pending.extend(getattr(command, "commands", ()))
    return total


def canonical_extension_name(name: str) -> str:
    """Normalize configured and discord.py extension names for comparison."""
    return f"cogs.{name.removeprefix('cogs.')}"


def collect_system_snapshot() -> dict:
    """Collect blocking psutil values away from the Discord event loop."""
    process = psutil.Process(os.getpid())
    with process.oneshot():
        memory = process.memory_info()
        try:
            full_memory = process.memory_full_info()
        except (psutil.AccessDenied, NotImplementedError):
            full_memory = memory
        process_data = {
            "rss": memory.rss,
            "vms": memory.vms,
            "uss": getattr(full_memory, "uss", None),
            "percent": process.memory_percent(),
            "threads": process.num_threads(),
            "cpu": process.cpu_percent(interval=None),
        }
        handle_method = getattr(process, "num_handles", None) or getattr(
            process, "num_fds", None
        )
        try:
            process_data["handles"] = handle_method() if handle_method else None
        except (psutil.Error, OSError):
            process_data["handles"] = None

    # A short measured sample is much more useful than psutil's first-call 0.0.
    host_cpu = psutil.cpu_percent(interval=0.2, percpu=True)
    process_data["cpu"] = process.cpu_percent(interval=None)
    virtual_memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    frequency = psutil.cpu_freq()

    disks = []
    seen = set()
    for partition in psutil.disk_partitions(all=False):
        key = (partition.device, partition.mountpoint)
        if key in seen:
            continue
        seen.add(key)
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (PermissionError, OSError, psutil.Error):
            continue
        disks.append(
            {
                "device": partition.device or "Device",
                "mount": partition.mountpoint,
                "used": usage.used,
                "total": usage.total,
                "percent": usage.percent,
            }
        )
    disks.sort(key=lambda item: item["used"], reverse=True)

    network = psutil.net_io_counters()
    interfaces = []
    interface_counters = psutil.net_io_counters(pernic=True)
    interface_stats = psutil.net_if_stats()
    for name, counters in interface_counters.items():
        stats = interface_stats.get(name)
        if stats and not stats.isup:
            continue
        total = counters.bytes_sent + counters.bytes_recv
        interfaces.append(
            {
                "name": name,
                "sent": counters.bytes_sent,
                "received": counters.bytes_recv,
                "speed": stats.speed if stats else 0,
                "total": total,
            }
        )
    interfaces.sort(key=lambda item: item["total"], reverse=True)

    try:
        temperatures = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        temperatures = {}
    hottest = []
    for device, readings in temperatures.items():
        valid = [reading for reading in readings if reading.current is not None]
        if valid:
            reading = max(valid, key=lambda item: item.current)
            hottest.append((device, reading.label or device, reading.current))
    hottest.sort(key=lambda item: item[2], reverse=True)
    try:
        battery = psutil.sensors_battery()
    except (AttributeError, OSError):
        battery = None
    return {
        "process": process_data,
        "cpu_per_core": host_cpu,
        "cpu_average": sum(host_cpu) / len(host_cpu) if host_cpu else 0,
        "cpu_logical": psutil.cpu_count() or 0,
        "cpu_physical": psutil.cpu_count(logical=False) or 0,
        "cpu_frequency": frequency.current if frequency else None,
        "memory": virtual_memory,
        "swap": swap,
        "disks": disks,
        "network": network,
        "interfaces": interfaces,
        "temperatures": hottest,
        "battery": battery,
        "cpu_model": cpu_model_name(),
        "boot_time": psutil.boot_time(),
    }


class WebDashboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dashboard = mount_on_bot(bot)

    async def command_activity(self, now: datetime) -> dict:
        """Load global command totals and leaders, tolerating DB reconnects."""
        empty = {
            "day": None,
            "week": None,
            "month": None,
            "thirty_days": None,
            "top": [],
        }
        if not self.bot.pool:
            return empty

        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        cutoffs = {
            "day": int((now - timedelta(days=1)).timestamp()),
            "week": int((now - timedelta(days=7)).timestamp()),
            "month": int(month_start.timestamp()),
            "thirty_days": int((now - timedelta(days=30)).timestamp()),
        }
        try:
            async with self.bot.utils.cursor() as cursor:
                await cursor.execute(
                    "select "
                    "coalesce(sum(case when ran_at >= %s then total else 0 end), 0), "
                    "coalesce(sum(case when ran_at >= %s then total else 0 end), 0), "
                    "coalesce(sum(case when ran_at >= %s then total else 0 end), 0), "
                    "coalesce(sum(case when ran_at >= %s then total else 0 end), 0) "
                    "from commands where ran_at >= %s;",
                    (
                        cutoffs["day"],
                        cutoffs["week"],
                        cutoffs["month"],
                        cutoffs["thirty_days"],
                        min(cutoffs.values()),
                    ),
                )
                row = await cursor.fetchone()
                await cursor.execute(
                    "select command, sum(total) as uses from commands "
                    "where ran_at >= %s group by command order by uses desc limit 8;",
                    (cutoffs["month"],),
                )
                top = list(await cursor.fetchall())
        except Exception as error:
            self.bot.log.warning(f"Owner dashboard command stats unavailable: {error}")
            return empty

        return {
            "day": int(row[0]),
            "week": int(row[1]),
            "month": int(row[2]),
            "thirty_days": int(row[3]),
            "top": top,
        }

    @commands.command(
        name="dashboard",
        aliases=["owner-dashboard", "sysdash"],
        description="Shows Fate's detailed private runtime dashboard",
    )
    @commands.is_owner()
    @commands.bot_has_permissions(attach_files=True)
    @commands.max_concurrency(1, commands.BucketType.default, wait=False)
    async def owner_dashboard(self, ctx):
        """Show detailed Discord, command, host, and process health."""
        started = monotonic()
        now = datetime.now(tz=timezone.utc)
        snapshot_future = asyncio.to_thread(collect_system_snapshot)
        activity_future = self.command_activity(now)
        snapshot, activity = await asyncio.gather(snapshot_future, activity_future)

        guilds = list(self.bot.guilds)
        total_members = sum(guild.member_count or 0 for guild in guilds)
        cached_users = list(self.bot.users)
        bots = sum(user.bot for user in cached_users)
        text_channels = sum(len(guild.text_channels) for guild in guilds)
        voice_channels = sum(len(guild.voice_channels) for guild in guilds)
        active_voice = sum(
            len(channel.members)
            for guild in guilds
            for channel in (
                *guild.voice_channels,
                *getattr(guild, "stage_channels", ()),
            )
        )
        emojis = sum(len(guild.emojis) for guild in guilds)
        latency = self.bot.latency
        latency_text = (
            "Unavailable" if latency != latency else f"{latency * 1000:.0f} ms"
        )
        bot_started = getattr(self.bot, "start_time", now)
        uptime = human_duration((now - bot_started).total_seconds())

        prefix_commands = len(self.bot.commands)
        app_commands = command_count(self.bot.tree.get_commands())
        activity_rows = (
            (DashboardRow("Database", "Unavailable", tone="warn"),)
            if activity["month"] is None
            else (
                DashboardRow("Last 24 hours", f"{activity['day']:,}"),
                DashboardRow("Last 7 days", f"{activity['week']:,}"),
                DashboardRow("This month", f"{activity['month']:,}"),
                DashboardRow("Rolling 30 days", f"{activity['thirty_days']:,}"),
            )
        )
        top_command_rows = tuple(
            DashboardRow(f"{index}. {name}", f"{uses:,}")
            for index, (name, uses) in enumerate(activity["top"][:5], 1)
        ) or (DashboardRow("This month", "No usage recorded", tone="muted"),)
        largest_guilds = sorted(
            guilds, key=lambda guild: guild.member_count or 0, reverse=True
        )[:5]
        top_server_rows = tuple(
            DashboardRow(
                f"{index}. {guild.name}", f"{guild.member_count or 0:,} members"
            )
            for index, guild in enumerate(largest_guilds, 1)
        ) or (DashboardRow("Cache", "No servers", tone="muted"),)

        cpu = snapshot["cpu_per_core"]
        per_core_rows = []
        for start in range(0, min(len(cpu), 16), 4):
            group = cpu[start : start + 4]
            per_core_rows.append(
                DashboardRow(
                    f"C{start}–C{start + len(group) - 1}",
                    " · ".join(f"{value:.0f}%" for value in group),
                    max(group, default=0),
                )
            )
        if len(cpu) > 16:
            per_core_rows.append(
                DashboardRow("Additional cores", f"{len(cpu) - 16} not shown", tone="muted")
            )
        if not per_core_rows:
            per_core_rows.append(DashboardRow("Core data", "Unavailable", tone="muted"))

        sensor_rows = [
            DashboardRow(f"{device} / {label}", f"{temperature:.1f} °C")
            for device, label, temperature in snapshot["temperatures"][:4]
        ]
        battery = snapshot["battery"]
        if battery:
            power = "plugged in" if battery.power_plugged else "on battery"
            sensor_rows.append(
                DashboardRow(
                    "Battery",
                    f"{battery.percent:.0f}% · {power}",
                    battery.percent,
                )
            )
        if not sensor_rows:
            sensor_rows.append(DashboardRow("Sensors", "Unavailable", tone="muted"))

        memory = snapshot["memory"]
        swap = snapshot["swap"]
        proc = snapshot["process"]
        process_rows = [
            DashboardRow("RSS / physical", human_bytes(proc["rss"])),
            DashboardRow("VMS / virtual", human_bytes(proc["vms"])),
        ]
        if proc["uss"] is not None:
            process_rows.append(DashboardRow("USS / private", human_bytes(proc["uss"])))
        process_rows.extend(
            (
                DashboardRow("Host share", f"{proc['percent']:.2f}%", proc["percent"]),
                DashboardRow(
                    "Threads / handles",
                    f"{proc['threads']:,} / {proc['handles']:,}"
                    if proc["handles"] is not None
                    else f"{proc['threads']:,} / unavailable",
                ),
            )
        )

        disk_rows = [
            DashboardRow(
                f"{disk['device']} · {disk['mount']}",
                f"{human_bytes(disk['used'])} / {human_bytes(disk['total'])}",
                disk["percent"],
            )
            for disk in snapshot["disks"][:5]
        ] or [DashboardRow("Mounted storage", "Unavailable", tone="muted")]

        network = snapshot["network"]
        interface_rows = []
        for interface in snapshot["interfaces"][:5]:
            speed = f" · {interface['speed']:,} Mbps" if interface["speed"] else ""
            interface_rows.append(
                DashboardRow(
                    f"{interface['name']}{speed}",
                    f"↑ {human_bytes(interface['sent'])} / ↓ {human_bytes(interface['received'])}",
                )
            )
        if not interface_rows:
            interface_rows.append(DashboardRow("Adapters", "Unavailable", tone="muted"))

        configured = self.bot.configured_extensions()
        loaded = tuple(self.bot.extensions)
        configured_names = {canonical_extension_name(name) for name in configured}
        loaded_names = {canonical_extension_name(name) for name in loaded}
        missing = sorted(configured_names - loaded_names)
        discord_ready = self.bot.is_ready()
        database_ready = self.bot.pool is not None and not getattr(
            self.bot.pool, "closed", False
        )
        web_ready = self.bot.app_is_running
        health_issues = [
            label
            for label, ready in (
                ("Discord", discord_ready),
                ("Database", database_ready),
                ("Web API", web_ready),
                ("Extensions", not missing),
            )
            if not ready
        ]
        healthy = not health_issues
        health_heading = (
            "Everything looks healthy"
            if healthy
            else f"Waiting for: {', '.join(health_issues)}"
        )
        snapshot_ms = (monotonic() - started) * 1000
        pages = (
            DashboardPage(
                slug="overview",
                label="Overview",
                title="Owner Dashboard",
                subtitle="Discord reach, community surface, and command activity at a glance.",
                status="Gateway ready" if self.bot.is_ready() else "Gateway starting",
                status_tone="good" if self.bot.is_ready() else "warn",
                accent=(121, 100, 255),
                icon_path=DASHBOARD_ASSETS[0],
                cards=(
                    DashboardCard(
                        "Discord Reach",
                        (
                            DashboardRow("Servers", f"{len(guilds):,}"),
                            DashboardRow("Member slots", f"{total_members:,}"),
                            DashboardRow("Unique cached users", f"{len(cached_users):,}"),
                            DashboardRow(
                                "Cached humans / bots",
                                f"{len(cached_users) - bots:,} / {bots:,}",
                            ),
                        ),
                    ),
                    DashboardCard(
                        "Discord Surface",
                        (
                            DashboardRow(
                                "Text / voice channels",
                                f"{text_channels:,} / {voice_channels:,}",
                            ),
                            DashboardRow("Users in voice", f"{active_voice:,}"),
                            DashboardRow("Custom emojis", f"{emojis:,}"),
                            DashboardRow("Shards", f"{self.bot.shard_count or 1:,}"),
                        ),
                    ),
                    DashboardCard("Command Activity", activity_rows),
                    DashboardCard("Top Commands · Month", top_command_rows),
                    DashboardCard("Largest Servers", top_server_rows),
                    DashboardCard(
                        "Gateway & Commands",
                        (
                            DashboardRow(
                                "State",
                                "Ready" if self.bot.is_ready() else "Starting",
                                tone="good" if self.bot.is_ready() else "warn",
                            ),
                            DashboardRow("Latency", latency_text),
                            DashboardRow("Fate uptime", uptime),
                            DashboardRow("Started", bot_started.strftime("%Y-%m-%d %H:%M UTC")),
                            DashboardRow(
                                "Registered · prefix / app",
                                f"{prefix_commands:,} / {app_commands:,}",
                            ),
                        ),
                    ),
                ),
                footer=f"Page 1 of 4  •  Updated {now:%Y-%m-%d %H:%M:%S} UTC  •  Command periods use UTC  •  Owner only",
            ),
            DashboardPage(
                slug="system-cpu",
                label="System & CPU",
                title="System & CPU",
                subtitle="Host identity, processor details, and a live per-core usage sample.",
                status=f"CPU {snapshot['cpu_average']:.1f}%",
                status_tone="warn" if snapshot["cpu_average"] >= 85 else "good",
                accent=(0, 194, 255),
                icon_path=DASHBOARD_ASSETS[1],
                cards=(
                    DashboardCard(
                        "Host",
                        (
                            DashboardRow("OS", f"{platform.system()} {platform.release()}"),
                            DashboardRow("Architecture", platform.machine() or "Unknown"),
                            DashboardRow("Hostname", platform.node() or "Unknown"),
                            DashboardRow(
                                "Host uptime",
                                human_duration(now.timestamp() - snapshot["boot_time"]),
                            ),
                        ),
                    ),
                    DashboardCard(
                        "CPU Summary",
                        (
                            DashboardRow(
                                "Average",
                                f"{snapshot['cpu_average']:.1f}%",
                                snapshot["cpu_average"],
                            ),
                            DashboardRow("Fate process", f"{proc['cpu']:.1f}%"),
                            DashboardRow(
                                "Physical / logical",
                                f"{snapshot['cpu_physical']} / {snapshot['cpu_logical']}",
                            ),
                            DashboardRow(
                                "Current clock",
                                f"{snapshot['cpu_frequency']:.0f} MHz"
                                if snapshot["cpu_frequency"] is not None
                                else "Unavailable",
                            ),
                        ),
                    ),
                    DashboardCard(
                        "Processor & Runtime",
                        (
                            DashboardRow("Processor", snapshot["cpu_model"]),
                            DashboardRow("Python", platform.python_version()),
                            DashboardRow("discord.py", discord.__version__),
                        ),
                    ),
                    DashboardCard("Per-core Load", tuple(per_core_rows)),
                    DashboardCard("Sensors & Power", tuple(sensor_rows)),
                ),
                footer=f"Page 2 of 4  •  {snapshot_ms:.0f} ms sample  •  Python {platform.python_version()} / discord.py {discord.__version__}  •  Owner only",
            ),
            DashboardPage(
                slug="resources",
                label="Resources",
                title="Resources",
                subtitle="Memory, mounted storage, and active network traffic, ordered by usage.",
                status=f"Memory {memory.percent:.1f}%",
                status_tone="warn" if memory.percent >= 85 else "good",
                accent=(45, 212, 168),
                icon_path=DASHBOARD_ASSETS[2],
                cards=(
                    DashboardCard(
                        "Host Memory",
                        (
                            DashboardRow(
                                "Used",
                                f"{human_bytes(memory.used)} / {human_bytes(memory.total)}",
                                memory.percent,
                            ),
                            DashboardRow("Available", human_bytes(memory.available)),
                            DashboardRow(
                                "Swap",
                                f"{human_bytes(swap.used)} / {human_bytes(swap.total)}",
                                swap.percent,
                            ),
                        ),
                    ),
                    DashboardCard("Fate Process", tuple(process_rows)),
                    DashboardCard("Mounted Storage", tuple(disk_rows)),
                    DashboardCard(
                        "Network · Since Boot",
                        (
                            DashboardRow(
                                "Sent / received",
                                f"{human_bytes(network.bytes_sent)} / {human_bytes(network.bytes_recv)}",
                            ),
                            DashboardRow(
                                "Packets sent / received",
                                f"{network.packets_sent:,} / {network.packets_recv:,}",
                            ),
                            DashboardRow(
                                "Errors in / out", f"{network.errin:,} / {network.errout:,}"
                            ),
                        ),
                    ),
                    DashboardCard("Active Network Adapters", tuple(interface_rows)),
                ),
                footer=f"Page 3 of 4  •  Updated {now:%H:%M:%S} UTC  •  Storage and adapters show highest usage first  •  Owner only",
            ),
            DashboardPage(
                slug="runtime-health",
                label="Runtime Health",
                title="Runtime Health",
                subtitle=f"{health_heading}. Current service, extension, and process status.",
                status="All systems healthy" if healthy else "Check status",
                status_tone="good" if healthy else "warn",
                accent=(87, 242, 135) if healthy else (240, 166, 74),
                icon_path=DASHBOARD_ASSETS[3],
                cards=(
                    DashboardCard(
                        "Services",
                        (
                            DashboardRow(
                                "Discord",
                                "Connected" if discord_ready else "Starting",
                                tone="good" if discord_ready else "warn",
                            ),
                            DashboardRow(
                                "Database",
                                "Connected" if database_ready else "Unavailable",
                                tone="good" if database_ready else "warn",
                            ),
                            DashboardRow(
                                "Web API",
                                "Running" if web_ready else "Stopped",
                                tone="good" if web_ready else "warn",
                            ),
                            DashboardRow("Instance", self.bot.instance_role.title()),
                            DashboardRow(
                                "Debug mode", "On" if self.bot.debug_mode else "Off"
                            ),
                            DashboardRow(
                                "Debug logging",
                                "On" if self.bot.debug_logging else "Off",
                            ),
                        ),
                    ),
                    DashboardCard(
                        "Extensions",
                        (
                            DashboardRow(
                                "Loaded / configured", f"{len(loaded):,} / {len(configured):,}"
                            ),
                            DashboardRow("Cogs", f"{len(self.bot.cogs):,}"),
                            DashboardRow(
                                "Missing",
                                f"{len(missing):,}" if not missing else ", ".join(missing),
                                tone="good" if not missing else "warn",
                            ),
                        ),
                    ),
                    DashboardCard(
                        "Process",
                        (
                            DashboardRow("PID", f"{os.getpid():,}"),
                            DashboardRow("Working directory", str(Path.cwd())),
                            DashboardRow("Snapshot time", f"{snapshot_ms:.0f} ms"),
                            DashboardRow("Fate uptime", uptime),
                        ),
                    ),
                    DashboardCard(
                        "Invocation Server" if ctx.guild else "Invocation Context",
                        (
                            (
                                DashboardRow("Name / ID", f"{ctx.guild.name} / {ctx.guild.id}"),
                                DashboardRow("Members", f"{ctx.guild.member_count or 0:,}"),
                                DashboardRow("Owner", ctx.guild.owner or "Unavailable"),
                            )
                            if ctx.guild
                            else (
                                DashboardRow("Location", "Direct message"),
                                DashboardRow("Invoker", str(ctx.author)),
                            )
                        ),
                    ),
                ),
                footer=f"Page 4 of 4  •  Updated {now:%Y-%m-%d %H:%M:%S} UTC  •  {'Healthy' if healthy else 'Attention recommended'}  •  Owner only",
            ),
        )
        rendered_pages = await asyncio.to_thread(
            render_dashboard_pages, pages, DASHBOARD_BACKGROUND
        )
        await DashboardMenu(ctx, rendered_pages)


async def setup(bot):
    await bot.add_cog(WebDashboard(bot), override=True)
