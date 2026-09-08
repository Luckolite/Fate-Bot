# Privacy and protection model

"Learn who to protect against" is implemented as temporary,
context-and-capability-specific interaction caution. It is not an enemy list,
a moral judgment, a diagnosis, or an automated moderation decision.

The `autonomic`, `ventral_vagal`, `sympathetic`, `dorsal_vagal`, and
`parasympathetic` names describe local software policies coordinated by
`autonomic/coordinator`. They are nonliteral metaphors, not stored
medical facts about an actor. Branch contributions may overlap; the system
does not assign a person to a permanent rung on a nervous-system ladder.

## Stored

- transient numeric state, its access time, and recent observation
  idempotency fingerprints, isolated by actor, scope, and capability;
- bounded, decaying functional-workspace trajectory and continuity held only
  in the running Fate service and isolated by an opaque
  guild/actor/capability slot; actorless frames retain neither;
- bounded adaptive cue and strategy weights and capacities;
- HMAC-pseudonymous subject, scope, episode, event, and transient-state keys;
- capability (`conversation`, `disclosure`, `tools`, or `moderation`);
- enumerated evidence source, kind, and reason codes;
- normalized numeric cue features, severity, confidence, and reward;
- timestamps, expiry, key identifiers, retraction links, a random datastore
  audience, short-lived consumed-authorization digests, and an authenticated
  security root over the durable security fields.

When discovery is explicitly configured, its separate authenticated store may
also contain short-lived HMAC references for question deduplication, advisor and
subject/topic cooldowns, delivery state, and expiry. Durable discovery models
contain only structured support/challenge counts for functional commitments,
moral principles, and riddle kinds plus actorless behavior and perspective
exemplars. No exemplar retains the source subject key.

Fate's sector registry places each guild in a separately authenticated state
file whose name is an HMAC-derived opaque sector identifier. Optional shared
sensory recall uses a separate state file and separate secret. Only reviewed
entries from a closed, code-audited non-personal association taxonomy may
address it. Arbitrary names, identifiers, message fragments, and dynamic tags
are rejected before signing. Each lookup also requires an association-key
capability, and the taxonomy value is HMAC-pseudonymized before persistence.
Cross-sector recall is capped and changes current cues only. It does not merge
guild member profiles.

Key identifiers detect a configured key mismatch; they are not the secret
keys. The JSON state file remains personal data despite pseudonymization. Keep
it local, restrict access, encrypt and expire backups, and honor inspection and
deletion requests.

Branch logits and the parasympathetic-named restoration drive are computed
transiently. They are not stored as separate actor facts and are not emitted in
the prompt packet. The coordinator's existing three mode weights, the derived
pain/panic/riddle regulatory motifs, generic guidance, and selected strategy
recruitments can cross that boundary.

## Never stored by this package

- raw messages, prompts, model responses, embeddings, usernames, or Discord
  IDs;
- profile display names, bios, pronouns, or other raw profile strings accepted
  by `ProfileSenses`;
- the caller's raw scope or episode identifier;
- race, religion, disability, health, sexuality, nationality, gender, or other
  protected or sensitive traits;
- diagnoses, inferred personality, alleged intent, social graphs, or guessed
  aliases;
- raw discovery questions or answers, human rankings, scalar moral worth,
  inferred person types, or links from an exemplar back to a person; and
- provider credentials, `identity_key`, `learning_key`, or `owner_key`.

Host RAM, queue, latency, rate-limit, compute, and error pressure are current
operational measurements, not member evidence. They may affect temporary load
and response pacing and may contribute to the nonliteral `pain` strain motif,
but are never admitted to durable caution by themselves. Pain, panic, and
riddle motif values are recomputed for the current packet and are not written
to the state file, evidence ledger, actor profile, or association store.

## Short-lived temporal dynamics

For an actor-scoped response, the running service may compare the current
functional moment with the newest valid predecessor in the same opaque guild,
actor, and capability slot. It retains at most eight coherent numeric
packet/moment records per slot in RAM so ordinary delivery replays can return
without mutating state. Stable frame and association inputs are reduced to a
process-keyed digest; raw messages, platform IDs, scope names, event IDs,
questions, answers, evidence rows, and free-form labels are not retained in the
cache. Profile strings can affect that non-reversible retry digest but are not
stored in clear text or included in the numeric trajectory. Actorless frames are
stateless, preventing one unidentified interaction from leaving temporal context
for another.

