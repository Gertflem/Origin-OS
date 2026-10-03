"""The core verb table — what the privileged Nucleus will answer.

Constitution section 2 gives the Nucleus an exhaustive list of powers, and
section 1 permits no foundational concept beyond the five primitives. A dispatch
table living inside `Nucleus` is still a dispatch table inside `Nucleus`: it is
the shape of an application layer growing in the one place the Constitution says
must stay tiny. So the table lives here, and the Nucleus keeps only the call.

`CORE_VERBS`, `CORE_SIGNALS` and `CORE_REPLIES` are re-exported from
`nucleus.py`, which remains the public name for them, because the closed
vocabulary is a property of the core as a whole rather than of this module.

Two rules keep this module from becoming the thing it was extracted to avoid:

**Closed vocabulary.** Every verb here maps to one enumerated power in section 2.
A verb that cannot be expressed as one of those powers does not belong in this
table; it belongs in `units/`, behind a Capability. The set is derived from the
dispatch table, not maintained beside it, so a verb cannot be reachable without
being declared.

**No authority of its own.** Nothing here decides whether a request is permitted.
Each handler takes the token off the Message and hands it to the Nucleus method
that does validate and record, because the audit trail is the Nucleus's to keep.
This module translates; it does not authorise.

`inspect` is the verb that most looks like an unlisted power. It is not one. The
audit trail, the Unit table and the Capability registry are core state; nothing
outside the core can report on them, and section 8 requires that the human can
always inspect Units, Capabilities and Messages. So the core serves read-only
descriptions of its own bookkeeping to any principal presenting an AUDIT token.
It cannot mutate, it cannot be called without authority, and refusing it would
break a constitutional promise.
"""

from __future__ import annotations

from typing import Callable

from .capability import Capability, CapabilityError, Right


def _spawn(nucleus, msg, p: dict) -> dict:
    unit = nucleus.spawn(
        msg.sender,
        p.get("kind", "unit"),
        p["name"],
        p["entry"],
        params=p.get("params"),
        code_object_id=p.get("code_object_id"),
        code_seq=p.get("code_seq"),
        authority=msg.caps[0] if msg.caps else None,
        endow=p.get("endow", ()),
        replaces=p.get("replaces"),
    )
    return {"unit_id": unit.unit_id, "name": unit.name}


def _kill(nucleus, msg, p: dict) -> dict:
    nucleus.kill(p["unit_id"], by=msg.sender, reason=p.get("reason", ""), authority=msg.caps[0] if msg.caps else None)
    return {"killed": p["unit_id"]}


def _freeze(nucleus, msg, p: dict) -> dict:
    nucleus.freeze(p["unit_id"], by=msg.sender, reason=p.get("reason", ""), authority=msg.caps[0] if msg.caps else None)
    return {"frozen": p["unit_id"]}


def _mint(nucleus, msg, p: dict) -> dict:
    new = nucleus.mint(
        [Right(r) for r in p["rights"]],
        p.get("target"),
        msg.sender,
        p["holder"],
        expires_in=p.get("expires_in"),
        label=p.get("label", ""),
        authority=msg.caps[0] if msg.caps else None,
    )
    return {"cap": str(new), "cap_id": new.cap_id}


def _revoke(nucleus, msg, p: dict) -> dict:
    nucleus.revoke(
        Capability(p["cap_id"]),
        by=msg.sender,
        reason=p.get("reason", ""),
        authority=msg.caps[0] if msg.caps else None,
    )
    return {"revoked": p["cap_id"]}


def _inspect(nucleus, msg, p: dict) -> dict:
    return nucleus.describe(msg.sender, msg.caps[0] if msg.caps else None, p)


#: verb -> handler. The single declaration of what the core answers; `CORE_VERBS`
#: is derived from these keys rather than listed separately.
CORE_VERB_HANDLERS: dict[str, Callable[..., dict]] = {
    "spawn": _spawn,
    "kill": _kill,
    "freeze": _freeze,
    "mint": _mint,
    "revoke": _revoke,
    "inspect": _inspect,
}

#: The closed vocabulary of verbs the Nucleus will accept when addressed
#: directly. Derived from the dispatch table so the two cannot drift apart.
CORE_VERBS = frozenset(CORE_VERB_HANDLERS)

#: Each verb's power in section 2. Used by `audit_powers` to report what the core
#: can still do, so the operator can see privilege shrink as the system seals.
CORE_VERB_POWERS: dict[str, str] = {
    "spawn": "birth and schedule Units",
    "kill": "freeze or kill Units",
    "freeze": "freeze or kill Units",
    "mint": "create, validate, transfer and revoke Capabilities",
    "revoke": "create, validate, transfer and revoke Capabilities",
    "inspect": "validate Capabilities (read-only transparency, AUDIT-gated)",
}


def dispatch(nucleus, msg) -> dict:
    """Answer a core request, or refuse it.

    The Nucleus calls this after it has decided the verb is core vocabulary. Any
    verb outside the table is a bug rather than a refusal: `CORE_VERBS` guards the
    front door, so reaching here with an unknown verb means the two disagreed.
    """
    handler = CORE_VERB_HANDLERS.get(msg.verb)
    if handler is None:
        raise CapabilityError(f"unhandled core verb {msg.verb!r}")
    return handler(nucleus, msg, msg.payload or {})
