"""The Retention Unit — section 3's tiering policy, running on its own.

Constitution section 3 states retention as a policy: recent versions at full
fidelity, pins never removed, older history semantically compacted. Until now
that policy only ran when a human typed `/sweep apply`, which meant in practice it
did not run at all and history grew until someone remembered. This Unit runs it.

**Why a Unit and not a core timer.** Section 2 gives the Nucleus an exhaustive
list of powers and "run retention periodically" is not on it. A timer in the
privileged core would be an unlisted power, so this is an ordinary Unit that
happens to be responsible for housekeeping. It can be wrong, frozen, replaced, and
its decisions are inspectable — which is what section 8 asks for.

**Why it is reactive rather than clocked.** Section 4 says Units sleep at zero
cost until a Message arrives, and this system has no notion of wall-clock time.
A Unit that spun waiting for a deadline would burn the scheduler's step budget
doing nothing. So this Unit does its work *in response to history growing* — the
Object store tells it an append happened, and it decides whether enough has
accumulated to be worth reclaiming. Nothing ticks; the trigger is the thing that
makes retention necessary.

**What it is allowed to do.** It holds PIN, because retention is a human decision
(section 5: authority exists only as explicit Capabilities) and compaction is
gated on PIN for the same reason. It holds no AUDIT, no GRANT, no KILL, and no
SPAWN: it may reclaim payloads and report what it did, and that is all. It cannot
mint, cannot spawn, and cannot inspect the wider system.

**Why compaction rather than deletion.** Every reclaimed version keeps its seq,
author, step and note forever, and each sweep appends a marker version naming
exactly what it reclaimed. Reclaiming is therefore lossy in payload and lossless
in history, which is what invariant 7 requires: nothing the human has been told
exists may be silently lost. Pinned and preferred versions are never reclaimed, so
the version a restart respawns from always survives.

**The cost of doing this automatically.** Because every sweep appends its own
marker version, an append-heavy Object grows its version list faster than it would
otherwise: one marker per sweep on top of each write. Payloads stay bounded, which
is the point, but version *metadata* still accumulates. That is the deliberate
trade — invariant 7 is worth more than a bounded seq list, and the metadata is
small next to the payloads it replaces. It does mean `min_versions` is the knob
that controls how often this happens, and the default of 32 keeps it rare.
"""

from __future__ import annotations

from typing import Optional

from ..capability import CapabilityError, Right
from ..message import Message
from ..unit import UnitContext, is_answer, unit_type

#: Append-only record of every sweep this Unit has run, including the ones that
#: decided to do nothing. A retention policy that can act silently is exactly the
#: kind of thing section 8 forbids.
LOG_LIMIT = 200