Momentum has a 30-second half-life and a hard 120-second predecessor horizon.
Expired, out-of-order, cross-scope, cross-actor, and cross-capability
predecessors are rejected. Replaying the same event is idempotent and cannot
extend retention or advance momentum, volatility, continuity, or revision.
Cache eviction, member forgetting, guild purge, and full purge erase the
applicable RAM entry. No trajectory or continuity object is written to the
organism state document, evidence ledger, actor profile, discovery store, or
cross-sector association store.

The provider-facing `organism.experience.v4` contract contains only a coarse
closed pattern and bounded aggregate temporal values alongside the existing
workspace summary. It strips raw current/prior vectors, per-axis history,
timestamps, elapsed time, cache revision, keyed slots, and identifiers. Its
temporal scope is the current response over short-lived history. Identifier-free
still does not mean unlinkable when it is sent beside the current request.

Temporal dynamics are internal functional-response summaries, not emotions,
mental-health signals, person attributes, moral judgments, or risk evidence.
They are computed downstream of risk, state, protection, and mode synthesis and
may influence only bounded response attention and continuity. They cannot
change evidence, discovery eligibility, permissions, moderation, tools, or any
person or moral model.

## Discovery and advisor review

Discovery observes only a closed, content-free `CuriosityStimulus`. Raw message
content, names, profile text, reactions, roles, and model-generated labels do
not fit that schema. Observation may reserve one fixed-template question, but
cannot update a functional identity, moral lens, riddle model, or exemplar.
Only the configured advisor assigned to a delivered question can submit its
closed structured review. Abstention and silence have no learning effect;
support and challenge remain independently visible.

Person-related inquiry requires a trusted episode, a current behavior or impact
signal, stricter curiosity threshold, and safe/agentic low-panic context.
Profile novelty alone never qualifies. The reviewed result is an actorless
behavior or perspective exemplar, not a personality, honesty, danger, protected
trait, or moral-worth profile. Constructive or adverse behavior selections
classify only that reviewed episode, and positive/negative impact remains a
separate pair of signals. Neither can affect moderation, permissions, tools,
risk, protection, or the evidence ledger.

The optional `organism.discovery.cortex.v1` handoff is scoped to the current
guild and contains only fixed model labels and bounded aggregates. It strips
timestamps, queue and revision data, pseudonymous keys, raw references,
questions, answers, and free text before provider disclosure. It is omitted
when no reviewed model exists and remains non-authoritative reflection context.

## Ephemeral profile sensing and consent

`ProfileSenses` is the one deliberately text-bearing body input. It accepts a
bounded current display name, bio, and pronouns plus visible structural
metadata. `SensorFrame.cues()` converts those values to numeric profile
presence, self-expression, profile-change, consent, and continuity signals.
Only that numeric reduction enters an `Observation`, idempotency fingerprint,
state transition, attention workspace, or persistence path. Raw profile text
does not become evidence and is excluded from the risk predictor.

The language-cortex boundary has a separate optional `organism.person.v1`
message. It is omitted unless the trusted application explicitly sets
`personalization_consent=True`; display-name use additionally requires
`name_use_allowed=True`. When enabled, its supplied bio and pronouns and any
allowed display name are disclosed to the configured model provider for the
current response. The wrapper labels the strings as untrusted data and escapes
delimiter characters, but this is still a real privacy disclosure. The host
application must obtain meaningful consent and provide a way to turn it off.

Profile completeness, account age, membership age, role count, avatar/banner
presence, name, bio, and pronouns cannot increase predicted risk, durable
caution, learning weight, permissions, or moderation authority. The package
does not infer protected traits, personality, honesty, mental state, or danger
from those values. Self-declared pronouns may be used only as supplied when
consented; they are not evidence for broader inference.

## Identity and namespace boundaries

