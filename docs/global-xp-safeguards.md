# Global XP safeguards

Global XP uses an account-wide ten-second cooldown, shared across servers. Bot
accounts, webhooks, replayed message IDs, and messages more than two minutes old
do not earn global XP. Existing channel exclusions and burst-spam checks still apply.

Fate temporarily pauses global XP when either pattern is observed:

- At least 180 consecutive intervals stay within 0.15 seconds of the initial
  interval, over at least two hours. Eligible intervals range from 10 to 300 seconds.
- Activity spans at least 36 hours, with at least ten XP events in 70 of the last
  72 completed half-hour windows and at least 720 events in those windows. A gap
  longer than one hour resets this evidence.

These are automation heuristics, not proof of a self-bot or a person's sleep
schedule. Presence, online status, message text, and time zones are not collected.
One long day, sparse overnight messages, and brief repetitive timing do not meet
the thresholds. Randomized automation can still evade the timing check.

The first flag pauses global XP for 24 hours. Another flag within 30 days raises
the pause to three days, then seven days. Pauses expire automatically; messages
sent during a pause do not extend it. Server XP, levels, roles, and access to Fate
are unaffected by these new penalties. Older global XP remains ranked.

On a flag, Fate removes recent global XP from both the all-time total and the
30-day leaderboard, using `global_monthly`. New global entries use minute buckets.
Only buckets starting inside the preceding 24 hours are removed; the partial
minute at the start is preserved. Historical daily buckets that overlap the
start of that window are also preserved, since they cannot be split accurately.
The flagged message does not earn global XP. Pending awards are included before
rollback, and totals cannot become negative.

Use `.globalxpstatus` to see eligibility, the reason and expiry of an active pause,
and the XP removed. Fate's owner can use `.globalxpunblock USER_ID` to lift a pause
and reset repeat penalties after review; it does not restore removed XP.

The bot creates three InnoDB tables automatically: `global_xp_activity` for compact
activity checkpoints, `global_xp_flags` for penalty audits, and `global_xp_batches`
for retry receipts. The existing `global_msg` and `global_monthly` tables must also
use InnoDB (checked before enabling awards). Inactive checkpoints expire after
35 days; audit records and completed retry receipts expire after 90 days.
The service assumes one XP-writing Fate process, as the old batching
pipeline did. Activity and awards checkpoint together every 30 seconds. A hard
crash can lose the unflushed interval; committed activity and pauses survive
restart. No historical account is flagged merely from its old daily XP totals.

Awards, checkpoints, flags, and rollback share one database transaction. Retry
receipts prevent duplicate awards or deductions after an uncertain commit result.
The bot fails closed for global XP when an account's checkpoint cannot be loaded.
Both Discord and the dashboard read the same corrected totals; Discord's cached
leaderboards are invalidated after the flush. No separate dashboard migration is
needed.
