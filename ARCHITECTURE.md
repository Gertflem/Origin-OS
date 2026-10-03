# Architecture

How the Constitution's rules map onto the actual modules.

The short version: there are two layers, and the boundary between them is the
architecture. `core/` is privileged; `units/` is ordinary.

## 1. Five primitives

The Constitution permits no foundational concepts beyond these:

| Primitive | Module | Property |
|---|---|---|
| Objects | `core/objects.py` | persistent, append-only, versioned |
| Units | `core/unit.py` | isolated actors, private arena |
| Capabilities | `core/capability.py` | unforgeable, scoped, expiring |
| Messages | `core/message.py` | the only communication channel |
| Nucleus | `core/nucleus.py` | minimal privileged core |

`core/core_verbs.py` is not a sixth primitive. It is the table of verbs the
Nucleus answers, lifted out of `Nucleus` so the dispatch cannot become an
application layer growing inside the privileged core. `CORE_VERBS` is derived
from that table's keys rather than listed beside it.

## 2. The privilege boundary

```text
core/   ── privileged ─────────────────────────────────────
         capability.py   authority tokens
         message.py      the wire
         objects.py      durable versioned state
         unit.py         isolated execution
         nucleus.py      scheduling, routing, enforcement
         constitution.py machine-checkable rules
         bootstrap.py    birth the boot Units, then seal

units/  ── ordinary ──────────────────────────────────────
         console.py      human control surface
         naming.py       names and intent resolution
         object_store.py persistent storage as a service
         watcher.py      detects invariant violations
         improver.py      diagnoses, proposes versions
         agent.py        agent lifecycle template
         demo.py         demo workers
```

Nothing in `units/` is special. The Console, the Naming Unit, and the Object
store hold Capabilities and answer Messages; they have no privileged path into
the system. If a service could do something, a different service with the right
token can do it too.

The rule of thumb: a verb that only the core may answer belongs in `core/`; a
verb that any service could answer given the right token belongs in `units/`.

## 3. Where each rule is enforced

| Rule | Enforced by |
|---|---|
| No ambient authority (invariant 2) | `Nucleus._execute_core_verb` resolves every verb against `msg.caps`; `Nucleus.validate` refuses a token that does not cover the right, target, kind, and holder |
| Delegation only narrows (section 5) | `Nucleus.mint` validates the presented GRANT against the *same* target and kind as the token being minted, so a delegate can attenuate but never widen |
| No shared mutable state (invariant 5) | `Nucleus._enforce_arena` confines every Unit to its own `UnitContext.mem` |
| All lasting state is versioned (invariant 3) | `ObjectStore._do_append` only ever adds a `Version`; there is no update or delete |
| Communication is by Message (invariant 4) | `Nucleus.send_message` is the only path between actors, and it audits the route |
| Units isolated and replaceable | `UnitContext` arenas; `Nucleus.kill`/`freeze`/`_contain` act on one Unit without touching the rest |
| Nothing silently lost (invariant 7) | `_quarantine` preserves damaged snapshots, `history_damage` is reported rather than repaired, `_flush_directory` failures surface as `durability.degraded` |

`core/constitution.py` restates the rules as data (`BOOT_ORDER`, reserved rights)
so the Nucleus and the test suite check them instead of trusting prose.

## 4. The Nucleus

`Nucleus` may schedule Units, create and enforce arenas, create/validate/transfer/
revoke Capabilities, route Messages, handle interrupts, freeze or kill Units, and
birth the boot Units. Nothing else.

The verb table lives in `core/core_verbs.py`, and `_execute_core_verb` delegates
to it. This matters for more than tidiness: a branching `if verb == ...` chain
inside the Nucleus is exactly how an application layer starts growing in the one
place the Constitution says must stay tiny. Two properties are now structural
rather than aspirational. `CORE_VERBS` is derived from the dispatch table, so a
verb cannot be reachable without also being declared. And the table holds no
authority logic of its own — each handler takes the token off the Message and
hands it to the Nucleus method that validates and records it, so the audit trail
stays the Nucleus's to keep.

It drops nearly all power at `seal()`, after bootstrap. From that point every
action — including the core's own — requires a Capability. The Console banner
states this, because it is the property that makes the rest coherent.

## 5. Bootstrap

`core/bootstrap.py` runs a multi-pass sequence:

1. Birth the boot Units, fixing their ids.
2. Create genesis Objects, skipping any that already exist, so a restart with a
   configured `--storage` path never overwrites real state.
3. Spawn the demo Units with empty params and their `code_object_id`.
4. Grant the standing tokens: Guardian to the human, scoped rights to services.
5. `seal()` the Nucleus, then `schedule()`.

Restart respawns demo Units from the effective code version (preferred, else
latest) with this boot's service ids overlaid, which is what makes a restart
indistinguishable from a resume.

## 7. Snapshot integrity

`history_damage` originally checked structure only: seq continuity, dangling
pin/preferred/compacted pointers. That cannot see a payload altered in place — every
seq present, no dangling pointers, and a payload that never existed. A tampered
snapshot loaded as valid history with no quarantine event at all.

