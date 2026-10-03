# Phase 1 — Pure Simulation Checklist

This document tracks the Phase 1 steps required by the Constitution and keeps the project moving in the intended order.

## Checklist

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

## Status

Phase 1 is now complete enough to treat the simulation as a stable operating model. The project has proven the core primitives and the bootstrap flow in a working Python package layout.

## Next step

The project should now move to Phase 2: Persistent Object Substrate.

### Phase 2 focus areas

- real durability guarantees
- stronger crash recovery semantics
- compaction and retention policy
- better persistence guarantees across process boundaries
- deeper validation of acknowledged object versions

## Working rule

Do not jump ahead into Phase 2 until the working Phase 1 model is stable and inspected. The constitution is the contract; we keep the sequence intact.

## Phase 2 progress

- [x] 2.1 Corrupt main snapshot no longer silently wipes memory: previous good snapshot kept as `.bak`, damaged file quarantined as `.corrupt.<ns>`, load falls back to backup
- [x] 2.2 Recovery events (quarantine/restore/unrecoverable) recorded on the store and shown in `--status`
- [ ] 2.2b Forward recovery events into the Nucleus audit trail (needs a message-based path, store must not gain ambient access)
- [x] 2.3 Load-time history audit: seq gaps and dangling pin/preferred/compacted pointers are detected and reported (`history_damage`, `history.damaged` event), never auto-repaired
- [x] 2.4 Damaged-but-parseable main snapshot falls back to `.bak` only when the backup is clean and contains every version main holds (no data loss); otherwise main is kept and the damage reported
- [x] 2.5 Crash-recovery test matrix: simulated kills during tmp write, mid-backup, before replace, after replace; acknowledged history always survives
- [x] 2.6 Persistence wired into the live system: `boot(storage_path=...)` and `--storage PATH`; genesis Objects are skipped (never overwritten) when they already exist. Before this, the durable store was only reachable from tests.
- [x] 2.7 Restart respawns demo Units from the effective (preferred-else-latest) code version with this boot's service ids overlaid; re-preferring seq 0 is honoured
