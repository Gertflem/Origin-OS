"""Messages — the only means of communication between Units (step 1.3).

Constitution section 1 lists Messages as a primitive, and invariant 4 is blunt:
communication occurs only through Messages carrying explicit Capabilities. There
is no shared memory, no global registry a Unit can reach into, no function call
across an arena boundary. If a Unit wants something from another Unit, it must
send a Message and attach the authority for what it is asking.

Messages are immutable. Once routed, a Message is a fact about history, which is
what lets the audit trail and the Watcher reason about the system after the
fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .capability import Capability
from .ids import new_id


@dataclass(frozen=True)
class Message:
    """A single immutable communication between principals.

    `sender` and `recipient` are unit ids, or `HUMAN` for the principal outside
    the system. `verb` names what is being asked; `payload` is opaque data.

    Carrying capabilities is what distinguishes a Message from a function call:
    the recipient can only do what the sender authorised, and the sender can
    only authorise what it itself holds.
    """

    sender: str
    recipient: str
    verb: str
    payload: Any = None
    caps: tuple[Capability, ...] = ()
    msg_id: str = field(default_factory=lambda: new_id("msg"))
    #: The msg_id this is answering. Used for correlation and for the audit trail.
    reply_to: Optional[str] = None
    step: int = 0

    def reply(
        self,
        verb: str,
        payload: Any = None,
        caps: tuple[Capability, ...] = (),
        *,
        sender: Optional[str] = None,
    ) -> "Message":
        """Build the response to this Message, pre-correlated.

        `reply_to` echoes the correlation the requester chose. If they used
        `UnitContext.request(..., then=cb)` that is their continuation id, so the
        callback fires; if they sent plainly it falls back to this Message's id,
        which correlates the pair in the audit trail without triggering anything.

        Replies deliberately do not inherit the request's capabilities. Authority
        is never implicit (invariant 2); if the responder wants to pass a token
        back it must say so explicitly.
        """
        return Message(
            sender=sender or self.recipient,
            recipient=sender or self.sender,
            verb=verb,
            payload=payload,
            caps=caps,
            reply_to=self.reply_to or self.msg_id,
        )

    def describe(self) -> dict:
        """Redacted rendering for inspection and the audit trail."""
        return {
            "msg_id": self.msg_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "verb": self.verb,
            "caps": [str(c) for c in self.caps],
            "reply_to": self.reply_to,
            "step": self.step,
        }

    def __str__(self) -> str:
        arrow = f"{self.sender} -> {self.recipient}"
        caps = f" +{len(self.caps)}cap" if self.caps else ""
        return f"[{self.verb}] {arrow}{caps}"
