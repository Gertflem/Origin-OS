# Origin

A capability-based operating system for autonomous AI agents, simulated in pure Python.

Not an app with an AI feature bolted on. A small runtime where agents live as
first-class system actors: they send Messages, hold Capabilities, read and write
persistent Objects, and operate under explicit authority rather than ambient power.

## Why this project exists

To build a safe operating environment for agentic software:

- no ambient authority
- explicit, scoped, expiring capability grants
- message-driven coordination
- durable, versioned object memory
- human oversight and auditability
- recovery and self-healing by design

`VISION.md` argues why an AI-native OS should not be organized around "run
programs" and "save files". `CONSTITUTION.md` is the rulebook.
`ARCHITECTURE.md` maps those rules onto the actual modules.

## Project status

Phases 0–2 are complete. `PROGRESS.md` is the itemised record; the phase label the
CLI prints lives in `origin/__init__.py` as `__phase__`.

| Phase | State |
|---|---|
| 0 — Design stabilization | complete |
| 1 — Pure simulation | complete (1.1–1.10) |
| 2 — Persistent object substrate | complete (2.1–2.9) |
| 3 — Minimal interactive system | current |

The intent loop works end to end: a plain-language request becomes a scoped
Capability proposal, the human confirms, and the Unit appends a new version.

| Phase | State |
|---|---|
| 0 — Design stabilization | complete |
| 1 — Pure simulation | complete (1.1–1.10) |
| 2 — Persistent object substrate | complete (2.1–2.10) |
| 3 — Minimal interactive system | current |

## Quick start

```bash
py -m pip install -e .
py -m origin --help
```

Run the interactive console:

```bash
py -m origin
```

Run the scripted tour:

```bash
py -m origin --demo
```

By default the Retention Unit is on. Turn it off with
`--no-retention` if you want to watch history grow untouched.

Print a runtime summary without entering the REPL:

```bash
py -m origin --status
```

Persist state across runs:

```bash
py -m origin --storage origin.json
```

## Try the intent loop

Type an ordinary sentence. The system resolves it, tells you exactly which
authority the action requires, and waits for you to confirm.

```text
you> brighten the beach photo by 20
PROPOSED: brighten the beach photo by 20
  authority cost:
    - READ+APPEND on the beach photo, granted to the photo Unit, expires in 24 steps
  type 'confirm' to proceed or 'cancel' to drop it.
you> confirm
RESULT:
  brighten: seq 0 -> 1 (brightened by 20)
```

Nothing happens without the Capability. `confirm` mints exactly the token that
was quoted, scoped to one Object, with an expiry.

The same loop reaches the versioned history, which is the point — reversal is not a
special feature but the substrate the system already uses on itself:

```text
history the beach photo        # every version, with author, step and note
undo the beach photo           # read the version before the current one
restore version 0 of the beach photo   # make an earlier version current again
```

`restore` moves the preferred pointer rather than deleting anything, so the version
it superseded is still there — the same mechanism the Improver uses to promote a
repaired Unit, and reversible for the same reason. `undo` holds no authority over
the current state at all; it only reads an earlier version, so it cannot quietly
become a destructive operation. A version that does not exist is refused with the
range that does, rather than clamped to the nearest one.

## See what compaction could reclaim

Compaction is a deliberate choice, so Origin shows what it would reclaim before
anything is touched:

```text
/reclaimable        # keep the 3 most recent versions per Object (default)
/reclaimable 5      # keep the 5 most recent
```

The report is read-only. It lists reclaimable versions and bytes per Object and
store-wide, and never offers pinned, preferred, recent, or already-compacted
versions. `--status` also shows a one-line summary.

## Run the retention policy

`/reclaimable` answers "what *could* be reclaimed". `/sweep` applies section 3's
tiering across the whole store, and previews until you say otherwise:

