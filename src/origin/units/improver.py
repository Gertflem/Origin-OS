"""The Improver Unit — self-healing by versioned proposal (step 1.10).

Constitution section 7: the system detects failures, contains them, then
Improvers diagnose, propose a better version, and restart cleanly; every
improvement is versioned and reversible; Improvers cannot modify the Nucleus or
self-grant authority.

How each of those three guarantees is actually kept here, because none of them is
a permission check:

**Cannot modify the Nucleus.** The Nucleus has no code Object (see nucleus.py).
Everything this Unit does starts by reading one. There is no id to read and no
version to append, so the attempt is not forbidden so much as inexpressible.

**Cannot self-grant.** Authority to mint is a RESERVED_RIGHT in capability.py:
only the human principal can create a GRANT, REVOKE or GUARDIAN token. This Unit
holds SPAWN, READ and APPEND and cannot manufacture anything else, so the most
damage a confused Improver can do is spawn a Unit that immediately fails for
want of authority — visibly, in the audit trail.

**Reversible.** An improvement is a new version of the Unit's code Object plus a
`preferred` pointer. The version that failed is still addressable by seq, so
`improve.rollback` is not a repair operation, it is the same operation pointed at
an older seq. Nothing is ever overwritten, which is invariant 3 doing the work
that a "revert" feature would otherwise have to do.

What this Unit deliberately does not hold: KILL and FREEZE (the agent that
proposes a better Unit is not the agent that destroys the old one — the Watcher
retires it), GRANT/REVOKE (above), PIN (retention policy is a human decision),
AUDIT and BIND (it has no reason to look around or rename things).

Honest known limitation: Capability targets are ids, not kinds, so this Unit's
APPEND token cannot be scoped to "code Objects only". An Improver that can rewrite
code can also rewrite data. Section 7's real protection here is that every change
is appended rather than applied, so a bad improvement is a bad *version* and not a
bad *state*. Object kinds and namespaces are Phase 3 work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..capability import CapabilityError, Right
from ..ids import NUCLEUS
from ..message import Message
from ..unit import UnitContext, is_answer, unit_type

#: Kinds this Unit will not rewrite, whatever it is asked.
#:
#: `nucleus` has no code Object and is listed for documentation. `object_store`
#: because the store instance currently lives in its arena, so replacing it would
#: destroy every Object. `improver` because an Improver editing itself is the one
#: change section 7 puts behind a human decision. `watcher` because the Watcher
#: holds KILL: letting this Unit spawn a "replacement" Watcher would be a route to
#: authority it is specifically not supposed to have, since the Nucleus re-homes a
#: contained Unit's Capabilities onto its same-kind replacement.
#:
#: The Watcher enforces the same list before escalating. Keeping one definition
#: here and importing it there means the two cannot drift — and if they ever do,
#: this Unit is the one that matters, because it is the one that acts.
PROTECTED_KINDS = frozenset({"nucleus", "object_store", "improver", "watcher"})


@dataclass
class Diagnosis:
    """The outcome of looking at one failure.

    `action` is "improve" or "decline". Declining is a first-class result, not a
    failure: most of what can go wrong in a system like this is not fixable by
    editing parameters, and an Improver that guesses in those cases is worse than
    one that says "I don't know, a human should look".
    """

    action: str
    reason: str
    code: Optional[dict] = None
    note: str = ""


def diagnose(event: str, detail: str, code: dict) -> Diagnosis:
    """Decide what, if anything, to change about a failing Unit's code.

    Rule-based and deliberately small. Every rule is a sentence a human could
    read aloud and agree or disagree with, which is the property section 8 cares
    about; a learned diagnosis would have to be trusted instead of checked.
    """
    params: dict[str, Any] = dict(code.get("params") or {})

    if event == "crashed" and "injected fault" in detail:
        # The defect is a parameter, not the entry point: `fail_every` is what
        # makes the demo Unit raise. Clearing it is the smallest change that
        # explains away the observed failure, and the faulty version stays in
        # history at its own seq.
        params["fail_every"] = None
        return Diagnosis(
            "improve",
            "crash detail names an injected fault; cleared fail_every",
            {**code, "params": params},
            note="improvement: fail_every cleared after repeated crashes",
        )

    if event == "preempted":
        batch = params.get("batch")
        if isinstance(batch, int) and batch > 1:
            params["batch"] = max(1, batch // 2)
            return Diagnosis(
                "improve",
                f"ran out of syscall budget; halved batch {batch} -> {params['batch']}",
                {**code, "params": params},
                note=f"improvement: batch {batch} -> {params['batch']}",
            )
        return Diagnosis(
            "decline",
            "ran out of syscall budget but there is no batch parameter to reduce; "
            "the entry point itself is doing too much, which is a human decision",
        )

    if event == "capability_abuse":
        return Diagnosis(
            "decline",
            "the Unit asked for authority it does not hold. That is a grant decision, "
            "not a code defect, and granting is reserved to the human",
        )

    if event == "invariant_violation":
        return Diagnosis(
            "decline",
            "a constitutional invariant was breached. Patching over that automatically "
            "would hide the breach, so it goes to a human",
        )

    return Diagnosis("decline", f"no diagnosis rule matches event {event!r}")


@unit_type("improver")
def improver_handler(ctx: UnitContext, msg: Message) -> None:
    params = ctx.mem["params"]
    services: dict[str, str] = params.get("services", {})
    store = services.get("object_store")
    #: Append-only record of every decision this Unit has made, including the
    #: declines. Proposals that were rejected matter as much as ones that ran.
    log: list[dict] = ctx.mem.setdefault("log", [])
    #: "kind:event" -> times attempted. Stops a repair that does not repair from
    #: looping forever, which is the failure mode a self-healing system has to be
    #: most careful about.
    attempts: dict[str, int] = ctx.mem.setdefault("attempts", {})
    max_attempts = int(params.get("max_attempts", 2))

    if msg.verb == "improve.request":
        _handle_request(ctx, msg, store, log, attempts, max_attempts, services)
    elif msg.verb == "improve.rollback":
        _handle_rollback(ctx, msg, store, log, services)
    elif msg.verb == "improve.report":
        _handle_report(ctx, msg, log, attempts)
    elif msg.verb == "nucleus.sealed":
        log.append({"event": "sealed", "at_step": ctx.step})
    elif not is_answer(msg.verb):
        ctx.respond(msg, "improve.error", {"reason": f"unknown verb {msg.verb!r}"})


# ---------------------------------------------------------------------------
# improve.request — the self-healing path
# ---------------------------------------------------------------------------
def _handle_request(
    ctx: UnitContext,
    msg: Message,
    store: Optional[str],
    log: list[dict],
    attempts: dict[str, int],
    max_attempts: int,
    services: dict[str, str],
) -> None:
    p = msg.payload or {}
    kind = p.get("kind")
    unit_id = p.get("unit_id")
    code_object_id = p.get("code_object_id")
    event = p.get("event", "")
    detail = p.get("detail", "")

    def decline(reason: str, *, escalate: bool = True) -> None:
        log.append(
            {
                "event": "declined",
                "unit_id": unit_id,
                "kind": kind,
                "trigger": event,
                "reason": reason,
                "at_step": ctx.step,
            }
        )
        ctx.respond(msg, "improve.declined", {"unit_id": unit_id, "kind": kind, "trigger": event, "reason": reason})
        if escalate and services.get("console"):
            ctx.send(
                services["console"],
                "console.notice",
                {"text": f"improver declined to repair {p.get('name')} ({kind}): {reason}"},
            )

    # Defence in depth: the Watcher already refuses protected kinds, but this
    # Unit is the one that acts, so it checks for itself rather than trusting the
    # caller. A Watcher that got it wrong must not become a way in.
    if kind in PROTECTED_KINDS:
        decline(f"{kind} is protected and cannot be rewritten by an Improver", escalate=False)
        return
    if not code_object_id:
        decline("the contained Unit has no versioned code Object, so there is nothing to improve")
        return
    if store is None:
        decline("no object_store service in my parameters")
        return

    key = f"{kind}:{event}"
    if attempts.get(key, 0) >= max_attempts:
        decline(
            f"already attempted {attempts[key]} repairs for {key} without the failures stopping; "
            "further automatic attempts would thrash, so this needs a human"
        )
        return

    read_cap = ctx.hold(Right.READ, code_object_id)
    if read_cap is None:
        decline(f"I hold no READ Capability for code Object {code_object_id}")
        return

    def on_code(c: UnitContext, reply: Message) -> None:
        if reply.verb != "object.read":
            decline(f"could not read the code Object ({reply.verb}: {reply.payload})")
            return
        code = reply.payload["version"]["payload"]
        found = diagnose(event, detail, code)
        if found.action != "improve":
            decline(found.reason)
            return

        attempts[key] = attempts.get(key, 0) + 1
        append_cap = c.hold(Right.APPEND, code_object_id)
        if append_cap is None:
            decline(f"I hold no APPEND Capability for code Object {code_object_id}")
            return

        def on_appended(c2: UnitContext, appended: Message) -> None:
            if appended.verb != "object.appended":
                decline(f"could not append the improved version ({appended.verb}: {appended.payload})")
                return
            new_seq = appended.payload["seq"]

            def on_preferred(c3: UnitContext, preferred: Message) -> None:
                if preferred.verb != "object.preferred":
                    decline(f"could not mark seq {new_seq} preferred ({preferred.verb})")
                    return
                _restart(c3, msg, p, found, code_object_id, new_seq, log, services, decline)

            # Preferring the new version is what makes the improvement the one a
            # later restart picks up by default — and what makes rollback a
            # one-message operation, since preferring an older seq is the same call.
            c2.request(
                store,
                "object.prefer",
                {"object_id": code_object_id, "seq": new_seq},
                (append_cap,),
                then=on_preferred,
            )

        c.request(
            store,
            "object.append",
            {"object_id": code_object_id, "payload": found.code, "note": found.note},
            (append_cap,),
            then=on_appended,
        )

    ctx.request(
        store, "object.read", {"object_id": code_object_id, "seq": p.get("code_seq")}, (read_cap,), then=on_code
    )


def _restart(
    ctx: UnitContext,
    msg: Message,
    p: dict,
    found: Diagnosis,
    code_object_id: str,
    seq: int,
    log: list[dict],
    services: dict[str, str],
    decline,
) -> None:
    """Ask the core for a clean restart on the new version, then hand over.

    A restart is a *new Unit*, not a resurrection. The contained one stays frozen
    with its state intact until the Watcher retires it, so there is a moment where
    both exist and the human can inspect the failure before it is released.
    """
    spawn_cap = ctx.hold(Right.SPAWN, None)
    if spawn_cap is None:
        decline("I hold no SPAWN Capability, so I can propose a better version but cannot restart it")
        return

    code = found.code or {}

    def on_spawned(c: UnitContext, reply: Message) -> None:
        if reply.verb != "spawn.result":
            log.append(
                {
                    "event": "restart_denied",
                    "unit_id": p.get("unit_id"),
                    "kind": p.get("kind"),
                    "reason": reply.payload,
                    "at_step": c.step,
                }
            )
            c.respond(msg, "improve.failed", {"unit_id": p.get("unit_id"), "reason": reply.payload})
            if services.get("console"):
                c.send(
                    services["console"],
                    "console.notice",
                    {"text": f"restart of {p.get('name')} was refused by the core: {reply.payload}"},
                )
            return

        new_unit_id = reply.payload["unit_id"]
        log.append(
            {
                "event": "improved",
                "unit_id": p.get("unit_id"),
                "replacement": new_unit_id,
                "kind": p.get("kind"),
                "trigger": p.get("event"),
                "code_object_id": code_object_id,
                "code_seq": seq,
                "reason": found.reason,
                "at_step": c.step,
            }
        )

        # Retiring the old Unit is the Watcher's job, not mine: it holds KILL and
        # I do not. Separation of duties means a bad Improver can spawn junk but
        # cannot delete anything.
        if services.get("watcher"):
            c.send(
                services["watcher"],
                "watcher.retire",
                {"unit_id": p["unit_id"], "replacement": new_unit_id},
            )
        # The Console owns naming, so it re-points the old name at the new Unit.
        if services.get("console"):
            c.send(
                services["console"],
                "console.replaced",
                {
                    "old_unit_id": p["unit_id"],
                    "new_unit_id": new_unit_id,
                    "name": p.get("name"),
                    "kind": p.get("kind"),
                    "code_seq": seq,
                    "reason": found.reason,
                },
            )
        c.respond(msg, "improve.done", {"replacement": new_unit_id, "code_seq": seq, "reason": found.reason})

    ctx.request(
        NUCLEUS,
        "spawn",
        {
            "kind": p.get("kind"),
            "name": p.get("name"),
            "entry": code.get("entry", p.get("kind")),
            "params": code.get("params"),
            "code_object_id": code_object_id,
            "code_seq": seq,
            "replaces": p.get("unit_id"),
        },
        (spawn_cap,),
        then=on_spawned,
    )


# ---------------------------------------------------------------------------
# improve.rollback — reversibility, demonstrated rather than asserted
# ---------------------------------------------------------------------------
def _handle_rollback(
    ctx: UnitContext, msg: Message, store: Optional[str], log: list[dict], services: dict[str, str]
) -> None:
    p = msg.payload or {}
    code_object_id = p.get("code_object_id")
    seq = p.get("seq")
    kind = p.get("kind")

    def fail(reason: str) -> None:
        log.append({"event": "rollback_failed", "reason": reason, "at_step": ctx.step})
        ctx.respond(msg, "improve.failed", {"reason": reason, "unit_id": p.get("unit_id")})

    if kind in PROTECTED_KINDS:
        fail(f"{kind} is protected and cannot be rewritten by an Improver")
        return
    if store is None or code_object_id is None or seq is None:
        fail("rollback needs object_store, code_object_id and seq")
        return

    read_cap = ctx.hold(Right.READ, code_object_id)
    append_cap = ctx.hold(Right.APPEND, code_object_id)
    if read_cap is None or append_cap is None:
        fail(f"I hold no READ/APPEND Capability for code Object {code_object_id}")
        return

    def on_code(c: UnitContext, reply: Message) -> None:
        if reply.verb != "object.read":
            fail(f"no such version: {reply.payload}")
            return
        code = reply.payload["version"]["payload"]

        def on_preferred(c2: UnitContext, preferred: Message) -> None:
            if preferred.verb != "object.preferred":
                fail(f"could not prefer seq {seq}: {preferred.payload}")
                return
            spawn_cap = c2.hold(Right.SPAWN, None)
            if spawn_cap is None:
                fail("I hold no SPAWN Capability")
                return

            def on_spawned(c3: UnitContext, spawned: Message) -> None:
                if spawned.verb != "spawn.result":
                    fail(f"the core refused the restart: {spawned.payload}")
                    return
                new_unit_id = spawned.payload["unit_id"]
                log.append(
                    {
                        "event": "rolled_back",
                        "unit_id": p.get("unit_id"),
                        "replacement": new_unit_id,
                        "kind": kind,
                        "code_object_id": code_object_id,
                        "code_seq": seq,
                        "at_step": c3.step,
                    }
                )
                if services.get("watcher"):
                    c3.send(services["watcher"], "watcher.retire", {"unit_id": p["unit_id"], "replacement": new_unit_id})
                if services.get("console"):
                    c3.send(
                        services["console"],
                        "console.replaced",
                        {
                            "old_unit_id": p["unit_id"],
                            "new_unit_id": new_unit_id,
                            "name": p.get("name"),
                            "kind": kind,
                            "code_seq": seq,
                            "reason": f"rolled back to version {seq}",
                        },
                    )
                c3.respond(msg, "improve.done", {"replacement": new_unit_id, "code_seq": seq, "rolled_back": True})

            c2.request(
                NUCLEUS,
                "spawn",
                {
                    "kind": kind,
                    "name": p.get("name"),
                    "entry": code.get("entry", kind),
                    "params": code.get("params"),
                    "code_object_id": code_object_id,
                    "code_seq": seq,
                    "replaces": p.get("unit_id"),
                },
                (spawn_cap,),
                then=on_spawned,
            )

        c.request(store, "object.prefer", {"object_id": code_object_id, "seq": seq}, (append_cap,), then=on_preferred)

    ctx.request(store, "object.read", {"object_id": code_object_id, "seq": seq}, (read_cap,), then=on_code)


# ---------------------------------------------------------------------------
# improve.report — transparency over this Unit's own judgement
# ---------------------------------------------------------------------------
def _handle_report(ctx: UnitContext, msg: Message, log: list[dict], attempts: dict[str, int]) -> None:
    cap = msg.caps[0] if msg.caps else None
    try:
        if cap is None:
            raise CapabilityError("improve.report requires an AUDIT Capability")
        # Validating someone else's token does not require holding one. The token
        # arrived attached to the Message, which means the Nucleus already checked
        # it was live and transferred it on delivery.
        ctx.mem["validator"](cap, Right.AUDIT, None, msg.sender)
        limit = int((msg.payload or {}).get("limit", 20))
        ctx.respond(
            msg,
            "improve.report.result",
            {"decisions": log[-limit:], "attempts": dict(attempts), "protected_kinds": sorted(PROTECTED_KINDS)},
        )
    except CapabilityError as exc:
        ctx.respond(msg, "improve.denied", {"reason": str(exc)})
