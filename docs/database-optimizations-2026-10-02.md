# Fate database optimization audit

Verified locally on October 2, 2026. Changes remain cumulative in the shared
checkout; production has not been deployed or benchmarked.

## Current pass

| Path | Improvement | Verification |
| --- | --- | --- |
| Privacy reads | Share concurrent reads for each user; user-info's three preference checks use one SQL read. Sequential events fetch current preferences without a new TTL. | Concurrent calls, cancellation, failure/retry, and real DB updates tested. |
| Moderation logs | Fetch the displayed 16 cases in SQL for every filter, instead of downloading all matching history. | Real 6,000-row fixture returns exactly the same first 16 cases. |
| Moderation user lookup | Bind user IDs as strings, matching the existing VARCHAR column and avoiding numeric conversion of indexed snowflakes. | Existing `cases_user_idx` becomes usable. Synthetic EXPLAIN estimated rows fall from 3,000 to 15; this is a query-plan estimate, not a production timing claim. |
| Modmail case chooser | Fetch only the five cases already offered by the chooser, using binary link ordering to preserve its previous Python string ordering. | Real DB chooser test preserves the five links and keeps adjacent large snowflakes distinct. |
| Invite persistence | A complete guild invite uses one atomic upsert instead of a read followed by an insert/update. Preserve missing metadata, monotonic use counts, original creation time, deletion time, and the incomplete-invite fallback. | Real DB insert, concurrent refresh, lower/higher counts, and incomplete invite tested. |
| Prefix writes | Commands, settings menu, and mounted dashboard share serialized, acknowledged prefix writes. Equal settings skip writes; failed or uncertain writes require a fresh acknowledgement before later no-ops. | 20 concurrent equal edits produce one write; failed writes/resets and ordering tested, plus real Mongo round trip. |
| Dashboard settings | Unchanged ranking settings use the existing snapshot comparison instead of force-writing the document. | Unchanged and changed saves tested. |
| Dashboard profiles | Read server/global XP and tied ranks in one query instead of two, preserving missing-board defaults. | Real MySQL ranks/ties and missing-user/server cases tested. |
| Standalone dashboard pool | Serialize initial pool creation and close, avoiding leaked pools from concurrent first requests; recycle idle connections with a configurable default of 1,800 seconds. | Concurrent first requests, configurable defaults, and close-during-create tested. |
| Existence reads | Request only `1` with `LIMIT 1` for blocked-server and duplicate-username checks. | Existing behavior covered by broader checks. |
| Invite fallback | Return SQL connection before replying, and terminate the async generator normally when no cached invite exists. | Missing-invite behavior and returned connection checked. |

Removed the unused prefix cleanup task, which assumed an obsolete timestamp
cache shape. Runtime prefix mappings contain authoritative configurations and
must remain available for prefix parsing.

## Earlier improvements retained

The checkout already includes bounded Mongo bulk writes and snapshot-safe
acknowledgements, coalesced Mongo document loads, projected ID iteration,
unchanged-context write suppression, shared global leaderboards, idle MySQL
recycling, bounded history cleanup, indexed global XP ordering, and shorter
SQL checkouts in custom commands, privacy saves, modmail, battle stats, snipe,
and administrative commands. Their existing tests were reused and included in
the final shared verification.

Audited active bot cogs, shared cache/pool code, XP guard transactions, votes,
UNO/factions persistence, and mounted/standalone dashboard database stores.
Disabled legacy cogs were identified separately. No additional schema indexes
or transaction rewrites were justified by the available local evidence.
Further tuning should use representative production query plans and workload
measurements before increasing write/index cost or changing consistency.

## Validation

- `checks` discovery: 436 passed with real isolated DB and global-XP tests enabled.
- Bot `tests` discovery: 109 passed.
- Dashboard tests: 133 passed, two optional tests skipped.
- One additional focused real-DB modmail chooser test passed after the shared run.
- Total: 679 passing test executions, two skips.
- Offline probe: 49 extensions, real isolated MySQL/Mongo clients, 89 application
  commands, private help filtering, and clean shutdown passed.
- Broad Python compilation and Git whitespace check passed.
- Ruff passed for the new DB helper/checks, ranking store, and updated probe.

The shared run also covers cumulative RAM changes from the coordinated Fate
chat. Updated stale test fixtures and offline-probe expectations to match the
current runtime fields, native `/anti-raid` group, and nested faction commands.
The probe simulates the secondary role without changing main runtime settings.

Database integration tests use loopback MySQL port 3307, MongoDB port 27018,
the `fate_test` database, and uniquely named temporary tables/collections.
Tests remove only their owned temporary data. Database services remain running
for the coordinated chat. No production data, credentials, or logs are included
here. Local test logs are under `.temp/database-optimization-*.log`.

Reproduce the shared checks from the repository root:

```powershell
$env:FATE_TEST_DATABASES = '1'
$env:FATE_XP_TEST_PORT = '3307'
.\venv\Scripts\python.exe -m unittest discover -s checks -t . -p '*.py'
.\venv\Scripts\python.exe -m unittest discover -s tests -p 'test*.py'
.\venv\Scripts\python.exe -m unittest discover -s apps\Dashboard\tests -t . -p 'test*.py'
.\venv\Scripts\python.exe apps\DevServices\offline-bot-probe.py
```
