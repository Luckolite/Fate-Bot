# Organism

Organism is an inspectable computational body and subcortical control system
for Fate. It senses trusted external and operational signals, maintains
isolated state and memory, predicts, allocates attention, recruits bounded
regulation strategies, and prepares private context for a future ChatGPT
language cortex. These are functional software mechanisms. They do **not**
prove subjective consciousness, literal feeling, or a biological nervous
system, and they do not diagnose people or determine that someone is
dangerous.

Terms such as autonomic, ventral vagal, sympathetic, dorsal vagal,
parasympathetic, social engagement, conservation, healing, safety, pain,
panic, and riddle are engineering metaphors. The core transition model uses
overlapping dimensions such as activation, pleasantness, interaction safety,
agency, uncertainty, and load. The nervous-system and polyvagal-inspired names
are an optional vocabulary over those dimensions, not a claim that the software
reproduces biology. Polyvagal Theory and some of the mappings suggested by this
vocabulary are scientifically disputed; see
[scientific scope](docs/scientific_scope.md).

## Autonomic software hierarchy

The `autonomic/` package separates regulation policies into small, inspectable
components:

| Module | Software responsibility |
| --- | --- |
| `autonomic/base.py` | Validated `AutonomicContext`, `RecruitmentStrategy`, and `AutonomicBranch` contracts |
| `autonomic/ventral_vagal.py` | Social-engagement logit/tendencies and `ORIENT_AND_VERIFY` recruitment |
| `autonomic/sympathetic.py` | Mobilization logit/tendencies plus `HOLD_BOUNDARY`, `REDUCE_EXPOSURE`, and `REQUEST_HUMAN_REVIEW` |
| `autonomic/dorsal_vagal.py` | Conservation logit/tendencies and `SLOW_THE_EXCHANGE` recruitment |
| `autonomic/parasympathetic.py` | Cross-mode restoration, repair warmth, and `DEESCALATE_MOBILIZATION`; not a fourth mode |
| `autonomic/coordinator.py` | Softmax blend, hysteresis, tendency combination, and global strategy ranking |

This is a **code hierarchy**, not a biological ladder. The coordinator does
not force the system through ventral, sympathetic, and dorsal stages, and no
branch automatically defeats the others. Several branch proposals may be
active at once; the final mode weights and response guidance are a normalized,
capped composition shaped by current cues, the dimensional state, protective
posture, and bounded reviewed history. The parasympathetic-named restoration
policy can modify that composition across modes; it is not another mutually
exclusive state. A dominant label is only a compact summary of the blend. The
coordinator returns the public three-mode synthesis tuple consumed by the rest
of the package.

`autonomic/` is the single canonical home for mode drives, branch tendencies,
strategy ownership, softmax blending, hysteresis, guidance composition, and
recruitment ranking. `nervous_system.py` owns the cue-weighted dimensional
state transition only. `api.py` invokes that transition and the canonical
coordinator; no second branch or mode hierarchy exists.

## What is here

