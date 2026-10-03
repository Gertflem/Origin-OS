"""Demo Units (step 1.10) — small, ordinary, and deliberately replaceable.

These exist to prove the model, not to be useful. Each one is a few dozen lines,
holds no privilege, does its work by exchanging Messages with the ObjectStore,
and can be frozen, killed and restarted by the Improver without anything else
noticing.

Two conventions every Unit here follows:

Service references arrive in `params["services"]`, which lives inside the Unit's
versioned code Object. A Unit therefore never has to discover anything by name at
runtime, and whoever spawns a replacement does not need to read the old Unit's
private arena to rebuild it — the code Object already says everything required.

Authority comes from `ctx.hold(right, target)`, never from assuming. A Unit acts
only with tokens it already holds — minted for it at birth, endowed by its
spawner, or granted by the human through the Console — and presents them when it
asks a service to act. Presentation proves possession; it never transfers
ownership, so talking to a service cannot quietly enrich it. If a Unit holds
nothing relevant, `hold()` returns None and it says so instead of trying to
proceed. That is invariant 2 at the level of a single line of code.
"""

from __future__ import annotations

from ..capability import Right
from ..message import Message
from ..unit import UnitContext, is_answer, unit_type


def _services(ctx: UnitContext) -> dict[str, str]:
    return ctx.mem["params"].get("services", {})


def _denied(ctx: UnitContext, msg: Message, right: Right, target: str | None) -> None:
    ctx.respond(
        msg,
        f"{msg.verb}.denied",
        {
            "reason": f"no {right.value.upper()} Capability for {target or 'this target'}",
            "needed": right.value,
            "target": target,
        },
    )


@unit_type("photo")
def photo_handler(ctx: UnitContext, msg: Message) -> None:
    """A tiny image editor. Brightens by appending a new version — never in place."""
    store = _services(ctx).get("object_store")
    if msg.verb not in ("brighten", "resize"):
        if not is_answer(msg.verb):
            ctx.respond(msg, "photo.error", {"reason": f"unknown verb {msg.verb!r}"})
        return
    object_id = (msg.payload or {}).get("object_id")
    if store is None or object_id is None:
        ctx.respond(msg, "photo.error", {"reason": "missing object_store service or object_id"})
        return

    read_cap = ctx.hold(Right.READ, object_id)
    if read_cap is None:
        _denied(ctx, msg, Right.READ, object_id)
        return

    amount = int((msg.payload or {}).get("amount", 10))

    # Continuations receive the context of the invocation they run in, not the one
    # that arranged them. Always act through that passed context rather than
    # reaching past it for a captured one: a context is the system's per-invocation
    # view of this Unit, and closing over a stale one would keep working if the view
    # ever carried invocation state again.
    def on_read(c: UnitContext, reply: Message) -> None:
        if reply.verb != "object.read":
            c.respond(msg, "photo.error", {"reason": f"store said: {reply.payload}"})
            return
        version = reply.payload["version"]
        before = version["payload"]

        if msg.verb == "brighten":
            after = {**before, "brightness": int(before.get("brightness", 50)) + amount}
            note = f"brightened by {amount}"
        else:
            scale = float((msg.payload or {}).get("scale", 0.5))
            after = {
                **before,
                "width": max(1, int(before.get("width", 800) * scale)),
                "height": max(1, int(before.get("height", 600) * scale)),
            }
            note = f"resized by {scale}"

        write_cap = c.hold(Right.APPEND, object_id)
        if write_cap is None:
            _denied(c, msg, Right.APPEND, object_id)
            return

        def on_write(c2: UnitContext, reply2: Message) -> None:
            if reply2.verb != "object.appended":
                c2.respond(msg, "photo.error", {"reason": f"store said: {reply2.payload}"})
                return
            # The result is inspectable: object id and version, not just "done".
            c2.respond(
                msg,
                "photo.done",
                {
                    "verb": msg.verb,
                    "object_id": object_id,
                    "previous_seq": version["seq"],
                    "new_seq": reply2.payload["seq"],
                    "before": before,
                    "after": after,
                    "note": note,
                },
            )

        c.request(
            store, "object.append", {"object_id": object_id, "payload": after, "note": note}, (write_cap,), then=on_write
        )

    ctx.request(store, "object.read", {"object_id": object_id}, (read_cap,), then=on_read)


@unit_type("mail")
def mail_handler(ctx: UnitContext, msg: Message) -> None:
    """Delivers by appending to the recipient's mailbox Object.

    Delivery is an append, not a side effect, so a sent message is permanently
    inspectable by anyone holding READ on that mailbox. There is no separate
    "outbox" state and nothing to save.
    """
    store = _services(ctx).get("object_store")
    if msg.verb != "send":
        if not is_answer(msg.verb):
            ctx.respond(msg, "mail.error", {"reason": f"unknown verb {msg.verb!r}"})
        return
    p = msg.payload or {}
    object_id, mailbox = p.get("object_id"), p.get("mailbox")
    if store is None or mailbox is None:
        ctx.respond(msg, "mail.error", {"reason": "missing object_store service or mailbox"})
        return

    def deliver(c: UnitContext, reply: Message) -> None:
        c.respond(
            msg,
            "mail.sent",
            {"to": p.get("to"), "mailbox": mailbox, "object_id": object_id, "seq": (reply.payload or {}).get("seq")},
        )

    append_cap = ctx.hold(Right.APPEND, mailbox)
    if append_cap is None:
        _denied(ctx, msg, Right.APPEND, mailbox)
        return

    body = {"from": ctx.name, "to": p.get("to"), "object_id": object_id, "body": p.get("body", "")}
    if object_id is not None:
        read_cap = ctx.hold(Right.READ, object_id)
        if read_cap is not None:
            def with_body(c: UnitContext, reply: Message) -> None:
                if reply.verb == "object.read":
                    body["attachment"] = reply.payload["version"]["payload"]
                c.request(store, "object.append", {"object_id": mailbox, "payload": body, "note": "delivered"}, (append_cap,), then=deliver)

            ctx.request(store, "object.read", {"object_id": object_id}, (read_cap,), then=with_body)
            return

    ctx.request(
        store, "object.append", {"object_id": mailbox, "payload": body, "note": "delivered"}, (append_cap,), then=deliver
    )


