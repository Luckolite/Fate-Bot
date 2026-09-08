# Fate Control Service

FateControl is the small always-on companion behind the Android and Windows
control surfaces. It runs separately from Discord, supervises `fate.py`, exposes
authenticated health/configuration APIs and the shared responsive panel, and
remains reachable while the bot is off.
Its public status payload also includes host RAM and storage totals plus labeled
breakdowns for Fate, the controller, project data, drive use, and free space.

From the repository root:

```powershell
.\venv\Scripts\python.exe -m apps.FateControl.server
```

On Windows, install the controller as a per-user auto-starting task instead of
relying on an open terminal:

```powershell
.\apps\FateControl\install-control-task.ps1
```

The task starts at sign-in, is restarted by Task Scheduler if its wrapper
fails, and includes a five-minute recovery trigger in case Windows interrupts
the first logon launch. The wrapper relaunches FateControl after an unexpected exit.
If Task Scheduler starts while a terminal-launched FateControl is already
healthy, its hidden wrapper stands by and takes ownership within about five
seconds after that interactive instance exits. A second wrapper still exits
cleanly instead of starting a duplicate controller or entering a port-conflict
loop.

The service listens on all interfaces on port `16421` by default, starts Fate, and writes
the one-time generated control key to `data\fate-control.token`. Enter that key
on the app's Settings page. Device discovery and the public status summary do
not expose the key; every bot or `config.json` change requires it. Set
`FATE_CONTROL_HOST` to a specific private-LAN address if you want to restrict
which interface accepts connections.

## Shared Android and Windows panel

Open `http://127.0.0.1:16421/control` on the bot computer to use the responsive
shared panel.
It provides overview/process controls, registry-driven metric
graphs, recursive configuration and backup editors, Drive folder selection,
and the bounded console. New server metrics and configuration keys appear from
FateControl's responses rather than requiring separate Android and Windows UI
implementations. Large Discord IDs are transported as protected decimal strings
and restored to integers on save so browser number handling cannot round them.
The panel coalesces status and console refreshes, loads heavy sections only when
opened, and revalidates preloaded data before showing it. Backup saves carry the
loaded config revision so two clients cannot silently overwrite each other.

`GET /status` and `/` remain the public health JSON endpoint for scripts and mobile
discovery.

Native clients exchange the bearer key for a one-time, 60-second launch ticket.
The browser receives a private HTTP-only, same-site session cookie; the bearer
key is not placed in the URL or stored by the page. Panel sessions expire after
12 hours or when FateControl restarts. A direct browser launch asks for the
control key once per FateControl session.

Android Settings supports named bot profiles. Each profile keeps its own
FateControl address and separately encrypted control key, so production and test
bots can be switched without re-entering credentials. Existing single-bot
settings migrate into a `Production bot` profile on first launch.
Both native clients accept copied `/control`, `/status`, and API URLs in their
address fields and reduce them to the FateControl server origin.

