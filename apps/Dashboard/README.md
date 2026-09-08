# Fate Dashboard

Fate Dashboard is a public-first website and authenticated server control panel.

- `/` is public. It explains Fate's strongest features and builds a searchable command catalog directly from `cogs/` without importing or starting the bot.
- `/dashboard` can be previewed without signing in. Configuration, personal rank cards, and leaderboards unlock after Discord OAuth. The legacy `/fate` path redirects to `/dashboard`.
- Discord authorization uses only the `identify` and `guilds` scopes. A user can manage only servers where Discord reports ownership, Administrator, or Manage Server permission.
- Fate rechecks those permissions against the live guild member on every protected request. Standalone mode refreshes the OAuth guild list at most once per minute.
- Settings write to the same MongoDB collections Fate already uses. When mounted in the bot, live caches update immediately; verification changes also publish, refresh, or remove the shared Discord entry panel.
- General settings expose the same 16-language catalog as Fate's Discord menu,
  including separate Simplified and Traditional Chinese options and Belgian
  Dutch; Discord ranks its menu by aggregate guild preferred locale without
  exposing counts.
- The Modules workspace manages Member Verification, Auto Roles, Chat Filter, Logging, Modmail, Spam Protection, Raid Protection, welcome and goodbye messages, Restore Roles, Self-Assign Roles, Giveaways, Starboards, and Voice Activity Log. Live controls run through Fate so dashboard changes take effect immediately and safely; Giveaways exposes its live operational state while winner actions remain in Discord.
- The overview derives live system totals, category coverage, setup gaps, and direct module shortcuts from that same module payload, so a save is reflected immediately without a page reload.
- Rank cards and leaderboard views read Fate's existing `msg`, `global_msg`, `monthly_msg`, `global_monthly`, and `commands` MySQL tables.

## Discord application setup

1. Open the Fate application in the Discord Developer Portal.
2. Under OAuth2, add `http://localhost:8080/auth/callback` for standalone development. For deployment, add the exact HTTPS callback URL instead.
3. Put the OAuth client secret in `Desktop\client_secret.txt`, or copy
   `.env.example` to `.env` and set `DISCORD_CLIENT_SECRET`.
4. Set `DASHBOARD_SESSION_SECRET` to a long random value.
5. Set `DISCORD_BOT_TOKEN` when running standalone so channel selectors and leaderboard names can be populated. When mounted inside Fate, the dashboard reuses Fate's configured bot token.

Never commit `.env`, a bot token, a client secret, or database passwords.

## Run with Fate

`cogs.core.dashboard`, `cogs.core.messages`, and `cogs.core.settings` are included in `data/config.json`. The dashboard mounts on Fate's existing aiohttp listener at port `16420`, alongside `/ping` and `/status`.

Running the owner command `.reload` with no arguments (or `.reload all`) reloads every configured cog, republishes application commands, refreshes the website command catalog, and tells open public-site and dashboard tabs to reload automatically. Targeted reloads such as `.reload logger` leave open pages alone.

Set the OAuth callback to the public URL that proxies to port `16420`, for example:

```text
https://fatebot.example/auth/callback
```

Keep port `16420` bound to the default loopback address, use HTTPS at the reverse proxy, and set:

```text
DASHBOARD_SECURE_COOKIES=1
DISCORD_REDIRECT_URI=https://fatebot.example/auth/callback
DASHBOARD_TRUSTED_PROXIES=127.0.0.1/32,::1/128
```

Configure the proxy to replace (or correctly append to) `X-Forwarded-For`, and list only the IPs or CIDRs of proxies that connect directly to Fate. Forwarding headers from any other peer are ignored. If the proxy runs on another host, replace the loopback defaults with that host's narrowly scoped address.

## Run standalone

From the repository root:

```powershell
copy apps\Dashboard\.env.example apps\Dashboard\.env
.\venv\Scripts\python.exe -m apps.Dashboard.server
```

Then open `http://localhost:8080`.

For a database-free local UI preview, set `DASHBOARD_DEV_MODE=1`. Mission Control will show an explicit **Enter local demo** action and use in-memory sample data. This mode is rejected entirely when the dashboard is mounted inside Fate.

## Configuration variables

| Variable | Purpose |
| --- | --- |
| `DISCORD_CLIENT_ID` | Discord application ID |
| `DISCORD_CLIENT_SECRET` | OAuth client secret |
| `DISCORD_CLIENT_SECRET_PATH` | External OAuth client-secret file; defaults to `Desktop\client_secret.txt` |
| `DISCORD_REDIRECT_URI` | Exact registered OAuth callback |
| `DISCORD_BOT_TOKEN` | Channel and leaderboard identity lookups in standalone mode |
| `MONGODB_URI`, `MONGODB_DATABASE` | Fate configuration collections |
| `MYSQL_*` | Fate XP and leaderboard tables |
| `DASHBOARD_SESSION_SECRET` | Signs opaque session cookies |
| `DASHBOARD_SECURE_COOKIES` | Force secure cookies; HTTPS callback URLs enable them automatically |
| `DASHBOARD_HOST`, `DASHBOARD_PORT` | Listener address; defaults to loopback only |
| `DASHBOARD_TRUSTED_PROXIES` | Comma-separated IP/CIDR allowlist allowed to supply `X-Forwarded-For`; defaults to loopback proxies |
| `DASHBOARD_DEV_MODE` | Explicit local-only sample mode |

Sessions are held in memory and expire no later than seven days or the Discord OAuth token. Restarting the dashboard signs everyone out. Configuration writes require a current Discord permission check, a valid signed-in session, and a matching CSRF token. OAuth state and write endpoints are bounded and rate-limited.

## Checks

```powershell
.\venv\Scripts\python.exe -m unittest discover apps\Dashboard\tests -v
```