Each version now carries a digest of its persisted content, and the load-time audit
reports a mismatch as `history.payload_digest_mismatch`. This is integrity, not
authenticity: it catches accidental corruption and unsophisticated tampering, but it
is not a MAC, so an attacker who can rewrite the file can also recompute the
digests. A digest-less version — a snapshot from before digests existed — is skipped
rather than reported, because its absence is not evidence of tampering.

A real adversary needs a key the store does not hold. That is a later phase's
problem, and the distinction is worth stating rather than blurring: "damaged history
is evidence" now means something checkable, not aspirational.

## 7. Retention

An Object is its full history. `append()` *is* the write — there is no `save()`,
because there is no unsaved state. A Unit holds an id plus a Capability and asks.

Retention is tiered: recent versions at full fidelity, pins never removed, older
versions semantically compacted. Compaction preserves every version's `seq`,
`author`, `step` and `note` forever and appends a marker version naming exactly
what it reclaimed, so the history still accounts for every moment it had. The
preferred version is never reclaimed.

When a `storage_path` is configured, each mutation is written to a temp file,
fsynced, the previous good snapshot kept as `.bak`, then atomically renamed and
the parent directory flushed. A crash anywhere in that sequence leaves either the
old or the new snapshot intact, never neither — which is what the crash matrix in
`tests/test_package.py` asserts at four distinct injection points.

The directory flush is not optional bookkeeping. The rename lives in the parent
directory's metadata, so until that entry is flushed a power failure can lose the
rename even though the file's contents were fsynced. Windows refuses to open a
directory as a file handle, so this needs `CreateFileW` with
`FILE_FLAG_BACKUP_SEMANTICS` and `FlushFileBuffers`. When it still cannot be done,
`_persist` records a `durability.degraded` event instead of swallowing the
failure — the acknowledged version survives, but the operator is told the
guarantee is weaker rather than left to assume it.

Damage is evidence: an unreadable snapshot is quarantined as `.corrupt.<ns>`
rather than deleted, and a damaged-but-parseable snapshot falls back to `.bak`
only when the backup provably loses nothing.

## 8. Retention

`retention.py` is an ordinary Unit that applies section 3's tiering on its own.

It is triggered by appends, not by a clock, and that is a constitutional
constraint rather than a shortcut. Section 4 says Units are reactive and sleep at
zero cost; section 2's power list for the Nucleus does not include timers. A
housekeeping Unit waiting for a deadline would either burn the scheduler spinning
or push a timer into the one component that must stay tiny. So the Object store
tells the Retention Unit when history grew — the event that makes retention
necessary — and the Unit decides whether to act.

It holds `PIN` and `SEND`, and nothing else: no AUDIT, GRANT, REVOKE, KILL, or
SPAWN. A regression asserts that, because a housekeeping Unit holding inspection
or delegation authority has a far larger blast radius than its job needs.

It is a demo Unit, not a sixth boot Unit. Section 9's boot set is the Units the
system cannot run without, and housekeeping is not among them — if this Unit dies
the system still works, history just stops being reclaimed, and `/retention` says
so. Adding it to `BOOT_ORDER` would make it unkillable-by-accident and quietly grow
the set the Constitution fixed.

`retention.report` is deliberately *not* AUDIT-gated, unlike the Watcher's and
Improver's. Those report on the system; this one reports only its own policy and
the sweeps it already performed under authority it demonstrably held, which is the
same thing `ctx.mem` already is.

## 9. Self-healing

`watcher.py` observes invariants and containment events. After
`--escalate-after` containments it asks `improver.py` to act. The Improver reads
the failing Unit's code Object, appends an improved version, and marks it
preferred; the Watcher then respawns the Unit from that version.

Improvements are versions, not edits, so a bad improvement is a bad *version* and
not a bad *state*. The Improver is denied the rights to modify the Nucleus, and
`PROTECTED_KINDS` stops it proposing changes to the Watcher, Object store, or
Console.

## 10. Human control surfaces

Section 8 asks for two: a clean textual Console, and a Studio that is a "spatial
continuous canvas of living Objects and Verbs". Both are ordinary Units, and both
are spawned rather than constitutional — a canvas that cannot be killed is a canvas
that cannot be redesigned.

`console.py` is deliberately unprivileged: five standing tokens (SEND, RESOLVE,
BIND, AUDIT, GRANT). It cannot read a photo, freeze a Unit, or kill one without a
proposal the human confirms.

`studio.py` renders Objects onto a grid grouped by kind, showing version counts and
how many are compacted. Its layout is derived state in its own arena — kill it and
the layout is rebuilt, because a view has no authority over what it views.

It holds AUDIT, SEND and RESOLVE, and *nothing that mutates*: no APPEND, PIN, GRANT,
SPAWN or KILL. That is the constraint that matters. A canvas that draws the whole
system and can also change it would be the most powerful Unit in the runtime, and
acting through it would bypass propose-then-confirm. Selecting an Object on the
canvas therefore routes to the Console, which keeps the intent loop as the only path
to authority.

