"""Units — isolated reactive execution entities (step 1.5).

Constitution section 4: Units are reactive by default and sleep at zero cost
until a Message arrives; each runs in a private memory arena and can only access
resources for which it holds Capabilities; any Unit may be frozen, killed,
restarted or replaced without bringing down the system; and drivers, services,
naming, intent resolution, human interfaces and Improvers are *all* ordinary
Units.

The isolation here is structural, not disciplinary. A Unit's handler is never
handed the Nucleus, another Unit, or the object table. It is handed a
`UnitContext` — a narrow window with four verbs on it. There is no attribute on
that window leading anywhere wider, so a Unit cannot accidentally (or
deliberately) reach outside its arena; the only way out is a Message, and the
only way a Message does anything is by carrying a Capability it already holds.

That is invariant 5 enforced by the shape of the API rather than by convention.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional, Protocol

from .capability import Capability, Right
from .ids import new_id
from .message import Message


class UnitState(str, Enum):
    BORN = "born"
    SLEEPING = "sleeping"  # quiescent, zero cost, waiting for a Message
    RUNNING = "running"
    FROZEN = "frozen"  # contained: retains state, will not be scheduled
    DEAD = "dead"  # arena released, capabilities dropped


class Preempted(RuntimeError):
    """A Unit exceeded its syscall budget and was cut off mid-invocation.

    This is the simulation's stand-in for the hardware timer interrupt. The
    Nucleus's scheduling power (section 2) is meaningless if a Unit can spin
    forever, so every invocation gets a bounded number of operations and the
    scheduler reclaims control when the budget runs out.

    Honest limitation: the budget counts *system calls*, not instructions. A
    Unit looping purely inside its own arena without touching the system cannot
    be preempted in pure Python. Real preemption needs a real timer, which is
    Phase 6 (Lowering Toward Metal) territory.
    """


#: Bounded work per invocation. Small enough that a runaway Unit is caught
#: quickly, large enough that legitimate multi-hop request chains complete.
MAX_SYSCALLS_PER_INVOCATION = 64


class SystemApi(Protocol):
    """The narrow surface a UnitContext is allowed to talk to.

    The Nucleus satisfies this Protocol. Unit code never learns that, and never
    receives the Nucleus itself — only this shape.
    """

    def send_message(self, msg: Message) -> Message: ...
    def find_capability(self, holder: str, right: Right, target: Optional[str]) -> Optional[Capability]: ...
    def describe_holdings(self, holder: str) -> list[dict]: ...
    def current_step(self) -> int: ...


@dataclass
class UnitContext:
    """A Unit's entire view of the system for the duration of one invocation.

    Constructed fresh per message by the Nucleus and discarded afterwards, so a
    Unit cannot stash it and act on stale authority later.

    The preemption budget deliberately does *not* live here. Continuations
    arranged by `request()` run on a later invocation with a different context
    object, and a budget attached to the context would either be double-spent or
    bypassed depending on which one the continuation closed over. Counting against
    the Unit means every context for that Unit charges the same counter, so the
    accounting holds no matter which one is in hand.
    """

    unit: "Unit"
    _api: SystemApi

    @property
    def id(self) -> str:
        return self.unit.unit_id

    @property
    def name(self) -> str:
        return self.unit.name

    @property
    def step(self) -> int:
        return self._api.current_step()

    @property
    def mem(self) -> dict[str, Any]:
        """The Unit's private memory arena.

        The Nucleus owns the backing dict and hands out no second reference, so
        no other Unit can reach it. This is the only unrestricted mutable state
        a Unit has, and it is unrestricted precisely because it is unreachable —
        sharing it would require copying values into a Message, which makes the
        sharing explicit and auditable.
        """
        return self.unit.arena

    def hold(
        self,
        right: Right,
        target: Optional[str] = None,
        target_kind: Optional[str] = None,
    ) -> Optional[Capability]:
        """Find one of my own tokens that authorises `right` against `target`.

        Handles are inert, so a Unit cannot inspect its own authority by reading
        them; it has to ask. This is the only introspection a Unit gets, and it
        is scoped to its own holdings — asking about another principal's tokens
        is not a question this method can express.

        `target_kind` names the kind of Object being acted on, so a token scoped to
        one kind is not selected for another. A Unit that omits it still gets a
        kind-scoped token only when that token's scope is unambiguous, because the
        final authority check happens at the service and will refuse a mismatch.
        """
        return self._api.find_capability(self.unit.unit_id, right, target, target_kind)

    def holdings(self) -> list[dict]:
        """Redacted description of everything I have been given."""
        return self._api.describe_holdings(self.unit.unit_id)

    def send(
        self,
        recipient: str,
        verb: str,
        payload: Any = None,
        caps: tuple[Capability, ...] = (),
        *,
        reply_to: Optional[str] = None,
    ) -> Message:
        """Fire-and-forget. The only way to affect anything outside this arena."""
        self._charge()
        msg = Message(
            sender=self.unit.unit_id,
            recipient=recipient,
            verb=verb,
            payload=payload,
            caps=tuple(caps),
            reply_to=reply_to,
            step=self.step,
        )
        return self._api.send_message(msg)

    def request(
        self,
        recipient: str,
        verb: str,
        payload: Any = None,
        caps: tuple[Capability, ...] = (),
        *,
        then: Optional[Callable[["UnitContext", Message], None]] = None,
    ) -> Message:
        """Send and arrange for `then` to run when the reply comes back.

        This is control-flow sugar over ordinary Messages, not a synchronous
        call: a real Message goes on the wire, the Unit returns to sleep, and
        the continuation fires on a *later* invocation when the reply is routed
        back. Nothing blocks, and the exchange is fully visible in the audit
        trail. It exists only so that a three-hop request chain reads as a
        sequence instead of a hand-rolled state machine.
        """
        if then is not None:
            correlation = new_id("req")
        else:
            correlation = None
        msg = self.send(recipient, verb, payload, caps, reply_to=correlation)
        if correlation is not None:
            self.unit.pending[correlation] = then
        return msg

    def respond(
        self,
        msg: Message,
        verb: str,
        payload: Any = None,
        caps: tuple[Capability, ...] = (),
    ) -> Message:
        """Answer an incoming Message so the requester's continuation fires.

        Echoes the correlation the requester chose rather than inventing a new
        one. This is the shape every service verb uses.
        """
        return self.send(msg.sender, verb, payload, caps, reply_to=msg.reply_to)

    def _charge(self) -> None:
        self.unit.budget += 1
        if self.unit.budget > MAX_SYSCALLS_PER_INVOCATION:
            raise Preempted(
                f"{self.unit.name} exceeded {MAX_SYSCALLS_PER_INVOCATION} system calls in one invocation"
            )


#: Handler signature. A Unit is a function from (its narrow window, a Message)
#: to whatever Messages it chooses to send. It returns nothing: effects happen
#: through ctx.send, which keeps every side effect on the audit trail.
Handler = Callable[[UnitContext, Message], None]


#: Verbs that answer or notify rather than request. The core's own signal and
#: refusal vocabulary is small and fixed, so it is listed outright; everything
#: else that is an answer ends in one of these suffixes.
_SIGNAL_VERBS = frozenset(
    {"interrupt", "nucleus.sealed", "unit.contained", "unit.killed", "core.rejected", "core.denied"}
)
_ANSWER_SUFFIXES = (".result", ".error", ".denied", ".rejected")


def is_answer(verb: str) -> bool:
    """True when a verb is a reply or signal, not a request for action.

    A Unit that receives one of these with no continuation waiting for it must
    swallow it, never answer it. Answering an answer is how two well-behaved
    Units — or a Unit and the core — trade refusals forever: the Watcher's
    fire-and-forget `kill` comes back as `kill.result`, an "unknown verb" error
    to the core comes back as `core.rejected`, and neither side ever runs out of
    something to say. Every handler's fallthrough checks this before replying.
    """
    return verb in _SIGNAL_VERBS or verb.endswith(_ANSWER_SUFFIXES)


#: The table of registered unit kinds. Each Unit kind registers its handler here
#: at import time; the Console lists these as the valid kinds for /spawn.
#:
#: This is the one place where "code" is not itself an Object, and it is worth
#: being explicit about why. A Unit's *parameters* and *chosen entry point* live
#: in a versioned code Object that the Improver can rewrite; the pool of entry
#: points it may choose from is fixed at import time. Nothing rebinds this dict
#: at runtime, so the Improver can recombine and retune existing behaviour but
#: cannot inject new code into the core — which is exactly what section 7 means
#: by "Improvers cannot modify the Nucleus".
#:
#: Loading genuinely new code is Phase 6 work.
UNIT_TYPES: dict[str, Handler] = {}


def unit_type(name: str) -> Callable[[Handler], Handler]:
    """Register a handler as a spawnable unit kind."""

    def register(fn: Handler) -> Handler:
        if name in UNIT_TYPES:
            raise ValueError(f"unit type {name!r} is already registered")
        UNIT_TYPES[name] = fn
        return fn

    return register


class Unit:
    """An isolated execution entity: code + private arena + Capabilities.

    A Unit is a record kept by the Nucleus, not a thread. It has no control flow
    of its own; it is invoked when a Message is routed to it and sleeps between
    invocations at zero cost. That is what makes freezing and killing cheap and
    safe: there is nothing running to interrupt.
    """

    __slots__ = (
        "unit_id",
        "kind",
        "name",
        "state",
        "arena",
        "caps",
        "inbox",
        "pending",
        "code_object_id",
        "code_seq",
        "handler",
        "born_step",
        "crashes",
        "preemptions",
        "messages_handled",
        "replaced_by",
        "budget",
    )

    def __init__(
        self,
        kind: str,
        name: str,
        handler: Handler,
        arena: dict[str, Any],
        *,
        code_object_id: Optional[str] = None,
        code_seq: Optional[int] = None,
        born_step: int = 0,
    ) -> None:
        self.unit_id = new_id("unit")
        self.kind = kind
        self.name = name
        self.state = UnitState.BORN
        self.handler = handler
        #: The private memory arena. Created and owned by the Nucleus; the
        #: reference stored here is the only one any Unit-visible object holds.
        self.arena = arena
        self.caps: set[Capability] = set()
        self.inbox: deque[Message] = deque()
        #: correlation id -> continuation, for ctx.request()
        self.pending: dict[str, Handler] = {}
        self.code_object_id = code_object_id
        self.code_seq = code_seq
        self.born_step = born_step
        self.crashes = 0
        self.preemptions = 0
        self.messages_handled = 0
        self.replaced_by: Optional[str] = None
        #: System calls made during the current invocation. Reset by the Nucleus
        #: before each dispatch; see UnitContext for why it lives here.
        self.budget = 0

    # --- lifecycle -------------------------------------------------------
    @property
    def schedulable(self) -> bool:
        return self.state in (UnitState.SLEEPING, UnitState.RUNNING, UnitState.BORN) and bool(self.inbox)

    def deliver(self, ctx: UnitContext, msg: Message) -> None:
        """Dispatch one Message, honouring continuations if one is waiting.

        Exceptions are not caught here. Letting them propagate to the Nucleus is
        deliberate: containment is the core's job (section 7 says faulty Units
        are immediately contained), and a Unit that swallowed its own crash
        would hide the failure from the Watcher.
        """
        self.messages_handled += 1
        key = msg.reply_to
        if key is not None and key in self.pending:
            continuation = self.pending.pop(key)
            continuation(ctx, msg)
        else:
            self.handler(ctx, msg)

    def describe(self) -> dict:
        """Inspectable state. Transparency over magic (section 8)."""
        return {
            "unit_id": self.unit_id,
            "kind": self.kind,
            "name": self.name,
            "state": self.state.value,
            "caps_held": len(self.caps),
            "inbox_depth": len(self.inbox),
            "pending_requests": len(self.pending),
            "code_object_id": self.code_object_id,
            "code_seq": self.code_seq,
            "born_step": self.born_step,
            "messages_handled": self.messages_handled,
            "crashes": self.crashes,
            "preemptions": self.preemptions,
            "replaced_by": self.replaced_by,
            # Arena *contents* are not exposed. Even for inspection the arena is
            # private; what the human can see is its size, which is enough to
            # notice a leak without breaking isolation.
            "arena_bytes": len(self.arena),
        }

    def __repr__(self) -> str:
        return f"<Unit {self.name} ({self.kind}) {self.state.value}>"
