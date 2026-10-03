# Verification brief — reviewing the fixes

Follow-up to `REVIEW_BRIEF.md`. The first review found nine defects and they were
fixed in commits `ccd7dff`, `07846f4` and `a72751f`.

**This brief is narrower and more adversarial than the first one.** It assumes the
fixes are wrong in some way that the tests written alongside them did not catch —
which is exactly what happened last time, twice.

---

## Your task

Review the *fixes*, not the codebase. The previous review already covered the wider
system, so do not re-run that sweep.

The useful question is not "is this fix correct?" It is **"what input makes this fix
fail, and did the accompanying test just agree with whatever the code happened to
do?"**

Treat "this was just changed to fix X" as the hypothesis under attack, never as the
conclusion. The author has now been wrong in ways that all looked correct on review
and only failed on a specific input.

## What to review

```
git log --oneline -4
git show ccd7dff     # _json_default / _flush_directory
git show 07846f4     # Watcher sender checks
git show a72751f     # time attenuation, digests, prefer(), counter, reply()
```

Read the current state of each file too — the fix as committed, not the diff alone.

## Priority 1 — does the digest actually catch tampering?

`core/objects.py`: `version_digest`, `_snapshot`, `_damage_of`, `_build`.

Each version now carries a digest of its persisted content. The claim is that a
snapshot with an altered payload is reported as `history.payload_digest_mismatch`
instead of loading as valid history.

Attack it. The accompanying tests cover exactly one case: swapping a payload on a
fresh Object. Try:

- **Tamper metadata, not payload.** Change `author`, `step`, `note` or `acked` on a
  version. The digest covers these too — does it, actually?
- **Swap payloads between two Objects.** Both digests should fail. Does it?
- **Strip a digest entirely.** The code treats a missing digest as "not tamperable"
  for backwards compatibility. Is that the right call, or does it give an attacker a
  trivial bypass by deleting the field?
- **Tamper a compacted version.** Compacted payloads are legitimately `None` and must
  hash identically across a compact/reload cycle. Does a compacted version's digest
  survive?
- **Reorder versions.** Does seq ordering interact with the digest?
- **Tamper `pins`, `compacted` or `preferred`.** Are those covered, or only payloads?

**Also test the false-positive direction, which matters just as much.** A digest that
reports damage on an innocent snapshot is worse than no digest, because the operator
learns to ignore it. Try hard to make it fire spuriously: compaction, pinning,
preferring, restart-and-reload, a snapshot written by an older version, a snapshot
containing sets or non-JSON-native payloads, unicode, very large payloads.

## Priority 2 — is the Watcher's sender check complete?

`units/watcher.py`. Containment and store-recovery are now refused unless they come
from the Nucleus and the human respectively.

- Which verbs are still accepted from any sender? `improve.done`, `kill.result`,
  `nucleus.sealed` were not guarded. Is any of them exploitable — can a spoofed one
  cause a repair, a retirement, or a misleading audit entry?
- Is the rejection path itself sound? Could a flood of forged claims be used to fill
  the events list or spam the Console?
- Is `NUCLEUS` unforgeable as a sender value, or could a Unit address a Message with
  `sender="nucleus"`?

## Priority 3 — the time-attenuation check

`core/nucleus.py::mint`.

A temporary GRANT can no longer mint a longer-lived or permanent token.

**The check sits inside the `if self._sealed:` branch.** Pre-seal minting does not run
it at all. The author believes that is correct — during bootstrap the human mints
directly under the Guardian, and the sealed core is the only path a delegate can
use — but has not reasoned it through carefully. Attack it:

- Can anything be minted with delegated authority before `seal()`?
- What about a GRANT that has already expired, or `expires_in=0`, or negative?
- Is the comparison `<=` on the step ceiling correct at the boundary?
- Can a delegate mint two tokens whose combined lifetime exceeds the grant, in a way
  that matters?

## Priority 4 — the smaller fixes

- **`prefer()` refuses a compacted seq.** `effective()` returns the preferred version
  without raising. Is there another route to an `effective()` returning `None`? What
  about an Object whose *only* version was compacted?
- **The Counter no longer invents counts.** Check `units/demo.py::counter_handler`
  for any remaining path that reports `counter.done` without a real append, or stays
  silent where it should answer.
- **`Message.reply(sender=...)`.** Now addresses the requester correctly. Any caller
  relying on the old behaviour?

## Reporting

For each finding:

- **What**: one sentence.
- **Where**: `file:line`, and the commit that introduced it if relevant.
- **Why it is wrong**: what breaks, under what input.
- **Evidence**: the smallest snippet or console session that demonstrates it. If you
  did not run it, say so.
- **Severity**: data loss, wrong answer, security guarantee, or cosmetic?

**Rank by severity.** Report "I attacked X and could not break it" explicitly — that
is a useful result, not a gap in the report.

State clearly which findings are **newly introduced by these fixes** versus
**pre-existing**. The author needs to know which is which: a fix that broke something
that used to work is worse than a fix that failed to fix.

Three real findings beat thirty speculative ones. If a fix is fine, say so plainly.