@unit_type("counter")
def counter_handler(ctx: UnitContext, msg: Message) -> None:
    """Keeps a tally in an Object, purely so there is a second kind of Unit."""
    store = _services(ctx).get("object_store")
    if msg.verb != "count":
        if not is_answer(msg.verb):
            ctx.respond(msg, "counter.error", {"reason": f"unknown verb {msg.verb!r}"})
        return
    p = msg.payload or {}
    object_id = p.get("object_id")
    append_cap = ctx.hold(Right.APPEND, object_id)
    if store is None or object_id is None or append_cap is None:
        _denied(ctx, msg, Right.APPEND, object_id)
        return

    def on_read(c: UnitContext, reply: Message) -> None:
        # Check that the read actually succeeded before counting from it.
        #
        # Without this the default of 0 was used for any failure -- a denied, missing
        # or compacted read -- so the Unit computed `1` and appended "counted to 1",
        # reporting a count it never read. Worse, the append's own answer was never
        # checked either, so a refused append was still reported as `counter.done`
        # with `seq: None`. Section 8 promises an inspectable result, and a result
        # that is invented is worse than no result.
        if reply.verb != "object.read":
            c.respond(
                msg,
                "counter.error",
                {"object_id": object_id, "reason": f"could not read the counter: {reply.verb} {reply.payload}"},
            )
            return

        version = (reply.payload or {}).get("version") or {}
        payload = version.get("payload") or {}
        if "count" not in payload:
            c.respond(
                msg,
                "counter.error",
                {"object_id": object_id, "reason": f"the counter has no count in seq {version.get('seq')}"},
            )
            return

        current = int(payload["count"])
        total = current + int(p.get("by", 1))

        def on_append(c2: UnitContext, r2: Message) -> None:
            # Same discipline on the write: a refusal is reported as a refusal.
            if r2.verb != "object.appended":
                c2.respond(
                    msg,
                    "counter.error",
                    {"object_id": object_id, "reason": f"the count was not stored: {r2.verb} {r2.payload}"},
                )
                return
            c2.respond(
                msg,
                "counter.done",
                {"object_id": object_id, "count": total, "seq": (r2.payload or {}).get("seq")},
            )

        c.request(
            store,
            "object.append",
            {"object_id": object_id, "payload": {"count": total}, "note": f"counted to {total}"},
            (append_cap,),
            then=on_append,
        )

    read_cap = ctx.hold(Right.READ, object_id)
    if read_cap is None:
        # Previously this was a silent no-op: no reply, no error, no audit entry. A
        # Unit that cannot do its job must say so, or the human waits for a result
        # that was never coming.
        _denied(ctx, msg, Right.READ, object_id)
        return
    ctx.request(store, "object.read", {"object_id": object_id}, (read_cap,), then=on_read)


@unit_type("flaky")
def flaky_handler(ctx: UnitContext, msg: Message) -> None:
    """A Unit with an injected fault, so self-healing has something to heal.

    `fail_every` in params makes it raise on every k-th invocation. Deterministic
    on purpose: a self-healing test that depends on a random draw is not a test.
    The Improver's fix is to append a code version with the fault removed, which
    is exactly the kind of change a real Improver would propose — retune a
    parameter, keep the old version in history, stay reversible.
    """
    if msg.verb != "work":
        if not is_answer(msg.verb):
            ctx.respond(msg, "flaky.error", {"reason": f"unknown verb {msg.verb!r}"})
        return

    params = ctx.mem["params"]
    calls = ctx.mem["calls"] = ctx.mem.get("calls", 0) + 1
    fail_every = params.get("fail_every")

    if fail_every and calls % int(fail_every) == 0:
        # Raised, not caught. Containment is the Nucleus's job (section 7); a Unit
        # that swallows its own crash hides the failure from the Watcher.
        raise RuntimeError(f"injected fault: {ctx.name} failed on call {calls} (fail_every={fail_every})")

    store = _services(ctx).get("object_store")
    object_id = (msg.payload or {}).get("object_id")
    append_cap = ctx.hold(Right.APPEND, object_id) if object_id else None

    def done(c: UnitContext, reply: Message) -> None:
        c.respond(
            msg,
            "flaky.done",
            {"calls": calls, "object_id": object_id, "seq": (reply.payload or {}).get("seq")},
        )

    if store and object_id and append_cap is not None:
        ctx.request(
            store,
            "object.append",
            {"object_id": object_id, "payload": {"work": calls}, "note": f"call {calls}"},
            (append_cap,),
            then=done,
        )
    else:
        ctx.respond(msg, "flaky.done", {"calls": calls, "object_id": None, "seq": None})