Durable actor memory requires a stable `identity_key` containing at least 32
bytes. Subject and episode keys are HMAC-bound to their scope, so the same
platform actor has unrelated profiles in unrelated guilds. Capability is also
part of profile selection: conversation history cannot silently become tool or
moderation authority.

Transient state and observation idempotency use an
`actor + scope + capability` namespace. Actorless observations use
`scope + capability`. Stale namespaces become inactive after 24 hours by
default and are physically removed by a later mutation or explicit expiry
purge. Namespace count is capped at 256, and at most 4,096 recent event
fingerprints are retained across all namespaces; least-recently used histories
are trimmed first. If no identity key is configured, a random process-local
pseudonymization key is used for transient namespaces. Existing transient state
is therefore not linkable to that actor after a restart and ages out normally.

The Fate registry supplies a separate datastore per guild and an opaque
HMAC-derived filename, so the raw guild ID is not leaked by the path. The
runtime directories are ignored by Git; deployments must still apply
filesystem access controls and backup retention.

## Learning authority and poisoning controls

Every `learn()` call must include a datastore-bound authorization from
`sign_feedback()`. Ordinary sources use a stable `learning_key`; owner override
events use a separate stable `owner_key`. Each must contain at least 32 bytes.
Identity, learning, and owner secrets must be pairwise distinct.
The signature covers every feedback field, an issue time, a random nonce, and
the datastore audience. Missing, expired, replayed, stale-after-mutation,
wrong-store, or wrong-key authorization applies no durable learning.

`verified=True` and the `source` enum are semantic claims inside that signed
record. They confer no authority by themselves. In addition:

- model suggestions contribute zero and are not stored;
- the trusted gateway, not free-form message text, supplies `actor_ref`,
  `scope`, numeric cues, and event identifiers;
- fixed enums prevent arbitrary allegation text from entering state;
- actor `ADVERSE`, `SAFE`, and `REPAIR` records require an `episode_id`;
- every record requires an explicit signed `occurred_at`;
- safe or repair evidence offsets only earlier adverse evidence from the same
  actor, scope, capability, and episode;
- event IDs are idempotent and cannot be reused with changed signed content;
- persisted evidence rows are authenticated again under their admission key,
  so local record mutation is detected before aggregation;
- persisted admission semantics are revalidated before aggregation, and the
  signed authority role cannot be changed by editing the source field;
- a security-root HMAC detects deletion or mutation of the ledger, consumed
  authorization history, audience, and configured key identifiers;
- ordinary and owner authorization histories have separate quotas, preserving
  owner retraction capacity when ordinary traffic is full;
- episode and per-day contribution caps limit correlated bursts;
- one non-severe episode cannot produce a durable guarded posture;
- historical influence on current state is capped; current cues stay dominant;
- retractions require an authenticated owner override and an existing target;
- unmatched healing rows and new evidence beyond a hard capacity are rejected,
  never used to evict active evidence silently;
- learned values cannot change code, hard policy, permissions, or thresholds.

Actor-scoped evidence affects only that pseudonymous actor profile. It never
tunes global cue weights, strategy weights, or capacities. Global calibration
requires authenticated, actorless feedback with `scope="global"` and an
authenticated-event or owner-override source. Strategy outcome calibration
requires the separate `owner_key` and `OWNER_OVERRIDE` source; only that kind
may carry strategy names or a nonzero reward.

## Retention, inspection, and erasure

- Evidence influence decays and retained rows expire after at most 180 days by
  default.
- `inspect()`, `inspect_profile()`, and `inspect_profiles()` return logically
  current aggregates without subject keys or evidence rows and do not rewrite
  unchanged state.
- `purge_expired()` explicitly performs physical cleanup and reports removal
  counts. A later ordinary mutation also removes logically expired records.
- `forget(scope=..., actor_ref=...)` deletes a scoped actor's evidence,
  retraction links, and transient namespaces. A non-identifying consumed nonce
  digest remains only until its short authorization window closes, preventing
  replay from recreating the deleted profile. The Fate service also removes
  subject-bound discovery activity and that actor's RAM-only trajectory/
  continuity entry.
- `reset_state()` deletes transient activation and idempotency history but
  deliberately retains reviewed evidence.
