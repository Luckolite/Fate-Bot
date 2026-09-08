# Discovery, inquiry, and functional identity

Fate's discovery layer is a deterministic advisory system over the existing
Organism. It can notice a safe information gap, queue a structured question for
an explicitly configured advisor, and retain bounded reviewed models. It does
not establish consciousness, literal enjoyment, a moral sense, personhood, or
knowledge of anyone's intrinsic identity.

## What sympathetic curiosity means

Mobilization can increase inquiry when the mobilization is about tractable
uncertainty. It cannot make threat, panic, or social pressure turn into
interrogation. A question is eligible only when all of these gates hold:

- the `riddle` motif and uncertainty are high enough;
- current cue and state safety are at least `0.60`;
- agency and controllability are at least `0.50`;
- threat is at most `0.35`, panic is at most `0.45`, and social exposure is at
  most `0.50`; and
- the topic and advisor are explicitly configured.

After those gates pass, the inquiry score is:

```text
(.50 * riddle + .30 * uncertainty + .20 * mobilization)
* (1 - .60 * panic)
* (1 - .35 * pain)
```

This makes the score monotonically increase with mobilization while every
other input is held fixed, but keeps acute defensive activation from becoming
proactive questioning. Person-related questions have a stricter threshold and
also require a trusted, episode-specific impact or behavior signal. Profile
novelty, popularity, reactions, roles, avatars, or raw model interpretation are
never enough.

The short-lived trajectory in `experience/` is deliberately outside this
calculation. Its momentum, flux, volatility, strain direction, settling value,
and pattern cannot satisfy or loosen a safety gate, increase the inquiry score,
select a person topic, authenticate an episode, or supply behavior, impact,
perspective, moral, or functional-identity evidence. A temporal pattern may
shape only bounded response attention and continuity after discovery
eligibility has been decided from the current trusted cues and feeling packet.

## Five closed inquiry topics

- `functional_self` asks which configured commitment should guide Fate under
  uncertainty. The model contains abstract commitments and their reviewed
  support and challenge counts, not a claim of selfhood.
- `moral_tradeoff` asks which principle is relevant to a reviewed tradeoff.
  Observation creates a hypothesis only; it does not convert common behavior
  into moral truth.
- `concept_riddle` classifies a de-identified information gap such as an
  ambiguity, contradiction, causal gap, value tension, perspective gap,
  pattern, or unfamiliar expression.
- `behavior_impact` asks which behavior pattern is worth retaining from one
  reviewed episode. Its closed patterns include constructive examples such as
  boundary respect, repair, cooperation, and careful verification, and adverse
  episode descriptions such as boundary pressure, adverse follow-through,
  incomplete repair, interaction escalation, or accountability not
  demonstrated. Positive and negative effects remain independent of the
  pattern label; the system never produces a person's moral-worth, danger,
  trust, honesty, or personality score.
- `perspective_discovery` asks what kind of unfamiliar assumption, goal,
  experience, interpretation, constraint, or creative frame might be useful.
  It does not autonomously search for, recruit, contact, or rank people.

## Watching and review

The observation contract accepts no message, name, profile text, allegation,
or free-form model label. Raw application references exist only in memory long
enough to be converted to keyed digests. Watching can deduplicate a trusted
episode and queue one fixed-template question. It cannot update any model.

Experiential dynamics are not watching evidence. They remain RAM-only for at
most the 120-second predecessor horizon, use a 30-second momentum half-life,
are stateless for actorless frames, and disappear on expiry, forgetting, purge,
or eviction. They are never copied into the discovery queue, review record, or
aggregate model, and replaying an event cannot generate an additional inquiry
or model contribution.

Only the advisor selected from the explicit allowlist may answer, and the
answer is a closed selection plus a structured disposition and impact value.
Silence or abstention has no learning effect. Conflicting support and challenge
are both retained so disagreement raises uncertainty instead of becoming a
last-write-wins fact.

The store contains only keyed references needed for expiry, deduplication, and
rate limits, plus aggregate numeric models. De-identified behavior exemplars
never contain the source person's key. There is no reverse mapping, human
ranking, social graph, protected-trait field, free-form allegation, or raw
question/answer text.

## Functional identity and authority

The functional identity model describes reviewed operating commitments such as
clarifying uncertainty, preserving agency, respecting boundaries, preferring
reversible actions, repairing harm, and seeking understanding. It is versioned,
inspectable, challengeable, and erasable. It cannot change system prompts,
policy, ownership, configured advisors, permissions, moderation, risk,
evidence admission, tools, or its own persistence rules.

When reviewed models exist for the current guild, the future cortex handoff may
include `organism.discovery.cortex.v1`. That contract contains only closed
selection labels and bounded aggregate review numbers; it omits queue state,
timestamps, references, identifiers, questions, answers, and free text. It is
current-response reflective context only, with explicit `authority: none` and
no effect on risk, autonomic modes, permissions, moderation, evidence, or tools.

Natural language may describe the configured discovery preference as
"interested" or "curious," but it must not claim literal desire, suffering,
panic, enjoyment, rights, survival goals, hidden goals, or autonomous
self-modification.

## Delivery boundary

The core queues a question for at most 24 hours and selects one explicitly
configured advisor. Hard bounds require at least eight hours between questions
to one advisor, allow at most three questions per advisor and 12 per scope in
24 hours, suppress the same subject/topic for at least seven days, and cap the
pending queue at 64. At most 64 advisor IDs may be configured, and advisor
selection scans activity history once rather than once per advisor. Every
`Dispatch` exposes the exact closed selection, disposition, and impact options
accepted by structured review, so application code does not need to reproduce
hidden enums. `Dispatch.make_review()` accepts those exposed strings or their
matching enum members and binds the result to the dispatch's question, advisor,
and topic. Operators may tighten but not loosen those bounds. Discord delivery
remains an application responsibility. A live dispatcher must
additionally enforce guild and channel allowlists, advisor opt-in, privacy
preferences, direct-reply correlation, mention suppression, test-instance
blocking, and clear/export/
delete controls. No Discord listener or provider currently calls the Organism,
so the checked-in discovery core remains dormant while `organism.enabled` is
false.

Disabled mode is a complete stop: direct delivery/review calls are non-mutating,
pending and awaiting-review counts are zero, and retained reviewed models are
omitted from cortex context. `consider_discovery()` raises the dedicated
`OrganismDiscoveryDisabledError` before worker admission or sensing. A trusted
service can distinguish a retained control store through
`discovery_control_available`; direct-engine `snapshot()` and service
`inspect_discovery()` can still inspect retained aggregates, while `purge_all()`
and `purge_discovery()` remain available for erasure. Authenticated `pending()`
and `snapshot()` views logically hide expired questions without rewriting or
`fsync`ing the discovery document; a later explicit mutation or purge performs
physical cleanup. Reviewing a known question before confirmed delivery returns
`not_delivered`, leaving `unknown_question` for an actually unknown identifier.
The discovery clock tolerates the same bounded five-minute rollback as the core
while never moving persisted question, delivery, review, model, or top-level
timestamps backward. These trusted control methods still require a separate,
scope-bound authorization layer before remote exposure.