```text
/sweep                # preview: keeping 3 recent per Object, skipping Objects under 32 versions
/sweep 5 16           # preview with a different window and floor
/sweep 3 8 apply      # actually compact
```

Compaction is tiered, never deletion: a reclaimed version keeps its seq, author,
step and note forever, and the sweep appends a marker version naming exactly what
it reclaimed. Pinned and preferred versions are never reclaimed.

You do not have to run any of that. The Retention Unit applies the same policy on
its own whenever an append grows an Object past the threshold, and `/retention`
shows its policy and its log:

```text
/retention
```

It holds `PIN` and nothing else beyond `SEND` — no inspection, no delegation, no
spawn. If it dies the system keeps working; history just stops being reclaimed and
`/retention` says so.

## Look at the canvas

Section 8 promises two human interfaces: the textual Console, and a spatial canvas
of living Objects. Both are ordinary Units.

```text
/studio             # render the canvas
/studio layout      # which cell each Object occupies
/studio focus beach photo
```

```text
Origin Studio - living Objects, grouped by kind

  obj_co..(16) [1v]          | obj_co..(14) [1v]          | obj_co..(13) [1v]
  obj_co..(14) [1v]          | tally [1v]                 | obj_fl..(13) [1v]
  anne [1v]                  | david [1v]                 | * beach photo [1v]
  sunset photo [1v]          |                            |

  10 Objects in 5 kinds.
  [Nv] = N versions, and Nc of those are compacted. * marks the focused Object.
```

It is a view, not a control. The Studio holds AUDIT, SEND and RESOLVE and nothing
that mutates, so acting on an Object still goes through an intent and a confirmed
grant. Kill it and the layout rebuilds — a view has no authority over what it views.

## Run the tests

```bash
py -m unittest discover -s tests
```

On Windows use `py`, not `python` — the `python` alias is often shadowed by the
Microsoft Store shim.

## Project structure

```text
Origin/
├── ARCHITECTURE.md      how the rules map onto the modules
├── CONSTITUTION.md      the rules and the phase roadmap (source of truth)
├── CONTRIBUTING.md      how to work on this codebase
├── PROGRESS.md          what is built and verified
├── VISION.md            why an OS for agents looks like this
├── pyproject.toml
├── src/origin/
│   ├── __main__.py      module entrypoint
│   ├── main.py          CLI, REPL, scripted demo
│   ├── core/            the privileged layer
│   │   ├── capability.py    unforgeable authority tokens
│   │   ├── ids.py           principal and object id helpers
│   │   ├── message.py       the only communication channel
│   │   ├── objects.py       persistent append-only versioned Objects
│   │   ├── unit.py          isolated execution actors
│   │   ├── nucleus.py       the tiny privileged core
│   │   ├── constitution.py  machine-checkable restatement of the rules
│   │   └── bootstrap.py     birth the boot Units, then seal
│   └── units/           ordinary Units — the majority of the system
│       ├── agent.py         agent lifecycle template
│       ├── console.py       human control surface
│       ├── demo.py          demo worker Units
│       ├── improver.py      diagnoses failures, proposes versions
│       ├── naming.py        names, intent resolution
│       ├── object_store.py  persistent storage as a service
│       ├── retention.py     applies the tiered-retention policy
│       ├── studio.py        spatial canvas of living Objects
│       └── watcher.py       detects invariant violations
├── tests/
│   └── test_package.py
└── .github/workflows/tests.yml
```

The modules at the top level of `src/origin/` (`bootstrap.py`, `capability.py`,
`constitution.py`, `ids.py`, `message.py`, `nucleus.py`, `objects.py`, `unit.py`,
`cli.py`) are thin compatibility re-exports. The implementations live in
`core/`.

## Why not just use [X]?

Origin is the reference design — the readable, runnable,
constitutionally-grounded model. Other projects implement pieces of this; nobody
has written the whole picture down as a coherent whole.

## License

See [LICENSE](LICENSE).