Recovery reaches the human through the same loop. `history`, `undo` and `restore`
are verbs in the Naming Unit's table, so they propose scoped Capabilities and run
only on confirmation like everything else — there is no unconfirmed "revert" button.

`undo` reads an earlier version and holds no authority over the current state at
all. `restore` moves the preferred pointer, which is the same operation the
Improver uses to promote a repaired Unit. Both are therefore reversible in the same
way, and neither can become destructive: the version that was current stays
addressable by seq. A version reference that does not exist is refused with the
range that does, rather than clamped to the nearest one, because a clamped answer
would be indistinguishable from a real one.

The intent loop the Console implements is section 8's promise:

```text
express → resolve → propose capabilities → confirm → execute → inspectable result
```

Every step is a Message. `confirm` mints exactly the token that was quoted,
scoped to one Object, with an expiry — never a broader grant than was shown.

## 11. Trust boundaries, tested as attacks

`TestAuthorityBoundaries` in the test suite is written adversarially: each test is
an attack that must fail. A security claim is only worth what it is worth when
someone tries to break it, and a test that exercises the happy path proves nothing.

Attacks currently asserted to fail:

- **Spoofed system events.** A `unit.contained` or `store.recovery` from any
  principal other than the one entitled to assert it. SEND proves reachability, not
  authority to claim a system fact — without this check, a forged crash escalated to
  the Improver, which appended to a real code Object and spawned a replacement.
- **Time-widening through GRANT.** A temporary GRANT minting a permanent token.
  Narrowing is allowed; outliving the grantor is not.
- **Escalation.** A delegate widening its own kind-scoped GRANT back to
  namespace-wide. Attenuation is one-directional in every scope.
- **Reserved rights.** A non-human minting GUARDIAN, GRANT or REVOKE.
- **Possession confused with delegation.** One principal spending another's token.
  Without this check, any service could accumulate ambient authority as a side
  effect of being talked to, and attaching the Guardian to a Unit-directed Message
  would hand that Unit the human's escape hatch.
- **Forgery.** A plausible-looking but unissued capability id.
- **Revocation.** A stale handle saved before revocation, which must die immediately.
- **Expiry.** A temporary token one step past its lifetime.
- **Cross-kind inheritance.** A replacement Unit collecting a Watcher's KILL.
- **Re-homing a running Unit.** Authority is only moved for a Unit that can no
  longer act for itself.
- **Unsealed minting.** The sealed core refusing to mint without a GRANT token.
- **Silent refusal.** Every rejection reaching the audit trail, because a refusal
  the operator cannot investigate is not much of a safeguard.

What is deliberately *not* claimed: this is a single-process simulation with no
adversary model beyond a misbehaving Unit. A hostile Unit cannot forge a handle,
but it also cannot be isolated from the host process, so "contained" means frozen
and stripped of authority, not sandboxed.

## 12. Known limitations

- A token may be scoped by Object **id**, by Object **kind**, or left
  namespace-wide, and never by anything broader than what granted it. The
  Improver's write token is scoped to the `code` kind, so it covers every code
  Object including ones created later, and is refused on data. `grants()` fails
  closed: a kind-scoped token rejects a target whose kind cannot be resolved.
- Kinds are plain strings the store asserts. The core cannot verify them — it has
  no code Object and knows nothing about storage — so a store that mislabelled a
  data Object as `code` would be mislabelled for authority too. The core passes
  the kind in as an argument rather than looking it up, which keeps the trust
  boundary explicit instead of hidden inside a lookup.
- The Object store's authoritative in-memory copy lives in its own Unit's arena,
  so killing that Unit mid-run still discards in-memory state. With a configured
  storage path a restart recovers from the snapshot; without one, data is lost.
  This is why the Watcher protects it and the Improver is denied it.
- Version metadata still accumulates on append-heavy Objects. Every sweep appends
  a marker version naming what it reclaimed, which invariant 7 requires, so an
  Object written in a tight loop grows its version list faster than an unwatched
  one. Measured, not estimated: 60 appends to one Object leave 65 version slots of
  which 52 are compacted markers, holding 13 live payloads. Payloads stay bounded,
  which is the point; the slots do not, and each marker's note embeds the reclaimed
  seq list. `min_versions` controls how often a sweep runs, and the store now ticks
  retention every 16 appends rather than every one — the review measured per-append
  ticking at 25 KB of snapshot for a 40-byte live payload, and it made the cost of a
  single write O(total versions in the store). A cap on version slots per Object
  would bound this properly; that is real work, not a knob.
- The Console, the Naming Unit and the Object store are protected from
  self-improvement by `PROTECTED_KINDS` rather than by authority. That list is
  policy in an ordinary Unit, so it is only as trustworthy as the Unit holding
  it — the real structural protection is that `object_store` and `nucleus` have
  no code Object to rewrite.
