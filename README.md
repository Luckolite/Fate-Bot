# Fate

Fate is a self-hosted Discord bot and management stack. The repository contains
the bot runtime, a public website and OAuth server dashboard, an always-on LAN
controller, a native Android Mission Control app, and isolated local database
services for development.

The current runtime is built on Python 3.10+ and discord.py 2.7. It supports
traditional prefix commands, application commands, server-installed commands,
and user-installed commands in DMs and private channels.

## What is included

| Component | Purpose | Default listener |
| --- | --- | --- |
| `fate.py` | Discord bot, command runtime, status API, and mounted dashboard | `127.0.0.1:16420` |
| `apps/Dashboard` | Public command catalog and Discord OAuth server control panel | Mounted at `16420`, or standalone at `127.0.0.1:8080` |
| `apps/FateControl` | Always-on bot supervisor, authenticated API, and shared web/Windows control panel | `0.0.0.0:16421` |
| `apps/FateStatus` | Native Android Mission Control app, shared Panel tab, and home-screen widgets | Connects to `16420`/`16421` |
| `apps/DevServices` | Isolated loopback MySQL and MongoDB services for local testing | MySQL `3307`, MongoDB `27018` |

Fate's enabled modules cover moderation and audit logging, anti-spam and
anti-raid protection, verification, modmail, configurable welcome/farewell and
role automation, XP and leaderboards, self-serve roles, utilities, and games.

The dashboard manages the same live MongoDB-backed server settings used by the
bot. Its module workspace includes verification, automatic roles, chat filter,
Logging, modmail, spam and raid protection, welcome/farewell messages, Restore
Roles, Self-Serve Roles, and Voice Activity Log.

## Repository layout

```text
apps/                 Dashboard, FateControl, Android app, and dev services
assets/               Runtime artwork
botutils/             Shared runtime, localization, storage, and renderer code
checks/               Command checks, exceptions, and regression scripts
cogs/                 Discord extensions grouped by feature category
data/                  Runtime configuration and local data
tests/                 Bot-level regression tests
fate.py                Main Discord process
PRIVACY.md             User-data and optional feature disclosures
```

## Requirements

- Python 3.10 or newer
- MySQL and MongoDB, or the isolated Windows development services included in
  `apps/DevServices`
- A Discord application and bot token
- Windows PowerShell for the bundled service launchers
- JDK 17+ and Android SDK 35 only when building the Android app
- .NET 10 SDK/runtime only when building or running the lightweight Windows
  Fate Control launcher

Install the Python dependencies in a virtual environment:

```powershell
git clone https://github.com/Villagers654/Fate.git
cd Fate
py -3.10 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

Voice support is not part of the configured production extension set, so the
runtime intentionally installs discord.py without its voice extra.

## Fastest local development setup

The development services create separate `fate_test` databases and generated
test configuration without changing the production profile.

1. Put only the test bot token in `%USERPROFILE%\Desktop\token.txt`, or set
   `FATE_TOKEN_PATH` to another external file.
2. Start and verify the local databases:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\apps\DevServices\start-databases.ps1
   .\venv\Scripts\python.exe .\apps\DevServices\smoke-test.py
   .\venv\Scripts\python.exe .\apps\DevServices\offline-bot-probe.py
   ```