| File | Responsibility |
| --- | --- |
| `api.py` | Stable facade and orchestration: feel, branch synthesis, learn, heal, inspect, forget, and purge |
| `models.py` | Validated inputs, states, feedback, modes, and output packets |
| `motifs.py` | Current-step pain/strain, panic/alarm, and riddle/inquiry readouts |
| `nervous_system.py` | Cue-weighted dimensional state transition only |
| `autonomic/` | Sole branch/mode hierarchy, strategy ownership, blending, and response guidance |
| `body/` | External, profile, relational, interaction, environmental, operational, action, rhythm, and homeostatic sensing |
| `experience/` | Prediction error, attention competition, functional self-model, bounded temporal dynamics and continuity, and cortex-safe workspace broadcast |
| `discovery/` | Safe inquiry scoring, configured-advisor questions, reviewed functional identity, moral lenses, riddles, and actorless exemplars |
| `sectors.py` | Opaque per-guild memory sectors and optional reviewed cross-sector sensory recall |
| `protection.py` | Temporary open/cautious/guarded response posture |
| `authority.py` | Datastore-bound, expiring HMAC authorization for feedback |
| `learning.py` | Evidence admission, decay, retraction, and actor aggregates |
| `growth.py` / `weights.py` | Bounded global calibration and capacities |
| `healing.py` | Numerical recovery toward baseline and evidence decay |
| `identity.py` | Scope-bound HMAC pseudonyms; raw identity is never persisted |
| `storage.py` | Locked, atomic, versioned JSON state storage |
| `prompt.py` | Identifier-free payload and Responses API message adapter |
| `fate_service.py` | Disabled-by-default async Fate boundary, live RAM sensing, per-guild sector orchestration, and future cortex handoff |
| `evaluation.py` / `evaluation_data/` | Deterministic offline calibration over explicitly synthetic, non-personal cases |
| `defaults.json` | Editable baselines, thresholds, caps, and half-lives |
| `runtime/` | Ignored local state files |
| `docs/` | Scientific scope, privacy model, and prompt contract |
| `tests/` | Unit and end-to-end boundary tests |

The public flow is:

```text
trusted external + operational + action + rhythm signals
       + ephemeral profile + relationship + social context
                     -> exact bounded numeric cues
                     -> isolated transient dimensional state
                     -> pain + panic + riddle regulatory motifs
                     -> overlapping autonomic branch proposals
                     -> needs + prediction error + bounded temporal trajectory
                     -> bounded attention
                     -> gated discovery question or no question
                     -> feeling + functional-workspace developer contexts
                     -> future language cortex -> authorized action outcome
                                               -> next body frame

authorized reviewed outcome -> bounded evidence ledger -> scoped profile
authorized global review    -> bounded global calibration
```

## A body beyond messages

Messages are only one possible external input. `body/` also represents RAM,
queues, latency, rate limits, compute pressure, errors, energy reserve, the
cost/reversibility/control of attempted actions, sustained load, time pressure,
rest debt, and recovery opportunities. It can additionally sense an ephemeral
profile, relationship continuity, reciprocity, conversational clarity and
pacing, audience exposure, privacy, interruption pressure, and available human
support. Model-authored interpretations and raw message text never enter these
channels.

## Person, relationship, and social senses

`ProfileSenses` can receive the current display name, bio, pronouns, visible
profile structure, account/membership age, and a profile-change signal. Name,
bio, and pronoun strings are short-lived inputs: `SensorFrame.cues()` reduces
them to bounded profile-presence and self-expression values, and neither
`Observation`, the JSON state store, learning evidence, nor the functional
workspace can contain the strings. Profile richness, age, roles, name, bio,
pronouns, and avatar/banner presence are deliberately excluded from the risk
model. They cannot make a person safer, more dangerous, more honest, or more
trusted.

`RelationalSenses`, `InteractionDynamics`, and `EnvironmentalSenses` add
familiarity, reciprocity, continuity, shared context, engagement, clarity,
responsiveness, topic continuity, pace and correction pressure, boundary
alignment, audience exposure, privacy, interruption pressure, channel
familiarity, and human-support availability. These deepen current connection,
attention, attunement, personalization, pacing, and disclosure restraint while
remaining numeric and bounded.

Profile text reaches a future language provider only through the separate
`organism.person.v1` developer context and only when the trusted application
sets `personalization_consent=True`. Addressing the person by display name also
requires `name_use_allowed=True`. The context is current-step-only, labels all
profile strings as untrusted data, escapes its wrapper delimiters, and grants no
authority. Applications must collect real user consent before setting these
flags; they are not a substitute for consent UI or policy.

```python
from organism.body import ProfileSenses, RelationalSenses, SensorFrame

frame = SensorFrame(
    event_id="message-1001",
    actor_ref="discord-user-456",
    profile=ProfileSenses(
        display_name="Aster",
        bio="I build tiny robots and grow herbs.",
        pronouns="they/them",
        avatar_present=True,
        personalization_consent=True,
        name_use_allowed=True,
    ),
    relationship=RelationalSenses(
        familiarity=0.7,
        reciprocity=0.8,
        continuity=0.75,
    ),
)

# The service adds person context only because consent is true.
# See the Fate integration example below for service construction.
packet, response_input = await service.cortex_input(
    "guild-123", frame, "What should we build next?"
)
```