@unit_type("retention")
def retention_handler(ctx: UnitContext, msg: Message) -> None:
    params = ctx.mem["params"]
    services: dict[str, str] = params.get("services", {})
    #: Every step at which this Unit last considered a sweep. Compared against
    #: ctx.step to decide whether enough history has accumulated.
    last_sweep: int = ctx.mem.setdefault("last_sweep", ctx.step)
    log: list[dict] = ctx.mem.setdefault("log", [])
    #: keep_recent / min_versions mirror `ObjectStore.sweep`. Kept here because the
    #: policy belongs to this Unit; the store only executes it.
    keep_recent = int(params.get("keep_recent", 3))
    min_versions = int(params.get("min_versions", 32))
    #: How many steps must pass between automatic sweeps. Zero means "every time".
    interval = int(params.get("interval", 0))
    enabled = bool(params.get("enabled", True))

    if msg.verb == "retention.tick":
        # The store saw an append. Decide whether that is enough to act on.
        if not enabled:
            _record(log, {"event": "tick_ignored", "at_step": ctx.step, "reason": "disabled"})
            return
        if ctx.step - last_sweep < interval:
            _record(
                log,
                {"event": "tick_deferred", "at_step": ctx.step, "since_last": ctx.step - last_sweep,
                 "interval": interval},
            )
            return

        store = services.get("object_store")
        pin = ctx.hold(Right.PIN, None)
        if not store:
            _record(log, {"event": "no_store", "at_step": ctx.step})
            return
        if pin is None:
            # Retention is a human decision, so this Unit was not given the
            # authority to act. That is a legitimate configuration, not a crash:
            # record it and let the operator see that policy is not running.
            _record(log, {"event": "declined", "at_step": ctx.step, "reason": "no PIN Capability"})
            return

        ctx.mem["last_sweep"] = ctx.step
        ctx.send(
            store,
            "object.sweep",
            {"keep_recent": keep_recent, "min_versions": min_versions, "apply": True},
            (pin,),
            reply_to=msg.reply_to,
        )
        return

    if msg.verb == "object.swept":
        result = msg.payload or {}
        entry = {
            "event": "swept",
            "at_step": ctx.step,
            "objects_swept": result.get("objects_swept", 0),
            "versions_reclaimed": result.get("versions_reclaimed", 0),
            "bytes_reclaimed": result.get("bytes_reclaimed", 0),
        }
        _record(log, entry)
        # Only speak up when something actually changed. A sweep that found
        # nothing is not news, and a Console that narrates every no-op trains the
        # human to ignore it.
        if entry["versions_reclaimed"]:
            ctx.send(
                services.get("console", ""),
                "console.notice",
                {
                    "text": (
                        f"retention reclaimed {entry['versions_reclaimed']} version payload(s) "
                        f"across {entry['objects_swept']} Object(s), {_fmt(entry['bytes_reclaimed'])}. "
                        "Metadata kept; pins and preferred versions untouched."
                    )
                },
            )
        return

    if msg.verb == "retention.report":
        # Deliberately not AUDIT-gated, unlike watcher.report and improve.report.
        #
        # Those report on the *system* -- containments, repairs, other Units -- so
        # inspecting the system is exactly what AUDIT is for. This report describes
        # only this Unit's own policy and its own log of sweeps it already performed
        # under authority it demonstrably held. That is self-introspection, the same
        # thing `ctx.mem` already is, so charging AUDIT for it would mean granting
        # this Unit authority it has no use for. It exposes no Object payload.
        limit = int((msg.payload or {}).get("limit", 20))
        ctx.respond(
            msg,
            "retention.report.result",
            {
                "enabled": enabled,
                "keep_recent": keep_recent,
                "min_versions": min_versions,
                "interval": interval,
                "last_sweep": last_sweep,
                "events": log[-limit:],
            },
        )
        return

    if msg.verb == "retention.configure":
        # The operator changing the policy through the same path that reports it.
        p = msg.payload or {}
        changed = {}
        for key in ("enabled", "keep_recent", "min_versions", "interval"):
            if key in p:
                value = p[key]
                if key == "enabled":
                    value = bool(value)
                else:
                    value = int(value)
                ctx.mem[key if key != "enabled" else "enabled"] = value
                changed[key] = value
        # keep_recent/min_versions/interval/enabled are read from params at call
        # time in the handler above, so mirror them there for the next tick.
        ctx.mem["params"].update(changed)
        _record(log, {"event": "reconfigured", "at_step": ctx.step, "changes": changed})
        if msg.reply_to is not None:
            ctx.respond(msg, "retention.configured", {"changes": changed})
        return

    if msg.verb in ("object.swept.result", "object.swept.error", "object.swept.denied"):
        # The store refusing or failing a sweep this Unit asked for. Recorded so a
        # retention policy that has silently stopped working is visible.
        _record(log, {"event": msg.verb, "at_step": ctx.step, "detail": msg.payload})
        return

    if not is_answer(msg.verb):
        ctx.respond(msg, "retention.error", {"reason": f"unknown verb {msg.verb!r}"})


def _record(log: list[dict], entry: dict) -> None:
    log.append(entry)
    # Bounded so an append-heavy session cannot grow this Unit's arena without
    # limit. Dropping old entries is fine: the marker versions each sweep appended
    # remain in Object history, which is the durable record.
    if len(log) > LOG_LIMIT:
        del log[:-LOG_LIMIT]


def _fmt(nbytes: Optional[int]) -> str:
    value = float(nbytes or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"
