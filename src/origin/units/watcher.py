"""The Watcher Unit — containment and transparency (steps 1.7 and 1.10).

Constitution section 7: the system detects invariant violations, crashes and
capability abuses; faulty Units are immediately contained; recovery uses version
history and clean restarts.

The Nucleus does the *detecting* and the immediate freeze, because a crash has to
be contained by something that cannot itself be crashed. What happens next —
diagnose, escalate, retire, report — is policy, and policy belongs in an ordinary
Unit that can be wrong and fixed. That division is why this file exists.

The Watcher also holds the KILL authority and the Improver does not. Separation
of duties: the agent that proposes a better Unit is not the agent that destroys
the old one. An Improver gone wrong can spawn junk; it cannot delete anything.

It holds AUDIT as well, because watching the system means being able to look at
it. That token is revocable by the Guardian like any other.
"""

from __future__ import annotations

from ..capability import CapabilityError, Right
from ..ids import NUCLEUS
from ..message import Message
from ..unit import UnitContext, is_answer, unit_type
from .improver import PROTECTED_KINDS


@unit_type("watcher")
def watcher_handler(ctx: UnitContext, msg: Message) -> None:
    params = ctx.mem["params"]
    services = params.get("services", {})
    events: list[dict] = ctx.mem.setdefault("events", [])
    escalations: dict[str, int] = ctx.mem.setdefault("escalations", {})

    if msg.verb == "unit.contained":
        p = msg.payload or {}
        unit_id = p.get("unit_id")
        events.append({**p, "at_step": ctx.step, "source": msg.sender})

        # The Nucleus has already frozen the Unit by the time this arrives, so
        # containment is a fact, not an action. The Watcher's job is to decide
        # what happens next.
        escalations[unit_id] = escalations.get(unit_id, 0) + 1
        threshold = int(params.get("escalate_after", 1))
        kind = p.get("kind", "")

        if escalations[unit_id] < threshold:
            ctx.send(
                services.get("console", ""),
                "console.notice",
                {"text": f"contained {p.get('name')} ({p.get('event')}); waiting for {threshold} before escalating"},
            )
            return

        if kind in PROTECTED_KINDS:
            ctx.send(
                services.get("console", ""),
                "console.notice",
                {
                    "text": f"{p.get('name')} is protected and cannot be auto-improved; "
                    f"it stays frozen for a human to review ({p.get('event')})"
                },
            )
            return

        improver = services.get("improver")
        if not improver or not p.get("code_object_id"):
            # No versioned code means nothing to improve. This is also the branch
            # that would fire for the Nucleus, which has no code Object at all —
            # section 7's "Improvers cannot modify the Nucleus" holds because
            # there is no mechanism to reach it, not because of a deny rule.
            ctx.send(
                services.get("console", ""),
                "console.notice",
                {"text": f"{p.get('name')} has no versioned code to improve; left frozen"},
            )
            return

        ctx.send(
            improver,
            "improve.request",
            {
                "unit_id": unit_id,
                "name": p.get("name"),
                "kind": kind,
                "event": p.get("event"),
                "detail": p.get("detail"),
                "crashes": p.get("crashes"),
                "code_object_id": p.get("code_object_id"),
                "code_seq": p.get("code_seq"),
                "occurrences": escalations[unit_id],
            },
        )
        return

    if msg.verb == "watcher.retire":
        p = msg.payload or {}
        cap = ctx.hold(Right.KILL, p.get("unit_id"))
        if cap is None:
            ctx.respond(msg, "watcher.denied", {"reason": "no KILL Capability for that Unit"})
            return
        ctx.send(
            NUCLEUS,
            "kill",
            {
                "unit_id": p["unit_id"],
                "reason": f"retired by {ctx.name}; replaced by {p.get('replacement')}",
            },
            (cap,),
            reply_to=msg.reply_to,
        )
        events.append(
            {
                "event": "retired",
                "unit_id": p["unit_id"],
                "replacement": p.get("replacement"),
                "at_step": ctx.step,
            }
        )
        return

    if msg.verb == "watcher.report":
        cap = msg.caps[0] if msg.caps else None
        try:
            if cap is None:
                raise CapabilityError("watcher.report requires an AUDIT Capability")
            ctx.mem["validator"](cap, Right.AUDIT, None, msg.sender)
            ctx.respond(
                msg,
                "watcher.report.result",
                {"events": events[-int((msg.payload or {}).get("limit", 20)) :], "escalations": dict(escalations)},
            )
        except CapabilityError as exc:
            ctx.respond(msg, "watcher.denied", {"reason": str(exc)})
        return

    if msg.verb == "nucleus.sealed":
        events.append({"event": "sealed", "at_step": ctx.step})
        return

    if msg.verb == "kill.result":
        # Confirmation that a retirement this Watcher requested actually happened.
        # Recorded for transparency and deliberately not answered: `kill` was sent
        # fire-and-forget, so this reply has no continuation, and answering an
        # answer is how a Unit and the core trade messages forever.
        events.append({"event": "kill.confirmed", "detail": msg.payload, "at_step": ctx.step})
        return

    if msg.verb in ("improve.done", "improve.declined", "improve.failed"):
        # The Improver answering the `improve.request` this Watcher sent. That
        # request was fire-and-forget, so there is no continuation to fire and
        # nothing to do but record the outcome: the Improver has already told the
        # Console (console.replaced) and asked for the retirement (watcher.retire).
        # These verbs are not `.result`/`.error`/`.denied`, so without this branch
        # the fallthrough would mistake a successful repair for an unknown verb and
        # reply watcher.error — a phantom failure in the audit trail of a clean heal.
        events.append({"event": msg.verb, "detail": msg.payload, "at_step": ctx.step})
        return

    if not is_answer(msg.verb):
        ctx.respond(msg, "watcher.error", {"reason": f"unknown verb {msg.verb!r}"})