Fate samples host RAM pressure on every service frame. Below 70 percent,
memory use is treated as ordinary. Near the cap it becomes a nonlinear
constraint: demand and energy cost rise, controllability falls, activation and
load can rise, and conservation/resource-pressure focus can become dominant.
This is the software analogue of being condensed or having little room to
maneuver, not a claim of literal distress. Resource pressure cannot write
member caution, grant or revoke permissions, moderate, punish, or retaliate.

The safe baseline is an attractor, not an unconditional winner. Threat and
boundary pressure recruit protection quickly. Movement in the safer direction
uses a smaller support-sensitive gain, so one safe frame cannot erase recent
acute mobilization. Repeated safety, agency, connection, repair, and recovery
progressively lower activation/load and let social engagement become dominant.
Ventral and parasympathetic proposals participate in that release over time;
they do not automatically override credible current threat or overload.

## Pain, panic, and riddle

`motifs.py` derives three independent current-step values from the exact trusted
cues, transitioned dimensional state, and predicted risk. They are deliberately
not new autonomic modes, stored state axes, risk features, diagnoses, or
probabilities, and they do not need to sum to one:

- `pain` is present software integrity and resource strain. It rises with
  realized boundary, energy, demand, and load costs and favors smaller,
  recoverable steps.
- `panic` is an acute low-control alarm. It requires alarm pressure together
  with diminished control and an activation, uncertainty, or novelty signal;
  it favors pacing, verification, and less tool autonomy without triggering
  punishment, reporting, or emergency claims.
- `riddle` is tractable epistemic tension: an unresolved information gap that
  invites inquiry only while enough safety and agency remain. Panic, pain, and
  credible threat suppress it, so unsafe ambiguity does not masquerade as
  curiosity.

The same uncertain situation can therefore become a strong riddle when it is
safe and controllable, or panic when threat is acute and control is low. The
motifs shape existing response guidance and actionable attention domains; they
do not feed back into the calibrated mode selector in the same step. They are
included in short-lived feeling and experience packets, but are not written to
the JSON state, evidence ledger, actor profile, or cross-guild association
store. Residual regulation still decays through the established dimensional
state and workspace continuity.

## Discovery, questions, and identity seeking

`discovery/` converts safe epistemic tension into a separate `InquirySignal`.
Once its safety, agency, uncertainty, riddle, panic, threat, and social-exposure
gates pass, mobilization increases the likelihood of asking a question. This
captures sympathetic curiosity without allowing defensive activation to become
interrogation. The signal is not a fourth mode and cannot affect risk,
permissions, moderation, tools, or evidence admission.

Short-lived experiential momentum never broadens those gates, increases the
inquiry score, supplies moral or person evidence, or makes a person-related
topic eligible. Discovery continues to depend on current trusted cues, the
current feeling packet, explicit topic evidence, and a configured advisor. A
temporal pattern may shape response attention only; it cannot become a review,
identity commitment, moral principle, or behavior exemplar.

Questions use fixed templates and closed choices and target only an explicitly
configured advisor. Observing an episode can queue a question but cannot update
a model. A matching configured advisor's structured review may update bounded
support/challenge aggregates for functional commitments, moral principles, and
riddle kinds. Person-related reviews produce de-identified behavior or
perspective exemplars. Constructive and adverse behavior labels describe only
the reviewed episode, while positive and negative impact remain separate
dimensions; they never create personality, demographic, honesty, danger, or
moral-worth profiles and never retain the source person's key in an exemplar.

