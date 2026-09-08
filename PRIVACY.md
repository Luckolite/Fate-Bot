# Privacy Policy

**Last updated: August 29, 2026**

Fate is a self-hosted Discord bot and management stack. This policy explains
what the software can process when you use the bot, its web dashboard, Fate
Control, or the Fate Mission Control Android app.

The person or organization running a Fate instance (the **operator**) controls
that instance's configuration, databases, backups, and optional integrations.
If you use someone else's instance, contact its operator or a server manager
about your data. The maintainers of this source repository do not automatically
receive data from independently hosted instances.

## Data Fate processes

Fate processes Discord data needed to provide the features that users and
server managers choose to use. Depending on the enabled modules, this can
include:

- Discord user, server, channel, role, message, and object identifiers;
- usernames, display names, avatars, membership, roles, permissions, and
  server configuration;
- command names, arguments, form input, and content deliberately submitted to
  features such as timers, giveaways, warnings, moderation, modmail, games,
  notes, welcome messages, and filters;
- XP, leaderboard, economy, game, vote, warning, and other feature state;
- moderation and audit events such as message edits or deletions, role and
  channel changes, kicks, bans, and the responsible actor when Discord makes
  that information available; and
- the time a member sends a message for activity and XP features. Ordinary XP
  counting does not require Fate to retain the message text.

Fate may temporarily inspect message text, embeds, attachments, reactions,
member state, and Discord audit logs to respond to events or execute enabled
features. Temporary processing does not necessarily mean that the data is
stored in Fate's databases.

## Optional collection and sharing

Server managers control features that can retain or relay additional data:

- **Logging.** Discord events can be posted to a configured logging channel.
- **Searchable Local Logging History.** This is off by default. When enabled,
  Fate stores generated log records on the operator's host. If uncached message
  recovery is also enabled, Fate keeps message content together with the
  author, channel, timestamps, embeds, and attachment metadata. Attachment
  bodies are stored only if the operator separately enables file storage.
- **Chat bridges.** Messages, display names, avatars, embeds, and attachments
  posted in a bridged channel are relayed to the other channels selected by a
  server manager through Discord webhooks.
- **Translation.** If a server manager selects a language other than English,
  user-visible strings can be sent to the configured translation provider.
  Fate masks Discord mentions, IDs, commands, links, and code before sending
  text, but variable text such as usernames or quoted excerpts may remain. The
  exact English string and translated result are cached in the operator's
  MongoDB database. Selecting English disables translation.
- **Role persistence.** Member roles can be retained after a member leaves so
  they can be restored if the member rejoins.
- **Organism discovery.** This experimental feature is off by default and is
  not connected to Discord messages in this release. If a future deployment
  enables it, trusted content-free observations can queue fixed-choice
  questions for explicitly configured, consenting advisors. The local store
  may retain keyed references temporarily for deduplication, expiry, and rate
  limits, plus aggregate reviewed functional commitments, moral lenses, riddle
  kinds, and de-identified behavior exemplars. It does not retain raw messages,
  names, profile text, free-form answers, protected traits, social graphs, or
  person rankings. Observation alone never changes a moral or identity model.
  Before connecting it to Discord, an operator must provide server and channel
  allowlists, user disclosure, advisor opt-in, observation opt-out, and clear,
  export, and deletion controls.

Individual users can run Fate's `privacy` command to control supported personal
features. Current controls cover XP, fun-command participation, nickname
display, username history, activity information, and certain member-profile
moderation logs. Some protections, moderation records, server audit logs, and
data required to carry out a command are governed by the server manager rather
than an individual preference.

## Dashboard and authentication

The web dashboard requests Discord OAuth scopes `identify` and `guilds`. Fate
uses the returned identity and server list to sign the user in and determine
which servers they may manage. Protected actions also recheck live ownership or
Manage Server access.

Dashboard sessions are held in the operator's memory and expire no later than
seven days or when the Discord OAuth token expires. Restarting the dashboard
ends all sessions. The browser receives a signed, HTTP-only session cookie; Fate
does not use it for advertising or cross-site tracking.

## Local operational telemetry

