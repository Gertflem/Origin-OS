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

- [x] 3.1 Capability targets addressable by Object kind, so a token can be scoped to "code Objects" rather than to every Object. The Improver's READ+APPEND is now one kind-scoped token instead of one per known code Object: it covers code created later and is refused on data. Attenuation stays one-directional — a kind-scoped GRANT can mint a narrower token, never a wider one
- [x] 3.2 Retention runs unattended in the Retention Unit (`units/retention.py`), triggered by appends rather than a clock — section 4 says Units sleep at zero cost, and section 2 gives the Nucleus no timer power, so waiting for a deadline would either burn the scheduler or grow the core. It holds PIN and nothing else beyond SEND. Visible at `/retention`, and it is a demo Unit rather than a sixth boot Unit because housekeeping is not something the system cannot run without
- [x] 3.3 Studio Unit — section 8's other human interface (`units/studio.py`): Objects on a grid grouped by kind, with version and compaction counts. Holds AUDIT, SEND and RESOLVE and nothing that mutates, so acting on an Object still goes through the Console's propose-then-confirm loop. Spawned rather than added to `BOOT_ORDER`, because both human interfaces are ordinary Units that may evolve. `/studio`, `/studio layout`, `/studio focus <name>`
- [ ] Studio: the canvas is text-rendered, not graphical. It carries position and grouping, but a real spatial surface would need a display the simulation does not have
- [x] 3.4 Intent loop reaches versioned history: `history`, `undo` and `restore` by plain language, including ordinal and positional references ("the last version", "the second version"). `undo` only ever reads an earlier version — no authority over current state — so it cannot become a destructive operation wearing a reassuring name. `restore` moves the preferred pointer, the same mechanism the Improver uses, so it stays reversible. Nonexistent versions are refused with the real range rather than clamped
- [x] 3.5 Trust boundaries asserted as attacks (Phase 4 groundwork): `TestAuthorityBoundaries` tries to escalate a kind-scoped grant, mint reserved rights as a delegate, present another principal's token, use a forged handle, use a revoked copy, use an expired token, inherit authority across kinds, and re-home a running Unit's tokens. All eleven must fail, and they do. The security model was already sound — this makes the claim falsifiable rather than asserted
- [x] 3.6 First independent adversarial review (`REVIEW_BRIEF.md`), and the fixes it produced:
  - `_json_default` returned `None` for non-set payloads, so acknowledged versions were written to disk as `null`. Author's own regression from the doc-consolidation session, invisible from Windows
  - `_flush_directory` held two stray lines that raised `NameError` on every POSIX persist. That platform has never worked
  - the Watcher accepted `unit.contained` and `store.recovery` from any holder of SEND, so any service could fabricate a crash and have the Improver rewrite real code at runtime
  - a temporary GRANT could mint a permanent token: time was the one axis attenuation did not cover
  - the Studio had no injected validator, so `studio.report` contained the Studio with its own verb
  - the Counter defaulted an unreadable reply to 0 and reported refused appends as `counter.done` — inventing counts
  - `Message.reply(sender=...)` misaddressed the reply to the override instead of the requester
  - `prefer()` accepted a compacted seq, so `effective()` handed consumers a `None` state and restart could respawn a Unit from nothing
  - snapshot integrity was structural only: a tampered payload loaded as valid history. Each version now carries a digest of its persisted content
- [x] 3.7 Verification review of the fixes (`VERIFY_BRIEF.md`), and what it found:
  - **A malformed snapshot crashed the boot.** Valid JSON is not a valid snapshot: a missing `kind`, a missing `payload`, or a non-dict top level all raised out of the constructor, wedging every later boot with the bad file in place and `.bak` never consulted. `_build` is now guarded, the file is quarantined, `.bak` is tried, and the outcome reported
  - **ctypes truncated 64-bit handles.** Only `CreateFileW` had `argtypes`, so `FlushFileBuffers` and `CloseHandle` received `c_int`. A large handle was flushed as one small integer and closed as another — in the code path that exists to make the rename durable. Invisible because handles are small on an idle machine
  - **Integrity was per-version only.** Moving `preferred` or relabelling `kind` left every version byte-identical, so the load was clean while `effective()` returned something else. `kind` matters because the kind-scoped authority layer reads it from the file. Objects now carry an object-level digest
  - **The `mint` verb dropped `target_kind`,** so a kind-scoped GRANT failed closed on the wire — the only path a Unit has. The delegation guarantee was false as shipped, and the in-process test missed it by never touching the wire
  - **`.tmp` promotion was silent.** A crash during fsync left a complete `.tmp` that returned as a version the caller was told had failed, with `recovery_events == []`. A valid older `.tmp` was also deleted before `main` was examined, discarding a snapshot that would have healed a corrupt main
  - **`retention.configure` was ungated.** Any principal that could address the Unit could set `min_versions=0` and have the next append trigger near-total compaction under the Retention Unit's own token
  - **A forged `nucleus.sealed` was believed** by the Watcher, Console and Improver
  - **Retention ticked on every append,** making one write cost O(total versions in the store)
  - Two more of the author's, found while fixing these: the Retention Unit had no injected validator (so the new gate contained it on a *valid* request), and `configure` wrote `ctx.mem[key]` — a key nothing reads — so it appeared to succeed while changing nothing
- [x] 3.8 False-positive verification of the digests, run by the project owner. Seven innocent paths (pin, prefer, compact, acknowledge, a real retention sweep through the booted system, full restart against a storage path, double reload) all reload with zero findings, and both positive controls behave. Quiet because `version_digest` is deterministic, one encoder is used on both sides, and every mutation funnels through `_persist → _snapshot`. This is a property, not a coincidence — which is what makes it worth relying on
- [ ] **The digest verifies file-vs-file consistency, not authoring-time truth.** Found by the project owner: `obj.versions[0].payload["n"] = 2` mutates history in place, and the next persist rewrites the digest over the mutated content, so the original acknowledged version is gone with `history_damage == []`. `Version` is frozen but `payload` is not — the freeze protects the fields, not the contents. No in-tree path does this; the fix is to hash a stored copy rather than the live object, at the cost of a copy per version. **No regression pins this boundary yet**
- [ ] The digest is integrity, not authenticity — an attacker who can rewrite the file can recompute it. A MAC the store does not hold the key for is the real fix, and is later-phase work
- [ ] Intent loop: the verb table is still rule-based. That is deliberate (section 8 wants every step inspectable) but it means coverage grows one verb at a time

## Working rule

The Constitution is the contract; the phase sequence stays intact. Commit
checkpoints after stable milestones, not after speculative work.