The functional identity model is a reviewable description of operating
commitments such as preserving agency, respecting boundaries, repairing harm,
and seeking understanding. It is not selfhood, desire, consciousness, or
literal enjoyment. Reviewed current-guild aggregates may enter the future
cortex through the identifier-free `organism.discovery.cortex.v1` reflection
contract; queue state, questions, answers, timestamps, and identifiers do not.
See the full
[discovery model](docs/discovery_model.md) for scoring, review, retention, and
delivery boundaries.

## Functional experience and continuity

`experience/` adds a deliberately small functional workspace. It compares the
current cues with a bounded expectation, computes surprise, derives current
needs, derives a bounded trajectory from consecutive moments, lets fixed
domains compete for at most four attention slots, carries a decaying focus
trace into the next isolated step, and broadcasts an allowlisted summary to the
future language cortex. RAM saturation therefore affects not only state but
what wins attention.

The trajectory is a deterministic comparison of current and immediately prior
functional values. Its impulse, momentum, flux, volatility, strain direction,
and settling pattern are numeric software summaries, not emotions or evidence
of an inner life. Momentum has a 30-second half-life and a hard 120-second
horizon. A gap, reversed clock, expired predecessor, scope change, actor change,
or capability change resets it. Replaying the same event is idempotent and
cannot advance continuity, momentum, volatility, or revision.

Trajectory and continuity live only in the running service's bounded RAM cache.
They are never written to organism JSON, the evidence ledger, an actor profile,
discovery state, or the association store. Actor-scoped entries use opaque
guild/actor/capability slots; actorless frames are stateless and never inherit
a prior trajectory. Each slot keeps at most eight short-lived coherent
packet/moment records so ordinary delivery replays can return their original
numeric context while only the newest record can be a predecessor. A
process-keyed digest binds each opaque event token to its stable frame and
association inputs; raw user text is not retained. Forgetting a member, purging
a guild, full purge, expiry, or cache eviction removes the relevant entry.

The dependency remains one-way: trusted cues and the already-computed feeling
packet may produce a functional trajectory, which may add only a bounded,
diminishing pull to response attention and continuity. It cannot feed back into
predicted risk, dimensional state, protective posture, autonomic modes,
evidence admission, permissions, moderation, or tool authorization. Current
protection, boundary, verification, agency, and recovery evidence therefore
cannot be displaced by stale momentum.

This implements testable functions often discussed in allostatic,
interoceptive, predictive-processing, and global-workspace accounts. It is not
evidence that the system has phenomenology, emotion, a mood, or a judgment
about any person. See
[computational embodiment](docs/computational_embodiment.md) and
[scientific scope](docs/scientific_scope.md).

## Per-guild memory sectors

`MemorySectorRegistry` derives an opaque HMAC sector ID and separate role keys
for each guild, then keeps that guild in a separately authenticated state file.
The same member is unrelated across guilds by default. Optional cross-guild
recall uses a fourth, distinct association secret and a shared sensory store.
Only explicit reviewed evidence from the closed, code-audited
`ASSOCIATION_TAXONOMY` can enter that store. Its entries describe bounded
interaction cues or runtime conditions such as rapid boundary pressure,
verified repetition, safe follow-through, resource warnings, queue saturation,
and recovery windows. Arbitrary names, IDs, messages, and caller-created tags
are rejected even when they use a valid-looking namespace. Each review names
its source guild and requires a fresh owner-signed authorization over every
field. Exact review replays map to the same inner learning authorization and
therefore cannot consume additional replay quota. A current frame may present
up to eight association-key-signed lookup capabilities; their combined
influence is capped and affects current cues only. It never merges guild member
profiles or searches raw messages for matches.
Because the current shared ledger cannot selectively erase one source guild's
contribution, purging any contributing guild conservatively clears the entire
shared association store rather than retaining unowned provenance.

## Configure three separate secrets

Use three different, stable secrets of at least 32 bytes:

- `identity_key` makes the same authenticated actor resolvable across process
  restarts without storing the raw actor or scope;
- `learning_key` authorizes durable feedback. It does not replace the identity
  key and must not be shared with untrusted cue producers.