- `purge_all()` erases all retained state and evidence and the service clears
  its RAM-only trajectory/continuity cache. It replaces durable state with a
  fresh state document with a new audience, and is the recovery path when a
  stable key has been lost or deliberately rotated without migration. The new
  audience invalidates authorizations issued for the erased store.

Service-level deletion is also an ordering barrier. Work that captured an older
lifecycle revision but had not started sensing or association learning is
rejected after member, guild, discovery, or association deletion rather than
being allowed to repopulate the erased data. One authenticated durable revision
and opaque domain-specific cross-process locks coordinate service instances
that share a state directory without globally serializing independent guilds;
neither the marker nor lock names retain an actor or guild identifier.

Loss or rotation of the owner key makes the authenticated lifecycle marker fail
closed. With every service instance stopped, the deliberately destructive
`MemorySectorRegistry.purge_all_state_after_key_loss()` bootstrap can erase all
known sector, shared-association, and discovery documents and reseal an empty
marker under the replacement key. It never preserves learned state and never
runs automatically.

A configured identity, learning, owner, evidence-addressing, or
security-integrity key mismatch fails closed rather than silently creating
inconsistent pseudonyms or accepting signatures under a new authority. The
key set becomes immutable when the state file is created; adding or omitting a
key later requires restoring the original set or deliberately using
`purge_all()`. Keep all three secrets stable, separate, backed up securely, and
inaccessible to model-generated content.

The file HMAC detects unauthorized changes to the current JSON document. It
cannot detect replacement of the entire file with an older, validly signed
snapshot. Production deployments whose host or backup path is adversarial
need an external monotonic version, append-only service, or transactional
datastore with anti-rollback guarantees.

## Provider and Fate boundaries

The feeling and experience packets contain no actor key, raw scope, username,
evidence row, or message history. They contain generic style values, the three
numeric regulatory motifs, and a coarse bounded trajectory summary derived
from current and short-lived functional values and, when available, a scoped
profile. A provider receiving the packet
beside the current user message can associate those values with that request
and its normal account, transport, or application metadata. Identifier-free
does not mean anonymous or unlinkable.

The optional consented `organism.person.v1` packet is intentionally not
identifier-free: it may carry display name, bio, and pronouns. It remains
current-step-only and is never written to organism persistence, but the
provider can associate it with the request. Do not enable it by default or set
its consent flags without a real application-level consent decision.

Autonomic and vagal-named computations do not authorize the application or
provider to infer, store, or disclose a user's health, trauma, disability,
arousal, or physiological condition. They are ephemeral response-policy
signals. The pain, panic, and riddle labels likewise describe only the software
simulation and must never be attributed to a user.

Never send the evidence ledger, subject keys, inferred health or protected
traits, Discord IDs, or historical profile aggregates to a model provider.
Only the explicitly consented current profile fields described above are an
exception. Do not import the legacy ChatterBot conversation dataset.

Fate now has a disabled-by-default body-service boundary that constructs opaque
per-guild sectors, samples current host memory pressure, and runs synchronous
stores through `asyncio.to_thread`. Global and opaque per-guild fail-fast
admission limits prevent unbounded executor queues, and a cancelled caller
retains its permit until the underlying worker exits. It is not connected to
Discord message events or an OpenAI client. Before enabling collection or
member-specific evidence, add explicit administrator controls, retention and
clear/export UI, user-facing disclosure, and corresponding updates to Fate's
canonical `PRIVACY.md`. Deterministic Discord and tool permissions must remain
in application code outside the model prompt.

The discovery core is likewise disabled by default. Its advisor list is always
explicit and never falls back to bot owners. A future Discord dispatcher must
add guild/channel allowlists, recorded advisor opt-in, member observation
preferences, direct-reply matching, delivery caps, test-instance blocking, and
authenticated inspect/export/clear/delete controls before it is enabled.

The Python methods `inspect_profile()`, `inspect_profiles()`, `forget()`,
`purge_all()`, `inspect_discovery()`, and `purge_discovery()` assume an already
trusted in-process control plane. They must not be exposed remotely or as model
tools without a separate, scope-bound authorization layer that distinguishes
self-deletion, moderator inspection, and datastore recovery.
