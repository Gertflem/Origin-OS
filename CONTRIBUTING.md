# Working on Origin

The rules that govern changes are in `CONSTITUTION.md`. This file is the
practical version: how to work on the codebase without eroding the safety model.

## Agent operating rules

1. Keep all code inside the package layout. No loose modules in the repo root.
2. Preserve the constitution as the source of truth. If it conflicts with a
   convenient shortcut, the shortcut loses.
3. Add a failing regression before a fix whenever behavior changes.
4. Keep human visibility first-class. Every agent action must be inspectable.
5. Prefer message-driven execution over ambient authority.
6. Commit checkpoints after stable milestones, not after speculative work.

This is a project for safe autonomy, not for unchecked automation.

## Environment

Python 3. Install in editable mode:

```bash
py -m pip install -e .
```

Run the suite (75 tests, a few seconds):

```bash
py -m unittest discover -s tests
```

On Windows, use `py` rather than `python` — the `python` alias is frequently
shadowed by the Microsoft Store shim and fails with a message that looks like a
missing install. The suite also spawns a subprocess for the CLI test, which sets
`PYTHONPATH` itself.

## Where to put things

The privilege boundary is the important part of the layout:

- `core/` is the privileged layer. `Nucleus`, `Capability`, `ObjectStore`,
  `Unit`, `Message`.
- `units/` is everything else. Every capability grant, intent resolution, and
  human interaction is an ordinary Unit that happens to hold tokens.
- The top-level `src/origin/*.py` modules are compatibility re-exports only.
  Implementations live in `core/`.

If you add a verb that only the privileged core should answer, it belongs in
`core/`. If it is something a service could do with the right Capability, it
belongs in `units/`. That distinction is the whole architecture.

## Conventions

- Comments explain *why*, especially where a constraint looks arbitrary. The
  existing code documents its reasoning densely; match that.
- Authority failures are reported to the requester, not raised into the runtime.
  A Unit that is refused should learn it was refused.
- Damage is evidence. Corrupt files get quarantined, not deleted, and detected
  inconsistencies get reported, not auto-repaired.
- Any behavior the human is told exists must reach them. A swallowed exception
  that weakens a guarantee is a bug; see the `durability.degraded` event in
  `objects.py` for the pattern.

## Documentation

`CONSTITUTION.md` is the rules and roadmap. `PROGRESS.md` is what is actually
built. `__phase__` in `origin/__init__.py` is the label users see — update all
three together when a phase changes, since the label previously drifted out of
date in three separate places.