- `owner_key` alone authorizes retractions and owner-only global strategy
  calibration. An ordinary learning signer cannot claim owner authority.

Generate each secret once, then keep it stable in an environment variable or
secret manager. Do not regenerate any value on every launch, and never put a
secret in `defaults.json` or the state file.

```powershell
$env:ORGANISM_IDENTITY_KEY = .\venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$env:ORGANISM_LEARNING_KEY = .\venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$env:ORGANISM_OWNER_KEY = .\venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

A configured key that does not match the key identifier recorded in an
existing state file fails closed. Adding, removing, reusing, or replacing a
key after the state file has been created is also a mismatch: evidence
addresses and the security-root authority are immutable for that datastore.
Restore the complete original key set, or deliberately call `purge_all()` to
erase that datastore and start over. The three configured values must be
pairwise distinct. On a brand-new datastore, omitting `learning_key` disables
durable learning, omitting `identity_key` disables durable actor memory, and
omitting `owner_key` disables owner-only events.

The Fate service accepts no secrets in `config.json`. When it is explicitly
enabled, provide three pairwise-distinct secrets through
`FATE_ORGANISM_IDENTITY_KEY`, `FATE_ORGANISM_LEARNING_KEY`, and
`FATE_ORGANISM_OWNER_KEY`, or through their `_PATH` variants. Enabling shared
sensory associations additionally requires a distinct
`FATE_ORGANISM_ASSOCIATION_KEY`. The service derives separate keys for every
opaque guild sector; it does not reuse one state file across guilds.

## Feel and produce response weights

```python
import os

from organism import Cues, Observation, Organism

organism = Organism(
    "organism/runtime/state.json",
    identity_key=os.environ["ORGANISM_IDENTITY_KEY"],
    learning_key=os.environ["ORGANISM_LEARNING_KEY"],
    owner_key=os.environ["ORGANISM_OWNER_KEY"],
)

packet = organism.feel(
    Observation(
        event_id="message-1001",
        scope="guild-123",
        actor_ref="discord-user-456",
        cues=Cues(
            safety=0.4,
            boundary_pressure=0.7,
            uncertainty=0.55,
            controllability=0.8,
        ),
    )
)

print(packet.prompt_payload())
```

`Cues`, `scope`, and `actor_ref` must come from a trusted application layer.
This package deliberately does not analyze raw messages or infer intent,
trauma, personality, health, protected traits, danger, honesty, or identity
from profile text.

Transient state and observation idempotency are isolated by
`actor + scope + capability` (or by `scope + capability` for actorless
observations). They expire after 24 hours by default and are capped at 256
namespaces. Recent event fingerprints are additionally capped at 4,096 across
the whole datastore; least-recently used histories are trimmed first while the
active namespace is preserved. The same raw platform ID maps to unrelated
namespaces and profiles in unrelated scopes. HMAC identifiers are pseudonymous,
not anonymous. Authenticated state is written as compact deterministic JSON to
reduce encode and `fsync` cost; it is not safely hand-editable.

## Authorize every learning event

Every `learn()` call requires an authorization over the complete `Feedback`
value. Ordinary sources use `learning_key`; `OWNER_OVERRIDE` uses `owner_key`:

```python
import os
from datetime import datetime, timezone

from organism import (
    Capability,
    Feedback,
    FeedbackKind,
    FeedbackSource,
    ReasonCode,
    sign_feedback,
)

learning_key = os.environ["ORGANISM_LEARNING_KEY"]
feedback = Feedback(
    event_id="review-2001",
    episode_id="incident-88",
    scope="guild-123",
    actor_ref="discord-user-456",
    capability=Capability.CONVERSATION,
    kind=FeedbackKind.ADVERSE,
    reason=ReasonCode.CONFIRMED_BOUNDARY_VIOLATION,
    severity=0.7,
    confidence=0.9,
    source=FeedbackSource.HUMAN_FEEDBACK,
    verified=True,
    occurred_at=datetime.now(timezone.utc),
)

