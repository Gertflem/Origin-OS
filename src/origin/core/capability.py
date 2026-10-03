"""Capabilities — unforgeable, transferable tokens of authority (step 1.2).

Constitution section 5: there is no ambient authority. Authority exists only as
explicit Capabilities, a Unit can only act on what it has been given, and
discovery without a relevant Capability is impossible.

The design decision that makes this real:

    A `Capability` token carries NO authority of its own. It is an inert handle
    containing only a secret id. Every right, target, holder and revocation
    state lives in the Nucleus's registry, and every use is checked against it.

So a token cannot be widened by editing it, cannot be forged without guessing a
128-bit secret, and dies instantly when revoked — even copies already delivered
into other Units' hands. The token is a pointer to authority, never the
authority itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .ids import mask, new_id


class Right(str, Enum):
    """The complete authority vocabulary.

    Deliberately small. A right is a verb the system will check, not a
    description of a feature; anything richer belongs in an ordinary Unit.
    """

    # Object rights — target is an object id.
    READ = "read"
    APPEND = "append"
    PIN = "pin"
    HISTORY = "history"

    # Unit lifecycle rights — target is a unit id, or None for "any".
    SPAWN = "spawn"
    KILL = "kill"
    FREEZE = "freeze"

    # Messaging — target is a unit id, or None for "any Unit may be addressed".
    SEND = "send"

    # Naming — target is a name, or None for namespace-wide.
    BIND = "bind"
    RESOLVE = "resolve"

    # Authority over authority. GRANT/REVOKE let a principal mint and withdraw
    # Capabilities on behalf of whoever granted them.
    GRANT = "grant"
    REVOKE = "revoke"

    # Read-only transparency over the whole system state (section 8). Not ambient
    # authority: it is an explicit, revocable token, and it can never mutate.
    AUDIT = "audit"

    # The human escape hatch (section 7). Implies everything, held by no Unit.
    GUARDIAN = "guardian"


#: Rights that imply all others. Only GUARDIAN qualifies, and only the human
#: principal ever holds it.
ABSOLUTE_RIGHTS = frozenset({Right.GUARDIAN})

#: Rights that may never be minted by anyone but the human principal.
#:
#: GUARDIAN because it is the human's escape hatch and section 7 says it is held
#: by no Unit. GRANT and REVOKE because they are authority over authority: if a
#: delegate could mint them, "Improvers cannot self-grant authority" would be a
#: policy rather than a fact, and one confused Unit could hand out permanent
#: power. Keeping delegation rights human-only makes attenuation one-directional
#: by construction — a delegate can narrow what it passes on, and can never
#: recreate the ability to delegate.
RESERVED_RIGHTS = frozenset({Right.GUARDIAN, Right.GRANT, Right.REVOKE})


class CapabilityError(PermissionError):
    """A Capability was missing, forged, revoked, expired, or out of scope."""

    def __init__(self, reason: str, *, right: Optional[str] = None, cap_id: Optional[str] = None) -> None:
        self.reason = reason
        self.right = right
        self.cap_id = cap_id
        super().__init__(reason)


@dataclass(frozen=True)
class Capability:
    """An inert, unforgeable handle. Safe to copy, store, and put in Messages.

    Equality is by secret id, so two handles to the same record compare equal
    and hash together — that is what lets a Unit keep its holdings in a set.
    """

    cap_id: str

    @staticmethod
    def new() -> "Capability":
        return Capability(new_id("cap"))

    def __repr__(self) -> str:
        # Never leak the full secret into a log line or a traceback.
        return f"Capability({mask(self.cap_id)})"

    def __str__(self) -> str:
        return mask(self.cap_id)


@dataclass
class CapabilityRecord:
    """The authoritative state of one Capability, held only by the Nucleus.

    Units never see this. They see `Capability` handles and, if they hold AUDIT,
    a redacted `describe()` rendering.
    """

    cap_id: str
    rights: frozenset[Right]
    grantor: str
    holders: set[str] = field(default_factory=set)
    #: None means namespace-wide. Otherwise the right applies only to this target.
    target: Optional[str] = None
    #: None means "no kind restriction". Otherwise the token only covers Objects
    #: of this kind, whichever Object they are.
    #:
    #: This is what lets an authority be scoped to "code Objects" instead of "every
    #: Object that happens to exist". Without it a token either names one Object id
    #: or covers the whole namespace, so a Unit trusted to rewrite code is
    #: mechanically trusted to rewrite data too. Both scopes narrow; a token may
    #: carry either, and never anything broader than what it was granted.
    target_kind: Optional[str] = None
    label: str = ""
    created_step: int = 0
    #: None = long-lived. An int = the step at which this token stops working.
    #: Section 5: temporary is preferred when sufficient.
    expires_at_step: Optional[int] = None
    revoked: bool = False
    revoked_by: Optional[str] = None
    revoked_reason: str = ""

    def grants(self, right: Right, target: Optional[str], target_kind: Optional[str] = None) -> bool:
        """Does this record authorise `right` against this target and kind?

        `target_kind` is the kind of the Object being acted on, resolved by the
        caller. It is only consulted when the record itself declares a kind scope,
        so the common case costs nothing and cannot be broken by a caller passing
        the wrong kind for an unrestricted token.

        A token that declares a kind scope refuses a target whose kind does not
        match, including when the kind cannot be resolved at all. Failing closed
        matters here: an unknown kind must never widen a scoped token into a
        namespace-wide one.
        """
        if self.revoked:
            return False
        if not (self.rights & ABSOLUTE_RIGHTS) and right not in self.rights:
            return False
        if self.target_kind is not None:
            if target_kind is None or target_kind != self.target_kind:
                return False
        # A namespace-wide token (target is None) covers any target. A scoped
        # token covers exactly its scope and nothing else.
        return self.target is None or self.target == target

    def live_at(self, step: int) -> bool:
        if self.revoked:
            return False
        return self.expires_at_step is None or step < self.expires_at_step

    def is_temporary(self) -> bool:
        return self.expires_at_step is not None

    def describe(self) -> dict:
        """Redacted view for inspection. Includes no secret material."""
        return {
            "id": mask(self.cap_id),
            "rights": sorted(r.value for r in self.rights),
            "target": self.target,
            "target_kind": self.target_kind,
            "grantor": self.grantor,
            "holders": sorted(self.holders),
            "label": self.label,
            "created_step": self.created_step,
            "expires_at_step": self.expires_at_step,
            "temporary": self.is_temporary(),
            "revoked": self.revoked,
            "revoked_by": self.revoked_by,
            "revoked_reason": self.revoked_reason,
        }


@dataclass
class Proposal:
    """An intent-time capability proposal (section 5).

    Low-friction granting works by showing the human exactly what authority an
    intent needs *before* anything runs, then minting it only on confirmation.
    A Proposal is that request: it confers nothing until the human approves it.
    """

    rights: frozenset[Right]
    target: Optional[str]
    reason: str
    #: Restrict to Objects of this kind, whichever Object they are. Narrows the
    #: grant; never widens it.
    target_kind: Optional[str] = None
    #: Preferred lifetime. Temporary is the default because section 5 says so.
    expires_in_steps: Optional[int] = 32
    label: str = ""

    def describe(self) -> dict:
        return {
            "rights": sorted(r.value for r in self.rights),
            "target": self.target,
            "target_kind": self.target_kind,
            "reason": self.reason,
            "expires_in_steps": self.expires_in_steps,
            "label": self.label,
        }