3. Run the isolated test bot:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\apps\DevServices\run-test-bot.ps1
   ```

Stop the local databases with:

```powershell
powershell -ExecutionPolicy Bypass -File .\apps\DevServices\stop-databases.ps1
```

The database processes run in the background without console windows. The
PowerShell launcher can be closed after it reports that both services are ready.

Generated database binaries, credentials, data, and logs remain under
`.fate-test-services` and are ignored by Git.

> **Profile selection:** if `%USERPROFILE%\Desktop\token.txt` exists and
> `FATE_CONFIG_PATH` is not set, a direct `python fate.py` launch intentionally
> selects `data/config.test.json` and `data/auth.test.json`. Remove that file or
> set the paths explicitly when launching the production profile.

## Production configuration

Fate normally reads:

- `data/config.json` for bot identity, enabled extensions, permissions,
  dashboard settings, and datastore locations
- `data/auth.json` for database credentials, token mappings, and integrations
- the token selected by `token_id`, unless an external token file is configured

The global config also exposes the optional runtime policies used by Mission
Control: public `mysql` connection/pool settings, `local_databases.auto_start`,
the unified `backups` schedule/retention/Drive destination,
the `translation` provider and timeout, the mounted `website`
listener/OAuth/proxy settings, the `control_panel` listener and supervision
policy, and the local `organism` service limits. The MySQL password remains in
encrypted auth storage rather than the editable global config.
Environment variables remain higher-priority overrides, so deployment secrets
do not need to live in `config.json`.

Important environment overrides are:

| Variable | Purpose |
| --- | --- |
| `FATE_CONFIG_PATH` | Select another bot configuration file |
| `FATE_AUTH_PATH` | Select another plaintext or Fernet-encrypted auth file |
| `FATE_TOKEN_PATH` | Read the Discord token from an external file |
| `FATE_AUTH_KEY` | Supply the Fernet key for encrypted auth data |
| `FATE_AUTH_KEY_PATH` | Read the Fernet key from a file; defaults to `Desktop/fate_auth.key` |
| `FATE_AUTO_START_DATABASES` | Set to `0` to disable loopback database startup preflight |
| `FATE_CONTROL_HOST`, `FATE_CONTROL_PORT` | Override the FateControl listener configured under `control_panel` |
| `FATE_INSTANCE_NAME` | Override the friendly FateControl discovery name |
| `FATE_BOT_STATUS_HOST` | Override the bot status address used by FateControl |
| `FATE_ALLOW_HOST_REBOOT` | Override the `control_panel.allow_host_reboot` safety switch |
| `GOOGLE_DRIVE_CLIENT_ID`, `GOOGLE_DRIVE_CLIENT_SECRET`, `GOOGLE_DRIVE_REDIRECT_URI` | Override the Google OAuth web-client settings used for remote backups |
| `DASHBOARD_HOST`, `DASHBOARD_PORT` | Override the bot status/dashboard listener |

When configured MySQL or MongoDB endpoints are on loopback and unavailable,
Fate can start the project-local services before loading database state. Remote
database hosts are never started automatically.

Never commit bot tokens, `auth.json`, Fernet keys, OAuth client secrets,
database passwords, `fate-control.token`, or generated test credentials.

## Running Fate and automatic instance handoff

For a direct foreground launch:

```powershell
.\venv\Scripts\python.exe .\fate.py
```

Instances are coordinated per configuration file. If a newer process starts
with the same profile, it asks the current process to log out of Discord and
close its database clients, background tasks, and HTTP listener. The newcomer
waits for that clean shutdown before continuing. Different production and test
profiles can still run side by side.

`debug_mode` controls whether Fate uses its secondary behavior. When it is
`true`, Fate skips primary-only extensions (`polis`, `dev`, and `backup`),
ignores Top.gg vote events, and removes primary-only vote/reward links. The
active role and debug-mode state are exposed by the status API and dashboard.

## Always-on control and Android Mission Control

For normal Windows operation, run Fate through FateControl. The controller
survives bot shutdowns, restores failed bot processes, recognizes externally
started replacements, and provides the Android app's real Start/Stop control.
An authenticated Restart can also stop a verified `fate.py` launched from
PyCharm and relaunch it under FateControl supervision; ordinary Stop remains
locked while another launcher owns the process.

Install and start the per-user scheduled task:

```powershell
powershell -ExecutionPolicy Bypass -File .\apps\FateControl\install-control-task.ps1
```

FateControl listens on TCP `16421`, starts Fate, and creates a one-time bearer
key at `data/fate-control.token`. Enter that key in the Android app's Settings
screen. Public discovery and status responses do not expose the key; process,
console, and configuration operations require it.

Mission Control provides:

- parallel LAN discovery for FateControl and status-only Fate instances
- online state, counts, latency, uptime, RAM/storage breakdowns, and health
- authenticated Start/Stop, bot restart, opt-in host reboot, and Save & Restart
  controls
- one responsive control panel shared by the Android Panel tab and the
  lightweight Windows desktop launcher, with one-time launch tickets and an
  HTTP-only session cookie instead of exposing the bearer key to the page
- a bounded live console with follow mode, traceback highlighting, and copy
- structured current-config editing with dirty state and revision conflicts
- backup scheduling/retention controls and optional Google Drive folder linking
- compact General/Modules metric graphs with arbitrary bounded whole-second
  buckets, up to 720 displayed samples, and persisted card ordering
- resizable status, memory, and console widgets
- Android Keystore-backed control-key storage

Keep `16421` restricted to trusted local networks. Keep the bot's `16420`
listener on loopback unless it is behind a correctly configured HTTPS reverse
proxy. See [FateControl](apps/FateControl/README.md) and
[Fate Mission Control](apps/FateStatus/README.md) for complete setup details.

## Dashboard

The mounted dashboard provides a public command catalog and an authenticated
Discord OAuth control panel. OAuth requests only `identify` and `guilds`, and
protected operations recheck live Discord ownership or Manage Server access.

For standalone local development:

```powershell
copy apps\Dashboard\.env.example apps\Dashboard\.env
.\venv\Scripts\python.exe -m apps.Dashboard.server
```

Open `http://localhost:8080`. Set `DASHBOARD_DEV_MODE=1` for an explicit
database-free local UI preview. This demo mode is rejected when the dashboard
is mounted inside Fate.