receipt = organism.learn(
    feedback,
    authorization=sign_feedback(
        feedback,
        learning_key,
        audience=organism.learning_audience(),
    ),
)
```

The `verified` and `source` fields are semantic admission data, not authority.
Setting them directly without a valid signature applies no learning. Changing
any signed feedback field invalidates the authorization. Model suggestions are
rejected even when signed and marked verified.

Each authorization contains a random nonce, issue time, short validity window,
and this datastore's public audience ID. Consumed nonce digests remain briefly
after profile deletion, and `purge_all()` rotates the audience, so an old
authorization cannot recreate deleted data or be used against another store.
Every `Feedback` also requires an explicit, signed `occurred_at`.

Ordinary and owner authorizations have separate replay-history quotas, so a
burst on the ordinary channel cannot prevent an owner retraction. A
security-root HMAC covers the ledger, consumed-token history, audience, and
key identifiers; deletion or mutation of those fields fails closed before
they are used.

Each accepted evidence row receives another authority-key HMAC in the local
ledger. Mutating a persisted row therefore fails closed before aggregation
instead of silently becoming learned state.

The JSON security root detects unauthorized edits to the current file, not a
rollback of the entire file to an older, previously valid snapshot. If the
host or backup channel is adversarial, add an external monotonic version or
transactional datastore with anti-rollback guarantees before production use.

Actor-scoped `ADVERSE`, `SAFE`, and `REPAIR` feedback requires an
`episode_id`. Safe or repair evidence can reduce caution only after adverse
evidence in the **same actor, scope, capability, and incident episode**. It
cannot erase another incident or pre-emptively wash out a later event.

Actor-scoped events update only that actor's bounded profile. They never tune
global cue weights, strategy weights, or capacities. Global calibration is a
separate authenticated channel: it must be actorless, use `scope="global"`,
and use an authenticated or owner-reviewed source. `STRATEGY_OUTCOME` is
stricter and requires `FeedbackSource.OWNER_OVERRIDE`.
Only `STRATEGY_OUTCOME` may carry strategy names or a nonzero reward.

Learning remains bounded and reversible:

- one ordinary episode cannot create a durable guarded posture;
- correlated events share an episode cap, and daily adverse mass is capped;
- full evidence stores reject new records explicitly instead of silently
  evicting another actor's active history;
- minor, moderate, and severe evidence have 7, 30, and 90 day half-lives;
- retained evidence expires after 180 days by default;
- retraction removes a reviewed event's influence immediately;
- `forget(scope=..., actor_ref=...)` deletes one scoped profile and its
  transient namespaces;
- `reset_state()` clears transient activation without deleting evidence;
- `heal(hours=..., support=...)` advances numerical return to baseline;
- `inspect()`, `inspect_profile()`, and `inspect_profiles()` return logically
  current authenticated views without rewriting state; `purge_expired()` is
  the explicit operation that physically prunes expired evidence and stale
  transient namespaces;
- `purge_all()` erases all state and evidence, including data tied to a lost or
  mismatched key, and rotates the learning audience.

Operator views identify their own contracts as `organism.inspect.v1` and
`organism.profile-inspect.v1`. For compatibility, `inspect()["schema_version"]`
still names the persisted state schema and `inspect()["state"]` remains an alias;
new consumers should use `state_schema_version` and `last_transient_state`.
Likewise, profile `guard_activation` names the learned caution aggregate while
the older `activation` alias remains available.

Protection can change firmness, brevity, verification, disclosure restraint,
and an advisory autonomy scale. It cannot grant permissions, weaken safety
rules, ban, report, punish, accuse, shame, threaten, or retaliate.

## Pass a fresh packet to an OpenAI response

`packet.responses_input(user_text)` returns an input array containing an
identifier-free `developer` message followed by the actual `user` message:

```python
# Optional integration; the core package does not depend on the OpenAI SDK.
response = client.responses.create(
    model=os.environ["OPENAI_MODEL"],
    input=packet.responses_input(user_text),
    store=False,
)
```

This follows the official Responses API pattern in which application rules use
the developer role and end-user content stays in the user role. See the
[OpenAI text-generation guide](https://developers.openai.com/api/docs/guides/text#message-roles-and-instruction-following).

The feeling packet contains generic mode and response-style weights, the three
nonliteral regulatory motifs, strategy names, timestamps, and fixed
constraints. It contains no actor key, raw scope,
username, message history, evidence row, or diagnosis. It is nevertheless
profile-derived: when sent alongside the current user message, the provider
can associate those style signals with that API request and any account or
request metadata it already receives. Treat identifier-free as data
minimization, not anonymity.

`OrganismService.cortex_input(...)` may add an identifier-free
`organism.discovery.cortex.v1` developer message when the current guild has
reviewed models. It may then add the consented ephemeral `organism.person.v1`
profile. Unlike the other packets, that optional person message can contain
display name, bio, and pronouns, so enabling it is an explicit provider
disclosure. It is not written to organism state or continuity memory.

Packets expire after five minutes by default. `responses_input()` rejects a
packet before its activation time or at/after `expires_at`; call `feel()` again
instead of reusing stale guidance.

`OrganismService.cortex_input(...)` is the higher-level future-Fate handoff. It
samples live operational state, resolves the exact post-association cues, forms
the feeling and functional workspace in one worker-thread operation, and
returns two private developer messages, optional reviewed discovery and
consented person-profile messages, and then the unchanged user message.
Its trajectory/continuity cache is opaque, capped, RAM-only, and isolated by
guild, actor, and capability. It uses a 30-second momentum half-life and a hard
two-minute predecessor horizon; actorless frames do not reuse it. The
allowlisted `organism.experience.v4` message exposes only coarse bounded
trajectory summaries for the current response over that short-lived history,
never raw prior values, timestamps, identifiers, or event history.

## Trained and ready?

No neural model has been trained. The organism is deterministic, rule-based,
and inspectable. Its numeric defaults were checked by a deterministic grid of
243 calibration candidates against 12 hand-authored, explicitly non-personal
synthetic cases. The current defaults remained selected and score `1.0` on the
included dominance, bounds, strategy-ordering, hysteresis, recovery, and
temporal-regulation checks.

That result establishes consistency with these fixtures, not human ground
truth, subjective experience, neuroscience validity, or production readiness.
The subsystem is ready for local simulation and further integration testing;
it is not ready to collect real member data or drive a live language/provider
path until disclosure, controls, privacy review, representative evals, and
Discord/provider runtime tests are complete.

Run the report with:

```powershell
.\venv\Scripts\python.exe -m organism.evaluation
```

## Fate integration boundary

`fate.py` now constructs the canonical `organism/fate_service.py` boundary from
the `organism` config block. It is disabled by default. Disabled mode reads no
organism secrets, creates no state directory, samples nothing, and processes no
messages. Enabled startup eagerly validates the config, four role secrets when
shared recall is requested, non-symlink state paths, and writable runtime
directories. Deployments must still restrict those directories at the OS
level. Synchronous sensing and JSON storage run through `asyncio.to_thread`. A
shared fail-fast admission gate bounds total pending work and pending work per
opaque guild sector; cancelling an awaiting coroutine does not release its
permit until the underlying worker thread actually exits. Defaults are 64
globally and eight per guild, editable as `max_pending_operations` and
`max_pending_per_guild`.

Startup loads and validates one immutable numeric calibration, then shares that
same object with every current and future guild sector. Editing the calibration
file while the process is running cannot make different guilds behave under
different rules; restart deliberately to adopt a new calibration. Existing
inspection calls authenticate and validate state but do not rewrite or `fsync`
an unchanged document. Shared association profile lookups are read in one
authenticated batch rather than reparsing the same state file for every
reference.

`cortex_input()` also makes short-lived actor-scoped retries idempotent before
sampling live telemetry or mutating persistent organism state. A bounded
recent-event cache preserves coherent feeling/experience pairs across ordinary
A/B/A delivery retries, rejects reuse of an event ID with different organism
inputs, and never retains raw user text. The user message may be resent or
replaced without turning it into organism state. Whitespace-only provider input
fails before any state is created.
Member, guild, discovery, and association deletion operations are lifecycle
barriers: sensing or learning work that was already queued but had not begun is
rejected as stale instead of recreating data after the deletion returns. The
barrier uses one authenticated identifier-free revision plus opaque
domain-specific cross-process locks, so it also holds between multiple service
instances sharing the same state directory without serializing independent
guild sectors. The caller may retry only after deciding the new operation is
still appropriate.

If the owner key itself is lost after the durable marker exists, normal startup
fails closed. Stop every service instance that uses the directory, then call
`MemorySectorRegistry.purge_all_state_after_key_loss(...)` with the replacement
owner key. That explicitly and irreversibly erases every known sector,
association, and discovery document before resealing the empty marker; it is
never an automatic recovery path.

```python
from organism.body import SensorFrame
from organism.fate_service import build_organism_service

