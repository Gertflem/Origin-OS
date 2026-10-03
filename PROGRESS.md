# Progress

The single checklist for the project. `CONSTITUTION.md` holds the rules and the
phase roadmap; this file records what is actually built and verified.

The summary label lives in `origin/__init__.py` as `__phase__`, which the CLI
banner, console help and `--help` all read. Update it here at the same time.

## Phase 0 — Design stabilization

- [x] Open questions resolved, Constitution locked, working method agreed

## Phase 1 — Pure simulation

The foundational model, proven as a runnable Python package.

- [x] 1.1 Project structure + Constitution file
- [x] 1.2 Capability (unforgeable token)
- [x] 1.3 Message
- [x] 1.4 Object + ObjectStore (append-only versioning)
- [x] 1.5 Unit (basic structure)
- [x] 1.6 Nucleus (minimal core + bootstrap)
- [x] 1.7 Console Unit (first human interface)
- [x] 1.8 Interactive loop and REPL/test path
- [x] 1.9 Basic Naming support
- [x] 1.10 Demo Units + first Improver experiments

## Phase 2 — Persistent object substrate

Durability that survives process death, plus the operator surfaces to make
retention a deliberate, visible choice.

- [x] 2.1 Corrupt main snapshot no longer silently wipes memory: previous good snapshot kept as `.bak`, damaged file quarantined as `.corrupt.<ns>`, load falls back to backup
- [x] 2.2 Recovery events (quarantine/restore/unrecoverable) recorded on the store and shown in `--status`
- [x] 2.2b Recovery events reach the Watcher (and the audit trail as a routed message) via a boot-time `store.recovery` message; the store itself holds no audit access
- [x] 2.3 Load-time history audit: seq gaps and dangling pin/preferred/compacted pointers are detected and reported (`history_damage`, `history.damaged` event), never auto-repaired
- [x] 2.4 Damaged-but-parseable main snapshot falls back to `.bak` only when the backup is clean and contains every version main holds (no data loss); otherwise main is kept and the damage reported
- [x] 2.5 Crash-recovery test matrix: simulated kills during tmp write, mid-backup, before replace, after replace; acknowledged history always survives
- [x] 2.6 Persistence wired into the live system: `boot(storage_path=...)` and `--storage PATH`; genesis Objects are skipped (never overwritten) when they already exist
- [x] 2.7 Restart respawns demo Units from the effective (preferred-else-latest) code version with this boot's service ids overlaid; re-preferring seq 0 is honoured
- [x] 2.8a Compaction bugs fixed: the preferred version is never reclaimed (it was; reading it then raised CompactedError and broke Unit respawn), and the compaction marker keeps the Object's real current state instead of replacing it with bookkeeping
- [x] 2.8b Operator report of reclaimable history: `ObjectStore.reclaimable_report`, the `object.reclaimable` verb, `/reclaimable [keep_recent]`, and a `--status` summary line. Read-only, AUDIT-gated, built on the same selection function `compact` uses, so the preview cannot disagree with the action
- [x] 2.9 Durable rename on Windows: the post-rename directory flush was silently skipped (Windows refuses to open a directory as a file handle) and is now performed via `CreateFileW`/`FlushFileBuffers`; a flush that still fails is reported as a `durability.degraded` event rather than swallowed
- [x] 2.10 Retention tiering runs as policy: `ObjectStore.sweep` applies section 3's tiering store-wide instead of leaving it to one-Operator-at-a-time intervention. Dry run by default, `min_versions` floor so short histories stay whole, selection shared with `compact` and `reclaimable_report`. Exposed as `/sweep [keep_recent] [min_versions] [apply]`

### Phase 2 notes

The crash matrix originally injected the `after_replace` failure by counting
`fsync` calls. That only works on POSIX, so the case silently stopped crashing on
Windows and the suite was red there. It now injects at the directory-flush seam,
which is portable and a more precise description of the crash window.

## Current phase: Phase 3 — Minimal interactive system

- [ ] Studio Unit: the spatial canvas of living Objects and Verbs (section 8's long-term interface)
- [ ] Capability targets addressable by kind/namespace, so a token can be scoped to code Objects rather than all Objects (this is what caps the Improver's APPEND today)
- [ ] Sweep wired into a scheduled Unit so retention runs without the operator typing `/sweep apply`
- [ ] Intent loop widened past the current verb set (deictic follow-ups and compound recipients already work)

## Working rule

The Constitution is the contract; the phase sequence stays intact. Commit
checkpoints after stable milestones, not after speculative work.
