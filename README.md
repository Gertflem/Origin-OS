# Origin

Origin is a capability-based operating system simulation for autonomous AI agents.
It is not a normal app with an AI feature bolted on. It is a small runtime where
agents live as first-class system actors: they send Messages, hold Capabilities,
read and write persistent Objects, and operate under explicit authority rather than
ambient power.

## Why this project exists

The goal is to create a safe operating environment for agentic software.
The core principles are simple:

- no ambient authority
- explicit capability grants
- message-driven coordination
- durable, versioned object memory
- human oversight and auditability
- recovery and self-healing by design

## Project status

This repository is actively evolving toward a minimal agent OS runtime in Python,
with a working package layout, CLI entrypoint, and versioned object persistence.

- Phase 1 (pure simulation) is complete.
- Phase 2 (persistent object substrate) is feature-complete through 2.8b: crash
  recovery, backup and quarantine of damaged snapshots, history audits, restart
  from durable state, safe compaction, and an operator report of reclaimable
  history. Progress is tracked in `PHASE_1_CHECKLIST.md`.

## Quick start

```bash
py -m pip install -e .
py -m origin --help
```

## Run the interactive console

```bash
py -m origin
```

## Run the scripted demo

```bash
py -m origin --demo
```

## Run a system status check

```bash
py -m origin --status
```

## Persist state across runs

```bash
py -m origin --storage origin.json
```

## See what compaction could reclaim

Compaction is a deliberate choice, so Origin shows what it would reclaim before
anything is touched. In the interactive console:

```text
/reclaimable        # keep the 3 most recent versions per Object (default)
/reclaimable 5      # keep the 5 most recent
```

The report is read-only. It lists reclaimable versions and bytes per Object and
store-wide, and never offers pinned, preferred, recent, or already-compacted
versions. `--status` also shows a one-line summary.

## Run the tests

```bash
python -m unittest discover -s tests -v
```

## Project structure

```text
Origin/
├── AGENT_OS_VISION.md
├── ARCHITECTURE.md
├── CONSTITUTION.md
├── README.md
├── pyproject.toml
├── src/
│   └── origin/
│       ├── __init__.py
│       ├── __main__.py
│       ├── bootstrap.py
│       ├── capability.py
│       ├── cli.py
│       ├── constitution.py
│       ├── core/
│       │   ├── __init__.py
│       │   ├── bootstrap.py
│       │   ├── capability.py
│       │   ├── constitution.py
│       │   ├── ids.py
│       │   ├── message.py
│       │   ├── nucleus.py
│       │   ├── objects.py
│       │   └── unit.py
│       ├── human/
│       │   └── __init__.py
│       ├── kernel/
│       │   └── __init__.py
│       ├── main.py
│       ├── message.py
│       ├── messages/
│       │   └── __init__.py
│       ├── objects/
│       │   └── __init__.py
│       ├── services/
│       │   └── __init__.py
│       ├── simulator/
│       │   └── __init__.py
│       └── units/
│           ├── __init__.py
│           ├── console.py
│           ├── demo.py
│           ├── improver.py
│           ├── naming.py
│           ├── object_store.py
│           └── watcher.py
├── tests/
│   └── test_package.py
└── .github/
    └── workflows/
        └── tests.yml
```

Note on layout: the modules at the top level of `src/origin/` (`bootstrap.py`,
`capability.py`, `constitution.py`, `ids.py`, `message.py`, `nucleus.py`,
`objects.py`, `unit.py`) are thin compatibility re-exports. The implementations
live in `src/origin/core/`.

## Why not just use [X]?

Origin is the reference design — the readable, runnable, constitutionally-grounded model. Other projects implement pieces of this; nobody has written the whole picture down as a coherent whole.

## Development philosophy

Origin follows a constitution-first approach:

- the Nucleus stays small and privileged
- authority exists only as explicit Capabilities
- objects are append-only and versioned
- units communicate through Messages
- humans retain oversight through audit and revocation

That makes it a runtime for safe agent behavior rather than a conventional AI application.

## Roadmap

The project is currently moving through the constitutional phases:

- Phase 1: pure simulation and foundational model
- Phase 2: durable object substrate and persistence
- Phase 3: minimal interactive system and richer user-facing workflows
- later phases: stronger security, naming, and agent lifecycle management

## License

This project is under active development. It does not yet carry a final public license decision.
