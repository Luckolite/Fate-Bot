# Fate local test databases

This folder provisions portable, loopback-only MySQL and MongoDB instances for
running Fate without touching its normal databases or configuration.

## Start and verify

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\apps\DevServices\start-databases.ps1
.\venv\Scripts\python.exe .\apps\DevServices\smoke-test.py
.\venv\Scripts\python.exe .\apps\DevServices\offline-bot-probe.py
```

MySQL listens on `127.0.0.1:3307` and MongoDB listens on
`127.0.0.1:27018`. Both processes are detached and run without console windows,
so the launcher window is not needed after it reports that they are ready.
Generated binaries, data, logs, credentials, and test config files are ignored
by Git.

The offline bot probe loads every configured cog and connects through Fate's
actual MySQL and MongoDB clients without logging in to Discord.

## Run the test bot

```powershell
powershell -ExecutionPolicy Bypass -File .\apps\DevServices\run-test-bot.ps1
```

The wrapper selects `data/config.test.json` and `data/auth.test.json`, uses the
isolated `fate_test` databases, and reads the Discord token from
`Desktop\token.txt`. The generated test auth file does not contain a Discord
token. The regular `data/config.json` and `data/auth.json` remain the default
when Fate is launched normally.

Put only the raw Discord bot token in `token.txt`; a trailing newline is fine.
Direct `python fate.py` launches also prefer this Desktop token file whenever it
exists. When no explicit `FATE_CONFIG_PATH` is set, that direct launch also
selects `data/config.test.json` and `data/auth.test.json`, so it connects to the
local test databases on ports `3307` and `27018`.

To use another location, pass it to the launcher or set `FATE_TOKEN_PATH`:

```powershell
powershell -ExecutionPolicy Bypass -File .\apps\DevServices\run-test-bot.ps1 -TokenPath "D:\secrets\fate-token.txt"

# Equivalent environment-variable form
$env:FATE_TOKEN_PATH = "D:\secrets\fate-token.txt"
powershell -ExecutionPolicy Bypass -File .\apps\DevServices\run-test-bot.ps1
```

## Stop the databases

```powershell
powershell -ExecutionPolicy Bypass -File .\apps\DevServices\stop-databases.ps1
```

The database files remain under `.fate-test-services` for the next test run.