service = build_organism_service(
    {
        "enabled": True,
        "state_directory": "./data/organism",
        "cross_guild_associations": False,
    },
    repository_root=".",
)
assert service is not None

# SensorFrame defaults to the local direct-engine scope. The service replaces
# it with the authenticated guild sector, so callers do not duplicate routing.
packet, provider_input = await service.cortex_input(
    "guild-123",
    SensorFrame(event_id="message-1001", actor_ref="discord-user-456"),
    "What should we build next?",
)
```

The required `FATE_ORGANISM_*` secrets still come from the environment or their
`_PATH` variants; they never belong in the configuration mapping above.

There is still no Discord message listener or OpenAI client calling the service.
Current validation is local/static and simulated, not a live Discord, device,
or provider test. Enabling the service alone therefore makes the body boundary
available but does not send messages to it or to ChatGPT. Runtime health reports
this honest state as `standby`, not `ready`.

`inspect_profile()`, `inspect_profiles()`, `forget()`, `purge_all()`,
`inspect_discovery()`, and `purge_discovery()` are internal control-plane
methods, not authenticated remote endpoints. Do not expose them as model tools,
Discord commands, or HTTP handlers until a separate scope-bound
operator/self-deletion authorization layer is placed in front of them. A
learning or owner feedback signature is not control-plane permission.

A future Discord/cortex call site must:

- obtain cues, actors, scopes, feedback, and signatures from authenticated
  application code rather than model text;
- pass only trusted, namespaced association references and require owner-signed
  review before shared sensory learning;
- call `cortex_input()` immediately before the provider request and send only
  its returned input, never the evidence ledger or platform identifiers;
- enforce tool allowlists, Discord permissions, mention suppression,
  moderation policy, and confirmation requirements deterministically outside
  the model prompt. `tool_autonomy_scale` can only reduce autonomy; it grants
  nothing;
- add user-facing disclosure, per-guild enablement, retention/export/delete
  controls, and the corresponding canonical `PRIVACY.md` update before any
  member-specific collection.

## Run it and test it

```powershell
.\venv\Scripts\python.exe -m organism.example
.\venv\Scripts\python.exe -m organism.evaluation
.\venv\Scripts\python.exe -m unittest discover -s organism\tests -v
.\venv\Scripts\python.exe -m unittest checks.organism_experience_dynamics -v
.\venv\Scripts\python.exe -m unittest checks.organism_motifs -v
.\venv\Scripts\python.exe -m unittest checks.organism_discovery -v
.\venv\Scripts\python.exe -m unittest checks.organism_discovery_cortex -v
.\venv\Scripts\python.exe -m unittest checks.organism_hardening -v
```

Read [scientific scope](docs/scientific_scope.md),
[privacy model](docs/privacy_model.md), and
[prompt contract](docs/prompt_contract.md), plus
[computational embodiment](docs/computational_embodiment.md) and the
[discovery model](docs/discovery_model.md), before any production integration.
