# Adversarial review brief — Origin OS

Paste everything below to a fresh model, together with the repo, as-is. Do not
add context of your own; the point is to get an opinion formed without the
history that produced the code.

---

## Your task

You are reviewing a codebase called **Origin**. It is a capability-based operating
system simulation for autonomous AI agents, written in Python.

**Your job is to find bugs.** Not to confirm that it works, not to praise the
design, not to suggest features. Find what is broken, what is claimed but false,
and what is quietly wrong.

Assume the author has been both the implementer and the reviewer, and is
therefore blind to at least some of the problems. That assumption is the reason
you exist. Be skeptical of anything that looks well-documented: confident prose is
not evidence, and code comments in this repo are frequently more optimistic than
the code they describe.

## The repository

Location: `C:\Users\flemi\OneDrive\Desktop\Origin`

Read in this order:

1. `CONSTITUTION.md` — the rules the system claims to obey. This is the spec.
   If the code and the constitution disagree, that is a finding.
2. `ARCHITECTURE.md` — what the author believes the architecture is.
3. `PROGRESS.md` — what is claimed complete.
4. `README.md`
5. `src/origin/core/` — the privileged layer. Start with `capability.py`,
   `nucleus.py`, `objects.py`, then `core_verbs.py`.
6. `src/origin/units/` — the ordinary Units.
7. `tests/test_package.py`

Run the suite (Windows, use `py` not `python`):

    py -m unittest discover -s tests

Also useful:

    py -m origin --status
    py -m origin --demo
    py -m origin --help

You can drive the console by piping text into it:

    "brighten the beach photo by 20`nconfirm" | py -m origin

## Where to attack

These are the areas the author is least confident about, in rough priority order.
Spend most of your effort here.

### 1. The durability path (`core/objects.py`)

The system claims an acknowledged version survives process death. Persistence is
write-temp → fsync → copy the old file to `.bak` → `os.replace` → flush the parent
directory.

Look for:
- Any window where an acknowledged version can be lost. Walk the crash matrix in
  the test suite and check whether the injected failures actually cover every
  dangerous point, or whether some point is claimed but never tested.
- Whether the `.bak` fallback can ever lose data. The author believes it cannot.
- Whether `history_damage` detection can be fooled by a specific sequence of
  appends, pins, preferences and compactions.
- Whether the Windows directory-flush path (`_flush_directory_windows`, using ctypes
  `CreateFileW` + `FlushFileBuffers`) can leak handles, fail silently, or report
  success when the data is not actually durable.
- Whether a corrupt snapshot can be crafted that loads as valid but with wrong
  content.

### 2. Automatic retention (`core/objects.py::sweep`, `units/retention.py`)

Section 3 of the constitution says retention is tiered. It now runs unattended,
triggered by appends.

Look for:
- Whether metadata grows without bound in a way the author has rationalised. The
  author knows each sweep appends a marker version and has documented that
  version *metadata* accumulates. Quantify it: how fast, and does anything break?
- Whether a sweep can reclaim a version that something still depends on. The
  preferred version is claimed never to be reclaimed. Verify that, and check the
  restart/respawn path that depends on it.
- Whether the `apply` flag polarity can ever be wrong on some path.
- Whether a Unit can trigger a sweep that reclaims something outside its authority.

### 3. The authority model (`core/capability.py`, `core/nucleus.py`)

`validate()` is claimed to be the single choke point every exercise of authority
passes through.

Look for:
- Any code path that performs a privileged action *without* calling `validate()`.
  Grep for every use of a capability and check each one.
- Whether `grants()` can be made to return `True` when it should not — the
  kind-scoping logic in particular, and the way an unknown kind is handled.
- Whether `mint()` can produce a token broader than the GRANT that authorised it.
  The author believes attenuation is one-directional in every scope. Try to break
  it.
- Whether `_rehome` can be used to escalate. It transfers a frozen Unit's authority
  to a same-kind replacement. Is "same kind" a sufficient restriction?
- Whether any Unit can accumulate ambient authority as a side effect of being
  talked to.
- Whether the reserved rights (`GUARDIAN`, `GRANT`, `REVOKE`) are truly
  human-only, including pre-seal.

### 4. The tests themselves

`tests/test_package.py` has 111 tests. Assume they are insufficient.

Look for:
- Tests that assert implementation details rather than behaviour, so they pass
  even when the behaviour is wrong.
- Tests that were clearly written to match code that already exists. The author
  admits this is a risk.
- Claims made in `PROGRESS.md` or `ARCHITECTURE.md` that no test actually covers.
- Obvious missing cases: what happens on a second concurrent-ish append, on a
  restart mid-write, on an Object with a huge number of versions, on a malformed
  snapshot, on a target that does not exist?

### 5. Crash containment and self-healing (`core/nucleus.py`, `units/watcher.py`, `units/improver.py`)

Look for:
- A Unit that can crash the system rather than just itself.
- Ways to make the Watcher and Improver disagree, or to prevent a repair.
- Whether the self-healing loop can loop forever, or stop healing.
- Whether a Unit can talk to anything it has no token reaching.

## What counts as a finding

- A bug that produces wrong behaviour, on some input or some sequence.
- A documented guarantee that is not actually true.
- A test that cannot fail.
- A place where an exception is swallowed and the weakened guarantee is not
  reported anywhere.
- Unbounded growth that will eventually break something.

## What does not count as a finding

- Style, naming, formatting.
- Missing tests for code you cannot find a bug in.
- Feature requests. This is explicitly not what you were asked for.
- Disagreement with the design. The constitution is the spec; if the code obeys
  it, that is not a bug even if you would have designed it differently.

## Reporting

For each finding, give:

- **What**: one sentence, the specific defect.
- **Where**: `file:line`.
- **Why it is wrong**: what breaks, and under what conditions.
- **Evidence**: the smallest snippet of code, test, or console session that
  demonstrates it. If you did not actually run it, say so — do not imply you
  verified something you only reasoned about.
- **Severity**: does it lose data, break a security guarantee, produce a wrong
  answer, or merely look untidy?

Rank findings by severity. If you find nothing in an area, say so explicitly
rather than inventing something — "I attacked the durability path and could not
break it" is a useful result. A report with three real findings beats one with
thirty speculative ones.

If you find something the author already documented as a known limitation, check
whether the documentation is accurate and then move on. The author would rather
know the limitation is real than hear about it again.
