# Fate Mission Control

A native Android management app and resizable home-screen widget suite for Fate.
The current application package is `xyz.fatebot.status`, version `3.2.5`
(`versionCode` 39).

The Android app and Windows Fate Control panel share eight visual themes and a
matching set of theme-specific artwork for Overview, Metrics, Config, Backups,
and Console.

The overview and widget show:

- Online/offline state
- Server and user counts
- Configurable server, user, gateway latency, shard, and calendar-month command metrics
- Readable status-widget typography with larger titles, state, health, metric,
  label, uptime, and refresh text in both full and compact layouts
- Uptime and last refresh time
- Storage use with an expandable, labeled breakdown sorted largest-first
- Authenticated host OS, architecture, uptime, process, temperature, and
  bot-runtime telemetry
- Component health for Discord, database, disk, extensions, commands, and logs
- A dedicated home-screen memory widget that breaks down Fate's process tree
- Controller, configuration, disk, database, extension, and recent-log health

The app also provides:

- The same eight visual themes as Fate Control on Windows—AMOLED Neon,
  Classic, Clockwork, Galaxy, Music, Nature, Underwater, and Waterfall—with
  shared generated menu artwork, matching widget palettes, and optional smooth
  ambient motion
- A prewarmed shared panel for Overview, Metrics, Config, Backups, and Console;
  these screens update together with the Windows desktop app. Native Settings
  handles device profiles, appearance, backup linking, and widget preferences.
  Superseded native screen builders and their chart/editor classes are removed
- Reliable Panel navigation that keeps the selected section synchronized with
  the Android WebView across delayed page finishes and reloads
- One-time launch-ticket authentication for the shared panel, keeping the
  Android Keystore-backed control key out of URLs and browser storage
- Parallel local-subnet discovery for FateControl (`16421`) and status-only Fate
  instances (`16420`)
- Automatic routing of authenticated management requests from a saved `16420`
  status address to FateControl on `16421`, with readable non-JSON HTTP errors
- Connection fields accept either a bare FateControl address or a copied
  `/control`, `/status`, or API URL and normalize it to the server origin
- A real Start/Stop switch backed by the always-on `apps/FateControl` supervisor
- Confirmed bot restart and opt-in host reboot controls backed by FateControl;
  restart recognizes a verified PyCharm-launched `fate.py`, warns that the Run
  session will end, and transfers the replacement process to FateControl
- An authenticated live console with a bounded stdout/stderr tail, traceback and
  warning highlighting, manual refresh, stable two-second follow mode that does
  not reset either scroll position, and copy support
- A separate resizable home-screen console widget that shows the newest log and
  traceback lines, refreshes on demand, and opens the full live Console tab
- A filterable, section-focused Config tab for core, databases, translation,
  website, FateControl, organism, permissions, and extension settings with no
  raw JSON editor, plus discard, dirty-state, and enabled-on-change
  save/restart actions
- Automatic current-config loading with safe cached previews, protected-secret
  handling, typed controls, revision conflict detection, edit detection, and an
  enabled-on-change Save button
- Automatic backups before config saves and an optional Save & Restart flow
- A collapsible Backups settings panel with live frequency, age, aggregate
  storage, complete-backup count, and local-file controls; Save activates only
  after an edit and rejects stale edits instead of overwriting a newer policy
- Google Drive OAuth linking in the system browser followed by an in-app remote
  folder browser; Google refresh credentials remain only on the Fate computer
- A distinct service-restart notice when edited FateControl listener or
  host-policy settings cannot be applied by restarting only the bot
- Best-effort 1-minute or 5-minute foreground/widget sync, including when the
  only installed widget is Console or service controls
- A remembered controller address shared by the app and widget across restarts,
  reboots, and in-place app updates
- Immediate Android Keystore-backed control-key persistence while the key is
  entered, including automatic migration from earlier app versions
- Eight selectable visual themes with original generated backgrounds
  that save and apply immediately across the app and every home-screen widget
  without saving unrelated Settings drafts, plus subtle lifecycle-aware gears
- A service-control widget with authenticated Start, Stop, Restart, and Refresh
- A Metrics tab with independently remembered 1m through 1y timeframes,
  arbitrary whole-second validated bucket sizes saved with Done, including
  60-second buckets for a 12-hour graph, reorderable compact graphs and Live
  host load, up to 720 displayed samples, labeled Y axes, drag-to-inspect sample
  previews, command drill-downs, and tap-the-graph settings
- A top Metrics page selector: General retains the existing host and reach
  graphs, while Modules shows AntiSpam and Chatfilter triggers plus UNO,
  Connect Four, and Tic-Tac-Toe usage; UNO overlays games and participant totals
- A remembered 5s, 10s, 15s, or 30s Metrics refresh cadence (15s by default),
  active only while the tab is visible
- A compact live Metrics panel for CPU, expandable RAM, network MB/s, and
  backend-sampled disk read/write MB/s with an immediate numeric first state
- A configurable home-screen metric graph widget with its own metric and window
- Additional selectable status-widget metrics for host CPU, RAM, storage,
  network throughput, and controller recoveries

The default status endpoint remains `http://144.76.105.17:16420`. For management,
run `python -m apps.FateControl.server` on the bot host, scan the local network,
and enter the key from `data/fate-control.token` in Settings. The app never
contains or requests the Discord bot token.

Host reboot remains disabled unless `control_panel.allow_host_reboot` is `true`
in `data/config.json` or `FATE_ALLOW_HOST_REBOOT=1` is present in the
FateControl service environment. Restart FateControl after changing that policy.
The public status route intentionally hides the capability; the authenticated
status response used by the app reports the live value.

## Build

Use JDK 17 or newer and Android SDK 35:

```powershell
.\gradlew.bat test lintDebug assembleDebug --offline
```

The debug APK is written to `app/build/outputs/apk/debug/app-debug.apk`. Widget
metrics can be selected in Settings; the compact layout uses the first two
enabled metrics, while the full layout can display the first five.

Android can defer alarms while a phone is idle or battery-restricted. Opening
the app, tapping refresh, or tapping the widget's LAN-scan control always starts
an immediate update.