Fate records bounded numeric operational metrics on the operator's computer.
These include aggregate server and member counts, registered command names and
counts, database operation counts, sent-message counts, and aggregate module or
game events. The telemetry store does not contain message text, command
arguments, Discord IDs, database query text, database names, or credentials.

Recent five-second metrics are kept for approximately 26 hours. Five-minute
historical aggregates are kept for 30 days. Telemetry is local and is available
through authenticated Fate Control routes; it is not sent to the Fate source
repository maintainers.

## Fate Control and Mission Control

Fate Control exposes public discovery and basic status information on the
operator's network. Private host information, console output, configuration,
metrics, backups, and process controls require the operator's control key.
Browser control-panel sessions expire after 12 hours or when Fate Control
restarts.

The Android app stores the selected Fate address, display preferences, widget
settings, and refresh choices on the device. Its Fate Control key is encrypted
with Android Keystore-backed key material. The app does not contain or request
the Discord bot token and does not include a separate advertising or analytics
service. It communicates with the Fate endpoints selected by the user and, when
linking backups, opens the system browser for Google authorization.

Fate's local, aggregate telemetry counts completed Dashboard sign-ins for the
control panel Metrics view. This counter does not retain Discord IDs, account
details, OAuth tokens, or the servers associated with a sign-in.

Operators should restrict Fate Control to trusted networks. The Android app
supports local HTTP endpoints, so traffic is not encrypted unless the operator
places the service behind HTTPS or uses another protected network channel.

## Storage, retention, and backups

Fate stores data in operator-controlled MySQL, MongoDB, SQLite, and local files.
The exact lifetime of most feature data depends on the feature, server actions,
and the operator's retention and backup policies.

Searchable Local Logging History has operator-configurable retention of 1 to
365 days and a retained-payload limit of up to 1 GB per Discord server. Its
default retention is 30 days. The oldest records are removed first when the
configured time or storage limit is reached. Turning the feature off stops new
records but does not erase existing history. A server owner can clear that
server's retained logging history with `log archive clear`.

When backups are enabled, an archive can contain Fate's MySQL and MongoDB data
and, at the operator's option, local datastore files. Backup retention can be
limited by age, file count, total storage, or a combination of those limits.
Archives remain on the operator's host unless Google Drive backup is enabled.

## Third-party services

Fate necessarily exchanges data with Discord. Optional features can also
exchange data with services selected by the operator:

- the configured translation provider receives text described under
  **Optional collection and sharing**;
- Google Drive receives backup archives if remote backup is enabled; and
- Top.gg can provide a user's Discord ID and vote information when its vote
  integration is configured.

These services process data under their own terms and privacy policies.

For Google Drive backups, Fate requests Drive access so the operator can browse
folders, upload archives, and apply remote retention by listing and deleting
backup files. Google access and refresh credentials remain on the Fate computer
and are not returned to the Android app. Unlinking Drive revokes the credential
when possible and removes Fate's local token file. Already uploaded backups
remain in the operator's Drive until removed there or by the configured
retention process.

## Security

Fate supports encrypted secret storage, scoped OAuth sessions, authenticated
management routes, Android Keystore-backed control-key storage, and local or
operator-selected database hosting. No system can guarantee absolute security.
Operators are responsible for access controls, host and database security,
HTTPS where appropriate, backup protection, and keeping credentials out of the
source repository.

## Access and deletion requests

Use the `privacy` command to change supported personal preferences. For access,
correction, or deletion requests involving a deployed instance, contact that
instance's operator or a server manager. Server managers can remove some
feature data through Fate's management commands; other records may require the
operator to act on the underlying database or backups. Data already copied to
Discord channels, bridged servers, external services, or retained backups may
need to be handled separately.

For questions about this repository's policy, open an issue at
<https://github.com/Villagers654/Fate/issues>. Do not post tokens, private
messages, personal data, or other secrets in a public issue.

## Changes to this policy

This policy may be updated when Fate's features or data practices change. The
date at the top identifies the latest revision. Operators who modify Fate or
enable additional integrations should update the policy presented to their own
users.
