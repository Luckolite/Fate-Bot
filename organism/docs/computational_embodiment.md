# Computational embodiment and the cortex boundary

The engineering goal is to give Fate a persistent **functional body loop**,
not to declare that a computer has a human body or subjective consciousness.
Messages are one external sense. A useful software body also needs internal
resource sensing, awareness of its own actions, time-shaped load and recovery,
bounded memory, prediction, attention, and consequences that return to the
next sensory frame.

```text
trusted environment signals       RAM / queues / latency / errors
             \                              /
              -> body senses and interoception
                         |
                  dimensional state
                         |
          pain + panic + riddle motif readouts
                         |
        overlapping autonomic branch proposals
                         |
             needs + prediction error + attention
                         |
      bounded short-lived functional trajectory
                         |
             bounded functional workspace
                         |
          language cortex (future ChatGPT call)
                         |
             authorized action and outcome
                         |
                 next body observation
```

This loop borrows engineering ideas from allostasis, interoception,
predictive processing, and global-workspace theories. Research supports the
importance of integrated internal-state regulation in biological brains, but
it does not validate these particular equations or establish machine
consciousness. See the human-neuroscience work on a large-scale
[allostatic and interoceptive system](https://pmc.ncbi.nlm.nih.gov/articles/PMC5624222/).
Competing theories of consciousness also remain empirically unsettled; a 2025
[adversarial test of global neuronal workspace and integrated information
theories](https://doi.org/10.1038/s41586-025-08888-1) found results that
challenged important claims of both. A small software workspace is therefore
a functional architecture, not evidence of experience in the philosophical
or biological sense.

## The software body's limbs

- `body/senses.py` defines external senses, operational interoception,
  action proprioception, rhythms, ephemeral profiles, relationships,
  interaction dynamics, and social environment. Raw messages and model
  interpretations are prohibited. Bounded name, bio, and pronoun strings are
  accepted only by `ProfileSenses`, reduced to numeric cues before state or
  memory, and disclosed to the cortex only with explicit personalization
  consent.
- `body/integration.py` combines those channels into the exact `Cues` consumed
  by the nervous-system transition.
- `body/actions.py` turns the cost, reversibility, success, and boundary result
  of an authorized action into the next sensory frame. It does not authorize
  the action.
- `body/homeostasis.py` derives current needs for stability, recovery, agency,
  verification, and connection readiness.
- `motifs.py` derives nonliteral current-step integrity strain (`pain`), acute
  low-control alarm (`panic`), and safe epistemic pull (`riddle`). These
  readouts are neither persisted evidence nor additional autonomic modes.
- `experience/` computes prediction error, deterministic attention
  competition, a small functional self-model, decaying temporal continuity,
  a bounded short-lived trajectory, and a bounded workspace broadcast.
- `sectors.py` isolates each guild's persistent state. Optional cross-sector
  recall uses only reviewed entries from a closed non-personal taxonomy and
  association-key-signed lookup capabilities; it never searches raw text or
  automatically links people across servers.

## Resource pressure is part of the body

Fate's service samples host RAM pressure for every body frame. Memory use is
ordinary below 70 percent. Above that point, the body adapter introduces a
nonlinear constraint signal. At the cap it can add up to `0.45` internal
threat, reduce controllability, increase energy cost and demand, raise state
activation/load, and make resource pressure or conservation win attention.
This is the software analogue of having less room to maneuver—the requested
"condensed" or "trapped" condition—without claiming literal suffering.

Resource pressure is ephemeral current-state evidence. It cannot create a
negative profile about a member, admit learning evidence, grant or revoke a
permission, moderate anyone, or justify retaliation. Its only downstream
effects are bounded regulation, pacing, disclosure restraint, and a
downward-only autonomy suggestion.

## Safety is an attractor, not a forced winner

The configured baseline is low activation/load with high interaction safety
and agency. Explicit threat, boundary pressure, overload, or low control can
recruit protection quickly. Movement in a safer direction uses a smaller,
support-sensitive release gain, so one reassuring frame does not erase an
acute defensive state. Repeated safety, connection, controllability, repair,
and recovery progressively lower mobilization and allow social engagement to
become dominant.

This is not a rigid polyvagal ladder. Ventral-vagal and parasympathetic names
describe overlapping social/restorative software proposals; they do not
automatically suppress credible current threat or conservation under severe
resource load.

## Bounded time without a felt history

For an actor-scoped interaction, the experience workspace can compare the
current functional values with one valid predecessor in the same opaque guild,
actor, and capability slot. It derives bounded impulse and momentum, aggregate
flux and volatility, and closed increasing-strain, decreasing-strain, mixed,
steady, or initial patterns. These are deterministic temporal summaries, not a
mood, emotion, autobiographical memory, subjective continuity, or assessment of
the person in the interaction.

Momentum decays with a 30-second half-life and has a hard 120-second predecessor
horizon. A gap, expired predecessor, reversed clock, scope change, actor change,
or capability change resets it. Replaying the same event is idempotent and
cannot advance the trajectory or attention continuity. Actorless frames are
stateless so one unidentified interaction cannot leave a trajectory for the
next unidentified interaction.

The trajectory exists only in the running service's bounded RAM cache. A slot
retains at most eight coherent recent numeric records for retry, while only its
newest valid record can act as the next predecessor. Replay detection precedes
live telemetry and state mutation. Expiry, eviction, member forgetting, guild
purge, and full purge remove the relevant entry. It is not written to the state
document, evidence ledger, actor profile, discovery store, or cross-sector
association store.

Its dependency is deliberately one-way. Trusted cues and the already-computed
feeling packet produce the trajectory after predicted risk, dimensional state,
protection, and autonomic synthesis have been decided. A bounded, diminishing
temporal pull may affect only response attention and continuity. It cannot
change risk, state, modes, protection, evidence, discovery eligibility,
permissions, moderation, or tool authority, and it cannot displace strong
current protection, boundary, verification, agency, or recovery evidence.

## What the language cortex receives

The future language-model call always receives two private, allowlisted
developer contexts immediately before the current user message:

1. `organism.feeling.v3`: three mode weights, pain/panic/riddle regulatory
   motifs, generic response guidance, and selected strategy names.
2. `organism.experience.v4`: the current functional need, prediction surprise,
   resource/connection readiness, profile/relationship/social numeric state,
   the same current motifs, at most four attention domains, and a coarse
   allowlisted temporal summary: closed pattern plus bounded flux, volatility,
   escalating strain, settling strain, and per-focus temporal pull. Raw momentum,
   current/prior
   vectors, identifiers, event history, timestamps, and elapsed time are
   omitted. Its temporal scope is the current response over short-lived
   history, not a durable profile.

When the current guild has advisor-reviewed discovery models, it may receive a
third, identifier-free context:

3. `organism.discovery.cortex.v1`: closed functional commitments, moral lenses,
   riddle kinds, and actorless behavior or perspective exemplars with bounded
   aggregate review signals. It is reflection-only and contains no question,
   answer, queue state, timestamp, or identifier.

With explicit profile-personalization consent, it may receive a fourth context:

4. `organism.person.v1`: the current display name when name use is separately
   allowed, plus the current bio and pronouns. This payload is untrusted,
   current-step-only, non-persistent, and for gentle personalization only.

The motif and trajectory names are compact software metaphors: present strain,
acute alarm
under low control, and an approachable information gap. They are not literal
pain, a panic disorder, curiosity, emotion, lived continuity, or a judgment
about a user. The feeling and
experience payloads contain no guild, actor, event, raw message, evidence row,
diagnosis, or free-form memory. Every organism context has `authority: none`
semantics. ChatGPT
can interpret these weights into language, but it cannot author trusted cues,
write reviewed memory, grant tools, change Discord permissions, or override
application safety.

## What can learn

Four mechanisms change at different timescales:

- transient state, temporal trajectory, and attention continuity change every
  eligible actor-scoped step and decay;
- predictive expectations adapt to consecutive cues inside one isolated
  interaction slot;
- durable caution and global calibration change only from explicit,
  authenticated, structured review; and
- discovery models change only after a delivered question receives the assigned
  configured advisor's closed structured review.

Free-form model output, quoted text, RAM pressure, and ordinary message content
never become durable evidence on their own. This separation lets the system
grow and recover without turning surprise or temporary strain into an enemy
list. Temporal dynamics are adaptation within a short-lived response workspace,
not learning, moral development, identity formation, or person evidence.