Production deployments should terminate HTTPS at a reverse proxy, use secure
cookies, register the exact Discord OAuth callback, and narrowly configure
trusted proxy addresses. See the [dashboard documentation](apps/Dashboard/README.md).

## Localization and privacy-sensitive features

Server administrators can select English, Spanish, French, Simplified Chinese,
Traditional Chinese, Portuguese, Arabic, German, Russian, Korean, Italian,
Polish, Japanese, Swedish, Ukrainian, or Belgian Dutch from `.settings` or the
web dashboard. The Discord menu ranks those choices by aggregate preferred
locale across Fate's connected servers without displaying server counts. The
centralized outbound translator preserves mentions, URLs, code, commands, and
protocol identifiers, caches exact translations in MongoDB, and falls back to
the original English response if its provider is unavailable.

Searchable Local Logging History is opt-in and disabled by default. It supports
1–365 day retention, a 1 GB retained-payload limit per server, optional
uncached-message recovery, and separately enabled attachment-body storage with
configurable limits. Authorized administrators can disable new collection or
clear their server's retained archive.

Read [PRIVACY.md](PRIVACY.md) before enabling translation, message recovery, or
attachment storage; it documents the data sent or retained by each feature.

## Tests and checks

Run the bot, dashboard, and controller suites separately from the repository
root:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
.\venv\Scripts\python.exe -m unittest discover -s apps\Dashboard\tests -v
.\venv\Scripts\python.exe -m unittest discover -s apps\FateControl\tests -v
.\venv\Scripts\python.exe -m unittest apps.FateControl.test_server -v
```

The offline bot probe is the preferred check for loading every configured cog
and exercising real MySQL/MongoDB clients without connecting to Discord. Unit,
compile, or build success alone does not prove live Discord, device, or Android
runtime behavior.

Build the Android app from `apps/FateStatus` with JDK 17+ and Android SDK 35:

```powershell
cd apps\FateStatus
.\gradlew.bat test lintDebug assembleDebug
```

The debug APK is written to
`apps/FateStatus/app/build/outputs/apk/debug/app-debug.apk`.

## License

Fate is released under the [MIT License](LICENSE). Copyright © 2025
Villagers654.
