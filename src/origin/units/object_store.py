"""The ObjectStore Unit — persistent storage as an ordinary service.

Section 9 puts the Object store in the boot sequence, and section 4 says every
service is an ordinary Unit. So this is not a subsystem the Nucleus knows about:
it is a Unit whose arena happens to contain an `ObjectStore`, answering Messages
and refusing anyone who arrives without the right Capability.

The store's authoritative copy lives in this Unit's private arena, which is what
keeps the Nucleus ignorant of storage. Killing the Unit mid-run therefore still
discards in-memory state, so the Watcher treats this Unit as precious and the
Improver is not allowed to propose changes to it.

The durability Phase 2 added lives *below* the Unit, not inside it: with
`boot(storage_path=...)` the snapshot is written through a temp file and an atomic
rename on every mutation, so a restart rebuilds every Object from disk. Replacing
this Unit without a configured storage path would lose data -- that single point
of failure is real, and it is why this Unit is protected.
"""

from __future__ import annotations

from ..capability import CapabilityError
from ..message import Message
from ..objects import CompactedError, ObjectNotFound, ObjectStore
from ..unit import UnitContext, is_answer, unit_type


def _cap_of(msg: Message):
    """The authority a request is being made under. One token per request keeps
    the question "what authorised this?" unambiguous in the audit trail."""
    return msg.caps[0] if msg.caps else None


@unit_type("object_store")
def object_store_handler(ctx: UnitContext, msg: Message) -> None:
    store: ObjectStore = ctx.mem["store"]
    payload = msg.payload or {}
    cap = _cap_of(msg)
    verb = msg.verb

    try:
        if verb == "object.create":
            obj = store.create(
                msg.sender,
                payload.get("kind", "generic"),
                payload.get("payload"),
                cap,
                note=payload.get("note", ""),
                step=ctx.step,
                object_id=payload.get("object_id"),
            )
            ctx.respond(msg, "object.created", {"object_id": obj.object_id, "seq": obj.latest_seq})

        elif verb == "object.append":
            v = store.append(
                msg.sender,
                payload["object_id"],
                payload.get("payload"),
                cap,
                note=payload.get("note", ""),
                step=ctx.step,
            )
            ctx.respond(msg, "object.appended", {"object_id": payload["object_id"], "seq": v.seq})

        elif verb == "object.read":
            v = store.read(msg.sender, payload["object_id"], cap, payload.get("seq"))
            ctx.respond(msg, "object.read", {"object_id": payload["object_id"], "version": v.describe()})

        elif verb == "object.history":
            rows = store.history(msg.sender, payload["object_id"], cap)
            ctx.respond(msg, "object.history", {"object_id": payload["object_id"], "versions": rows})

        elif verb == "object.describe":
            ctx.respond(msg, "object.describe", store.describe(msg.sender, payload["object_id"], cap))

        elif verb == "object.enumerate":
            ctx.respond(msg, "object.enumerate", {"objects": store.enumerate(msg.sender, cap)})

        elif verb == "object.pin":
            store.pin(msg.sender, payload["object_id"], payload["seq"], cap)
            ctx.respond(msg, "object.pinned", {"object_id": payload["object_id"], "seq": payload["seq"]})

        elif verb == "object.prefer":
            store.prefer(msg.sender, payload["object_id"], payload["seq"], cap)
            ctx.respond(msg, "object.preferred", {"object_id": payload["object_id"], "seq": payload["seq"]})

        elif verb == "object.reclaimable":
            # Read-only preview of what object.compact would reclaim. With an
            # object_id it reports that Object; without one, the whole store.
            report = store.reclaimable_report(
                msg.sender,
                cap,
                object_id=payload.get("object_id"),
                keep_recent=int(payload.get("keep_recent", 3)),
            )
            ctx.respond(msg, "object.reclaimable", report)

        elif verb == "object.sweep":
            # Automatic retention. Section 3 states tiering as a policy, and a
            # policy nobody applies is just a comment -- so it runs here. Dry run
            # by default: automatic reclamation of durable memory has to be asked
            # for, not stumbled into.
            report = store.sweep(
                msg.sender,
                cap,
                keep_recent=int(payload.get("keep_recent", 3)),
                min_versions=int(payload.get("min_versions", 32)),
                dry_run=bool(payload.get("apply", False)),
            )
            ctx.respond(msg, "object.swept", report)

        elif verb == "object.compact":
            result = store.compact(
                msg.sender,
                payload["object_id"],
                cap,
                keep_recent=int(payload.get("keep_recent", 3)),
            )
            ctx.respond(msg, "object.compacted", result)

        else:
            if not is_answer(verb):
                ctx.respond(msg, "object.error", {"reason": f"unknown verb {verb!r}", "verb": verb})

    except (CapabilityError, PermissionError) as exc:
        # Authority failures are reported, not raised: the requester is told no,
        # and the Nucleus has already logged the attempt for the Watcher.
        ctx.respond(
            msg,
            "object.denied",
            {"verb": verb, "reason": str(exc), "object_id": payload.get("object_id")},
        )
    except (ObjectNotFound, KeyError) as exc:
        ctx.respond(msg, "object.error", {"verb": verb, "reason": f"no such object or version: {exc}"})
    except CompactedError as exc:
        ctx.respond(msg, "object.compacted_away", {"verb": verb, "reason": str(exc)})