Build and publish the lightweight Windows launcher with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\apps\FateControl\publish-desktop.ps1
```

The script compiles the launcher first and then replaces the stable
`Desktop\Fate Control.exe` copy after hash verification. Ordinary shared-panel
changes are served by FateControl and do not require rebuilding the executable.
The launcher embeds the same `control_panel_icon.jpg` artwork declared by the
Android app, converted to a multi-size Windows icon during project maintenance.
Publishing requires the .NET 10 SDK; the framework-dependent launcher requires
the .NET 10 runtime. It hosts the shared panel in an embedded WebView2 window. `FATE_CONTROL_DESKTOP_URL` selects another
controller endpoint, while `FATE_CONTROL_TOKEN_PATH` selects an external key
file.

Fate can also be started from PyCharm or another local launcher. FateControl
reports that copy as external: normal Stop remains locked, while an authenticated
Restart can terminate only a verified `fate.py` process from this checkout and
relaunch it under FateControl supervision. That first restart ends the PyCharm
Run session; the replacement process is then visible through FateControl's
console and process controls.

`GET /api/v1/console?lines=250` returns a bounded tail of the supervised bot's
combined stdout and stderr. It requires the same bearer key as process and
configuration controls; the public status response never includes console text.

Authenticated `GET /api/v1/status` also returns live host CPU, OS, uptime,
process, network, disk-I/O, temperature (when exposed by the OS), and Fate
runtime details. `POST /api/v1/bot/action` with `{"action":"restart"}` performs
a supervised restart. Host reboot uses `POST /api/v1/host/action`; it requires
the bearer key, an exact host confirmation value, and the explicit opt-in below.

## Backups and Google Drive

The Android Settings tab contains a collapsible Backups panel backed by
`/api/v1/backups/settings`. Each run creates one restorable ZIP containing a
transaction-safe MySQL dump, compressed MongoDB archive, manifest, and
optionally Fate's local datastore files. `mysqldump` and `mongodump` run as
async subprocesses; directory scanning, ZIP work, and local retention run in
worker threads. Updating the backup policy does not require restarting Fate.

Retention can be limited by age, number of complete archives, aggregate backup
storage, or any combination. Limits apply independently to the local archive
folder and selected Drive folder, and the oldest complete archive is deleted
first. A blank limit disables only that limit.

To link Google Drive:

1. Create a Google OAuth **Web application** client and enable the Google Drive
   API. Register the exact FateControl callback URL, ending in
   `/api/v1/backups/google/callback`. Google requires HTTPS except for loopback
   development callbacks.
2. Save the downloaded client JSON as `data/google-drive-client.json`, or set
   `GOOGLE_DRIVE_CLIENT_ID`, `GOOGLE_DRIVE_CLIENT_SECRET`, and
   `GOOGLE_DRIVE_REDIRECT_URI`.
3. Restart FateControl, open Settings > Backups in the Android app, and tap
   **Link Google Drive**. After Google returns to the app, choose a remote
   folder in the folder browser that appears.

Fate requests Drive access because choosing an existing arbitrary folder and
maintaining oldest-first retention both require listing, uploading, and
deleting backup files there. The refresh token stays on the Fate computer in
`data/google-drive-token.json`; it is never returned to or stored by the phone.
OAuth state is one-time and expires after ten minutes.

## Metrics API

Fate records bounded local telemetry for the Android Metrics tab. All metrics
routes require the FateControl bearer key. Fetch one chart with:

```text
GET /api/v1/metrics?metric=commands&window=1h
GET /api/v1/metrics?metric=commands&window=1h&command=ping
GET /api/v1/metrics/commands/ping?window=1h
```

Supported metrics are `servers`, `users`, `commands`, `mysql_calls`,
`mongo_calls`, `messages_sent`, `discord_rate_limits`,
`discord_global_rate_limits`, `discord_invalid_requests`, `discord_api_blocks`,
`antispam_triggers`, `chatfilter_triggers`,
`dashboard_signins`, `uno_games`, `uno_players`, `connect_four_games`, and
`tictactoe_games`.
Supported windows are `1m`, `5m`, `15m`, `1h`, `6h`, `12h`, `24h`, `7d`, `2w`,
`30d`, `2mo`, `3mo`, `6mo`, and `1y`. Pass an optional whole-number
`bucket_seconds` to customize graph buckets;
FateControl accepts any value large enough to keep the response at 720 points or
fewer, with no multiple-of-five restriction. Five-minute history aggregates are
retained locally for up to one year, subject to the database's bounded row and
size limits, and every individual response series contains at most 720 points.
Timestamps are Unix epoch seconds. Gauge points (`servers` and
`users`) are the last observed value in each bucket; all other point values are
event counts in that bucket. The user gauge is Fate's aggregate guild member
count, matching the overview status rather than an estimate of globally unique
Discord accounts. `dashboard_signins` counts completed Discord OAuth sign-ins;
it stores only aggregate numeric counts, not Discord IDs or account details.
Discord API error metrics also include a ranked normalized-route breakdown. Those
route labels retain channel and message IDs for incident tracing, while query
strings, message content, response bodies, and webhook/interaction tokens are
never collected.

Set `telemetry_path` in each Fate config to place its SQLite telemetry store on
a dedicated disk. `FATE_TELEMETRY_PATH` remains the higher-priority process-wide
override.

The compatibility response shape is:

```json
{
  "metric": "commands",
  "window": "1h",
  "bucket_seconds": 60,
  "points": [{"timestamp": 1786406400, "value": 12}],
  "summary": {
    "current": 12,
    "total": 381,
    "change": 4,
    "change_percent": 50.0
  },
  "command": null,
  "top_commands": [
    {
      "name": "ping",
      "count": 91,
      "share": 23.88,
      "points": [{"timestamp": 1786406400, "value": 3}]
    }
  ],
  "available": true,
  "sampled_at": 1786406460
}
```

`summary.change` compares the first and latest displayed bucket. `total` is the
sum of the selected counter window and the latest value for a gauge. If the
local store is temporarily unavailable, the route remains healthy and returns
the same schema with empty points and `available: false`.

The collector keeps five-second rollups for 26 hours and five-minute rollups
for one year, prunes both by age and a hard row cap, and constrains SQLite to
512 MiB with a bounded WAL. It stores numeric aggregates, registered command
names, and normalized routes for Discord API error incidents. Module graphs
retain only trigger, started-game, and aggregate participant counts; telemetry
never stores message content, SQL/Mongo payloads, secret URL tokens, database
names, or credentials. `FATE_TELEMETRY_PATH` can override the default
`telemetry-<config-name>.sqlite3` file under `datastore_location` when both Fate
and FateControl receive the same override.

Environment options:

- `FATE_CONTROL_TOKEN` supplies the bearer token without a token file.
- `FATE_CONTROL_HOST`, `FATE_CONTROL_PORT` change the listener (defaults:
  `0.0.0.0:16421`).
- `FATE_INSTANCE_NAME` changes the friendly LAN discovery name.
- `DASHBOARD_PORT` changes the supervised bot status port (default `16420`).
- `FATE_ALLOW_HOST_REBOOT=1` enables the Android app's confirmed host-reboot
  action. It is disabled by default. The FateControl service account must also
  have operating-system permission to schedule a reboot.
- `FATE_TELEMETRY_PATH` overrides the shared local SQLite metrics path. Set the
  same absolute path for Fate and FateControl.
- `GOOGLE_DRIVE_CLIENT_ID`, `GOOGLE_DRIVE_CLIENT_SECRET`, and
  `GOOGLE_DRIVE_REDIRECT_URI` override the Google OAuth client file used for
  backup authorization.

These non-secret defaults can also be edited under `control_panel` in
`data/config.json`: `host`, `port`, `instance_name`, `bot_status_host`,
`bot_status_port`, `autostart`, `allow_host_reboot`, and `allowed_ips`.
`allowed_ips` accepts IPv4 or IPv6 CIDR networks and rejects every request whose
direct peer is outside the list; omit it or use an empty list only on a trusted
private network. Environment values win when both are present. Listener and
host-policy changes take effect after the Fate Control Service restarts; the
Android app calls this out separately from a normal Fate bot restart.

Keep TCP `16421` limited to trusted networks in the host firewall or configure a
narrow `allowed_ips` list when the hosting provider exposes only public port
allocations. An IP allowlist does not encrypt HTTP traffic; use a trusted network
or an HTTPS reverse proxy when possible. The service redacts dashboard/session
secrets in the app editor and atomically backs up `config.json` to
`config.json.bak` before each save.

## Checks

From the repository root, run both the core controller suite and the shared-panel
suite:

```powershell
.\venv\Scripts\python.exe -m unittest apps.FateControl.test_server -v
.\venv\Scripts\python.exe -m unittest discover -s apps\FateControl\tests -v
```
