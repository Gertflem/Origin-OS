"""The Nucleus — the minimal privileged core (step 1.6).

Constitution section 2 gives the Nucleus an exhaustive list of powers: schedule
Units preemptively, create and enforce memory arenas, create/validate/transfer/
revoke Capabilities, route Messages, handle basic interrupts, freeze or kill
Units, and birth the initial boot Units. Everything else must be an ordinary
Unit. Then the Nucleus drops nearly all power immediately after bootstrap.

Three design choices carry most of the weight here:

**Even the core is addressed by Message.** A Unit that wants a Unit spawned does
not call into the Nucleus — it sends a Message to `nucleus` with verb `spawn`
and a SPAWN Capability attached. This keeps invariant 4 literally true: there is
no privileged function-call channel, so the audit trail records every exercise of
core power exactly as it records everything else.

**Sealing is real, not decorative.** Before `seal()`, the Nucleus can mint
Capabilities because bootstrap needs it to. After `seal()`, `mint` refuses
unless the caller presents a valid GRANT token. The core's ambient authority is
spent, not merely unused — which is what section 2 means by dropping power.

**The Nucleus is not a Unit and has no code Object.** The Improver works by
appending versions to a Unit's code Object and asking for a restart. There is no
code Object for the Nucleus, so there is no version to append and no restart to
request. "Improvers cannot modify the Nucleus" is enforced by the absence of a
mechanism rather than by a permission check.

Honest limitation, stated once: this is a single-threaded deterministic
simulation. "Preemptive" scheduling means a bounded syscall budget per
invocation (see unit.Preempted), not a hardware timer. A Unit that spins
entirely inside its own arena without touching the system cannot be interrupted
in pure Python. Real preemption is Phase 6.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from . import constitution, core_verbs
from .capability import (
    Capability,
    CapabilityError,
    CapabilityRecord,
    Proposal,
    RESERVED_RIGHTS,
    Right,
)
from .core_verbs import CORE_VERBS
from .ids import HUMAN, NUCLEUS, new_id
from .message import Message
from .unit import MAX_SYSCALLS_PER_INVOCATION, UNIT_TYPES, Preempted, Unit, UnitContext, UnitState

#: The closed vocabulary of verbs the core answers is declared once, in
#: `core.core_verbs`, and imported above. It is derived from the dispatch table
#: rather than listed beside it, so a verb can never be reachable without also
#: being declared, and the declaration cannot go stale.
#:
#: Verbs the Nucleus may originate on its own. Also closed, and disjoint from
#: everything a Unit might ask for — the core cannot impersonate a service.
CORE_SIGNALS = frozenset({"interrupt", "unit.contained", "unit.killed", "nucleus.sealed"})

#: The only replies the core may send: an answer to a core request, or a refusal.
#: Derived from CORE_VERBS so the two lists cannot drift apart.
CORE_REPLIES = frozenset({f"{v}.result" for v in CORE_VERBS}) | {"core.denied", "core.rejected"}


class UnitNotFound(KeyError):
    pass


@dataclass(frozen=True)
class AuditEntry:
    """One immutable fact about something the core did.

    Section 8 promises transparency over magic. This is the mechanism: every
    capability mint, validation failure, transfer, revocation, route, freeze and
    kill is appended here and can be read back by any principal holding AUDIT.
    """

    step: int
    kind: str
    detail: dict


class Nucleus:
    """The tiny, runtime-immutable, paranoid core."""

    def __init__(self) -> None:
        self._caps: dict[str, CapabilityRecord] = {}
        self._units: dict[str, Unit] = {}
        self._names: dict[str, str] = {}
        #: unit_id -> arena. The authoritative reference, held only here.
        self._arenas: dict[str, dict[str, Any]] = {}
        self._runnable: deque[str] = deque()
        self._step = 0
        self._sealed = False
        self._audit: deque[AuditEntry] = deque(maxlen=4096)
        self._output: deque[dict] = deque()
        self._halted = False

    # =====================================================================
    # Power: create / validate / transfer / revoke Capabilities
    # =====================================================================
    def mint(
        self,
        rights: Iterable[Right],
        target: Optional[str],
        grantor: str,
        holder: str,
        *,
        expires_in: Optional[int] = None,
        label: str = "",
        authority: Optional[Capability] = None,
        target_kind: Optional[str] = None,
    ) -> Capability:
        """Create a Capability.

        Before seal(): permitted, because bootstrap has nothing else to mint
        with. After seal(): the caller must present a valid GRANT token, and the
        new Capability can never be broader than the token that authorised it.
        That last part is what stops a delegate from using a namespace-wide GRANT
        to mint itself a GUARDIAN.

        `target_kind` narrows a grant to Objects of one kind. Delegation stays
        one-directional: a delegate may not hand on a *wider* kind scope than the
        GRANT token it holds, exactly as it may not hand on a wider target.
        """
        rights = frozenset(rights)
        constitution.require(2, bool(rights), "cannot mint an empty Capability")
        constitution.require(
            2,
            not (rights & RESERVED_RIGHTS) or grantor == HUMAN,
            f"only the human principal may mint {sorted(r.value for r in rights & RESERVED_RIGHTS)}",
        )

        if self._sealed:
            if authority is None:
                raise constitution.InvariantViolation(
                    2,
                    f"{grantor} asked the sealed Nucleus to mint authority without presenting a GRANT Capability",
                )
            # A GRANT token's reach is its *target*, not its rights list: holding
            # GRANT on target X means "may delegate authority over X". validate()
            # enforces that scope, and the GUARDIAN-only rule above means no
            # delegate can ever mint an absolute token. Between those two, a
            # delegate can narrow but never widen. Passing `target_kind` is what makes
            # attenuation one-directional for kind scope as well: a delegate
            # restricted to code Objects passes this check when it mints another
            # code-scoped token and fails it when it tries to mint an unrestricted
            # one. Without the argument a narrow kind grant could be laundered back
            # into a namespace-wide token, making the scoping decorative.
            self.validate(authority, Right.GRANT, target, grantor, target_kind)

        cap = Capability.new()
        self._caps[cap.cap_id] = CapabilityRecord(
            cap_id=cap.cap_id,
            rights=rights,
            grantor=grantor,
            holders={holder},
            target=target,
            target_kind=target_kind,
            label=label,
            created_step=self._step,
            expires_at_step=(self._step + expires_in) if expires_in is not None else None,
        )
        self._record(
            "mint",
            {
                "cap": str(cap),
                "rights": sorted(r.value for r in rights),
                "target": target,
                "target_kind": target_kind,
                "grantor": grantor,
                "holder": holder,
                "expires_in": expires_in,
                "label": label,
            },
        )
        if holder in self._units:
            self._units[holder].caps.add(cap)
        return cap

    def validate(
        self,
        cap: Capability,
        right: Right,
        target: Optional[str],
        holder: str,
        target_kind: Optional[str] = None,
    ) -> CapabilityRecord:
        """The single choke point every exercise of authority passes through.

        Invariant 2 says no ambient authority shall exist. This is what makes
        that operational: there is no code path in the system that performs a
        privileged action without calling this first, and this function has no
        exceptions for anyone — not the boot Units, not the Improver, not the
        Nucleus itself.

        `target_kind` is the kind of the Object being acted on, supplied by the
        caller that actually holds the Object. The core cannot resolve kinds
        itself — it has no code Object and knows nothing about storage — so this
        stays an ordinary argument rather than a lookup the privileged core would
        have to perform. It only matters for tokens minted with a `target_kind`
        scope; every other token ignores it.
        """
        rec = self._caps.get(cap.cap_id)
        if rec is None:
            self._record(
                "capability.rejected",
                {"cap": str(cap), "holder": holder, "right": right.value, "reason": "unknown token"},
            )
            raise CapabilityError(
                f"{holder} presented a Capability the Nucleus has never issued", right=right.value
            )
        if holder not in rec.holders:
            self._record(
                "capability.rejected",
                {"cap": str(cap), "holder": holder, "right": right.value, "reason": "not a holder"},
            )
            raise CapabilityError(
                f"{holder} presented a Capability it does not hold", right=right.value, cap_id=cap.cap_id
            )
        if rec.revoked:
            self._record(
                "capability.rejected",
                {"cap": str(cap), "holder": holder, "right": right.value, "reason": "revoked"},
            )
            raise CapabilityError(
                f"Capability {cap} was revoked by {rec.revoked_by}: {rec.revoked_reason}",
                right=right.value,
                cap_id=cap.cap_id,
            )
        if not rec.live_at(self._step):
            self._record(
                "capability.rejected",
                {"cap": str(cap), "holder": holder, "right": right.value, "reason": "expired"},
            )
            raise CapabilityError(
                f"Capability {cap} expired at step {rec.expires_at_step} (now {self._step})",
                right=right.value,
                cap_id=cap.cap_id,
            )
        if not rec.grants(right, target, target_kind):
            self._record(
                "capability.rejected",
                {
                    "cap": str(cap),
                    "holder": holder,
                    "right": right.value,
                    "target": target,
                    "target_kind": target_kind,
                    "scope_kind": rec.target_kind,
                    "reason": "out of scope",
                },
            )
            raise CapabilityError(
                f"Capability {cap} does not grant {right.value} on {target!r}",
                right=right.value,
                cap_id=cap.cap_id,
            )
        return rec

    def transfer(self, cap: Capability, from_holder: str, to_holder: str) -> CapabilityRecord:
        """Delegate a Capability to another principal.

        Delegation, not handover: the sender keeps its copy. That is the standard
        capability semantics and it is what makes revocation meaningful — when
        the grantor revokes, every downstream holder loses the token at once,
        including copies already sitting in inboxes that have not been delivered.
        """
        rec = self._caps.get(cap.cap_id)
        if rec is None or from_holder not in rec.holders:
            raise CapabilityError(f"{from_holder} cannot transfer a Capability it does not hold")
        if rec.revoked or not rec.live_at(self._step):
            raise CapabilityError(f"{from_holder} cannot transfer a dead Capability")
        rec.holders.add(to_holder)
        if to_holder in self._units:
            self._units[to_holder].caps.add(cap)
        self._record("transfer", {"cap": str(cap), "from": from_holder, "to": to_holder})
        return rec

    def _assert_holds(self, cap: Capability, holder: str) -> CapabilityRecord:
        """Check that `holder` legitimately possesses `cap`, without moving it.

        Routing is the moment a Capability is *presented*, not the moment it is
        *delegated*. Conflating the two lets a service accumulate ambient
        authority as a side effect of being talked to: presenting the Console's
        long-lived AUDIT token to the object store would quietly make the store a
        permanent AUDIT holder, and attaching the Guardian to any Unit-directed
        Message would hand a Unit the human's escape hatch. Invariant 2 forbids
        exactly that, so presentation here only proves possession — and does not
        check rights, because what a token grants is decided by the party that
        acts on it, not by the party that carries it.

        Delegation stays an explicit act with a name: mint for a holder (GRANT),
        `endow` at birth, `transfer()`, or the Nucleus's `_rehome`.
        """
        rec = self._caps.get(cap.cap_id)
        if rec is None:
            raise CapabilityError(f"{holder} presented a Capability the Nucleus has never issued")
        if holder not in rec.holders:
            raise CapabilityError(f"{holder} presented a Capability it does not hold")
        if rec.revoked:
            raise CapabilityError(f"{holder} presented a Capability revoked by {rec.revoked_by}")
        if not rec.live_at(self._step):
            raise CapabilityError(f"{holder} presented a Capability that expired at step {rec.expires_at_step}")
        return rec

    def revoke(self, cap: Capability, by: str, reason: str, *, authority: Optional[Capability] = None) -> None:
        """Withdraw a Capability from every holder at once."""
        if by != NUCLEUS:
            if authority is None:
                raise CapabilityError(f"{by} attempted revocation without presenting a REVOKE Capability")
            self.validate(authority, Right.REVOKE, None, by)
        rec = self._caps.get(cap.cap_id)
        if rec is None:
            raise CapabilityError(f"cannot revoke unknown Capability {cap}")
        rec.revoked = True
        rec.revoked_by = by
        rec.revoked_reason = reason
        for uid in list(rec.holders):
            unit = self._units.get(uid)
            if unit is not None:
                unit.caps.discard(cap)
        self._record("revoke", {"cap": str(cap), "by": by, "reason": reason, "holders": sorted(rec.holders)})

    def find_capability(
        self,
        holder: str,
        right: Right,
        target: Optional[str] = None,
        target_kind: Optional[str] = None,
    ) -> Optional[Capability]:
        """Return one live token `holder` owns that grants `right` on `target`.

        `target_kind` mirrors `validate`: a Unit selecting a token says what kind
        of Object it is about to act on, so a kind-scoped token is only ever
        selected for an Object of that kind. Without this a Unit would pick a
        kind-scoped token for the wrong kind and only discover it had done so when
        the service refused the message a hop later.

        Prefers the narrowest and shortest-lived *class* of match, per section 5's
        "temporary is preferred when sufficient": spending a scoped, expiring token
        before a broad permanent one limits the blast radius of whatever happens
        next. That is the primary rank — scoped beats namespace-wide, temporary
        beats permanent — and it is never overridden by the tie-break below.

        The tie-break matters because authority is checked twice, at two different
        steps: once here when a Unit selects a token, and again when the service it
        calls validates it a message-hop later. Among tokens of the *same* rank we
        therefore prefer the one with the most remaining life. Picking the
        soonest-to-expire match instead is a trap: a token can be live when
        selected and dead by the time the append it authorises is processed, so a
        freshly minted intent token must win over an older one about to lapse.
        """
        best: Optional[tuple[tuple[int, int, float], Capability]] = None
        for cap in self._caps.values():
            if holder not in cap.holders or not cap.grants(right, target, target_kind) or not cap.live_at(self._step):
                continue
            # Scoped beats namespace-wide; temporary beats long-lived. Within a
            # rank, a later expiry (None = permanent = +inf) sorts smaller via
            # negation, so the longest-lived match of the narrowest class wins.
            life = float("inf") if cap.expires_at_step is None else float(cap.expires_at_step)
            # A kind-scoped token is narrower than a namespace-wide one, so it
            # ranks with target-scoped tokens rather than with the unrestricted
            # ones. Otherwise the Improver's "code Objects" token would lose to a
            # broader token and the scoping would buy nothing.
            scoped = cap.target is not None or cap.target_kind is not None
            key = (0 if scoped else 1, 0 if cap.is_temporary() else 1, -life)
            if best is None or key < best[0]:
                best = (key, Capability(cap.cap_id))
        return best[1] if best else None

    def describe_holdings(self, holder: str) -> list[dict]:
        return [r.describe() for r in self._caps.values() if holder in r.holders]

    def propose(self, proposal: Proposal, grantor: str, holder: str, *, authority: Optional[Capability] = None) -> Capability:
        """Enact an intent-time proposal (section 5) that the human has confirmed.

        Proposals confer nothing on their own; this is the step where a
        confirmed proposal becomes a real token.
        """
        return self.mint(
            proposal.rights,
            proposal.target,
            grantor,
            holder,
            expires_in=proposal.expires_in_steps,
            label=proposal.label or proposal.reason,
            authority=authority,
            target_kind=proposal.target_kind,
        )

    # =====================================================================
    # Power: create and enforce memory arenas; birth Units
    # =====================================================================
    def _enforce_arena(self, unit_id: str) -> dict[str, Any]:
        """Allocate a fresh private arena and keep the only authoritative handle.

        "Enforce" means the core is the sole holder of the mapping. A Unit gets
        its arena through a UnitContext that is constructed per invocation and
        thrown away, so there is no long-lived reference for another Unit to
        steal, and killing a Unit can genuinely release its memory.
        """
        arena: dict[str, Any] = {}
        self._arenas[unit_id] = arena
        return arena

    def birth(
        self,
        kind: str,
        name: str,
        entry: str,
        *,
        params: Optional[dict] = None,
        code_object_id: Optional[str] = None,
        code_seq: Optional[int] = None,
    ) -> Unit:
        """Bring a Unit into existence. Pre-seal this is a core power; post-seal
        it is reachable only through `spawn` with a valid SPAWN Capability."""
        handler = UNIT_TYPES.get(entry)
        if handler is None:
            raise constitution.InvariantViolation(
                1, f"no unit entry point {entry!r} — the Nucleus cannot invent code"
            )
        unit = Unit(
            kind=kind,
            name=name,
            handler=handler,
            arena={},
            code_object_id=code_object_id,
            code_seq=code_seq,
            born_step=self._step,
        )
        unit.arena = self._enforce_arena(unit.unit_id)
        unit.arena["params"] = dict(params or {})
        self._units[unit.unit_id] = unit
        previous = self._names.get(name)
        self._names[name] = unit.unit_id
        # A replacement Unit legitimately takes over its predecessor's name, so
        # rebinding is allowed — but it is recorded, because a name silently
        # changing what it points at is exactly the kind of thing section 8 says
        # the human must be able to see.
        if previous is not None and previous in self._units and self._units[previous].state is not UnitState.DEAD:
            self._record("name.rebound", {"name": name, "from": previous, "to": unit.unit_id})
        unit.state = UnitState.SLEEPING
        self._record("birth", {"unit_id": unit.unit_id, "name": name, "kind": kind, "entry": entry})
        return unit

    def spawn(
        self,
        requester: str,
        kind: str,
        name: str,
        entry: str,
        *,
        params: Optional[dict] = None,
        code_object_id: Optional[str] = None,
        code_seq: Optional[int] = None,
        authority: Optional[Capability] = None,
        endow: Iterable[Capability] = (),
        replaces: Optional[str] = None,
    ) -> Unit:
        """Capability-mediated birth. The post-bootstrap path."""
        if authority is None:
            raise constitution.InvariantViolation(2, f"{requester} attempted spawn with no SPAWN Capability")
        self.validate(authority, Right.SPAWN, None, requester)
        unit = self.birth(
            kind, name, entry, params=params, code_object_id=code_object_id, code_seq=code_seq
        )
        # The spawner may only endow what it itself holds — authority attenuates
        # down a chain of births, it never grows.
        for cap in endow:
            self.transfer(cap, requester, unit.unit_id)
        if replaces is not None:
            old = self._units.get(replaces)
            if old is not None:
                old.replaced_by = unit.unit_id
                inherited = self._rehome(old, unit)
                self._record(
                    "replace",
                    {
                        "old": replaces,
                        "new": unit.unit_id,
                        "by": requester,
                        "kind": kind,
                        "capabilities_inherited": [str(c) for c in inherited],
                    },
                )
        return unit

    def _rehome(self, old: Unit, new: Unit) -> list[Capability]:
        """Move a contained Unit's authority to the Unit replacing it.

        This is the `transfer_capability` power, exercised by the core on behalf
        of a Unit that has been stopped and can no longer act for itself. Without
        it, containment would strand authority permanently: the frozen Unit keeps
        the only APPEND token reaching its data, and its replacement is born
        unable to do the job it was born to do.

        Narrow on purpose. The predecessor must be FROZEN — a running Unit's
        authority is never moved behind its back — and must be the *same kind*,
        so spawning a harmless-looking `photo` Unit cannot be used to inherit a
        Watcher's KILL token. Every inherited token is named in the audit trail.

        The predecessor keeps its copies rather than losing them, because
        transfer in this system is delegation, not handover; `kill` drops them
        when the old Unit is retired.
        """
        if old.state is not UnitState.FROZEN or old.kind != new.kind:
            return []
        moved: list[Capability] = []
        for rec in self._caps.values():
            if old.unit_id in rec.holders:
                rec.holders.add(new.unit_id)
                handle = Capability(rec.cap_id)
                new.caps.add(handle)
                moved.append(handle)
        return moved

    # =====================================================================
    # Power: route Messages
    # =====================================================================
    def send_message(self, msg: Message) -> Message:
        """Route one Message. The single communications path in the system."""
        msg = Message(
            sender=msg.sender,
            recipient=msg.recipient,
            verb=msg.verb,
            payload=msg.payload,
            caps=msg.caps,
            msg_id=msg.msg_id,
            reply_to=msg.reply_to,
            step=self._step,
        )

        if msg.sender != NUCLEUS:
            cap = self.find_capability(msg.sender, Right.SEND, msg.recipient)
            if cap is None:
                raise CapabilityError(
                    f"{msg.sender} holds no SEND Capability reaching {msg.recipient} and may not communicate"
                )
            self.validate(cap, Right.SEND, msg.recipient, msg.sender)
            # Attaching a token proves the sender legitimately holds it. It does
            # not give the token away; see _assert_holds.
            for attached in msg.caps:
                self._assert_holds(attached, msg.sender)
        else:
            constitution.require(
                1,
                msg.verb in CORE_SIGNALS or msg.verb in CORE_REPLIES,
                f"Nucleus tried to originate non-core verb {msg.verb!r}",
            )

        self._audit_route(msg)

        if msg.recipient == HUMAN:
            # Output to the human is a Message like any other. It simply has no
            # arena to land in, so the core parks it where main.py can drain it.
            self._output.append({"msg_id": msg.msg_id, "verb": msg.verb, "payload": msg.payload})
            return msg

        if msg.recipient == NUCLEUS:
            self._enqueue_core(msg)
            return msg

        unit = self._units.get(msg.recipient)
        if unit is None:
            raise UnitNotFound(msg.recipient)
        if unit.state is UnitState.DEAD:
            self._record("route.dropped", {"msg_id": msg.msg_id, "reason": f"{unit.name} is dead"})
            return msg
        unit.inbox.append(msg)
        if unit.state is UnitState.SLEEPING and unit.unit_id not in self._runnable:
            self._runnable.append(unit.unit_id)
        return msg

    def _audit_route(self, msg: Message) -> None:
        self._record("route", {**msg.describe(), "payload_kind": type(msg.payload).__name__})

    def _enqueue_core(self, msg: Message) -> None:
        """Handle a request addressed to the core itself."""
        if msg.verb not in CORE_VERBS:
            self.send_message(
                msg.reply("core.rejected", {"reason": f"{msg.verb!r} is not a core power"})
            )
            return
        try:
            result = self._execute_core_verb(msg)
            self.send_message(msg.reply(f"{msg.verb}.result", result))
        except (CapabilityError, constitution.InvariantViolation, UnitNotFound) as exc:
            self.send_message(
                msg.reply("core.denied", {"verb": msg.verb, "reason": str(exc), "type": type(exc).__name__})
            )

    def _execute_core_verb(self, msg: Message) -> dict:
        """Answer a core request by delegating to the verb table.

        The dispatch lives in `core.core_verbs`, not here. Section 2 says the
        Nucleus stays tiny, and a branching table inside this class is precisely
        how an application layer starts growing in the privileged core. This
        method keeps one job: hand the request to the table and return its answer.
        """
        return core_verbs.dispatch(self, msg)

    def describe(self, requester: str, cap: Optional[Capability], p: dict) -> dict:
        """Read-only transparency over core bookkeeping. AUDIT-gated.

        Called by the `inspect` verb in `core.core_verbs`. Why this is not an
        extra power: the audit trail, Unit table and Capability registry are core
        state, nothing outside the core can report on them, and section 8 requires
        the human to always be able to inspect Units, Capabilities and Messages.
        It cannot mutate and cannot be called without an AUDIT token.
        """
        if cap is None:
            raise CapabilityError(f"{requester} requested inspection without an AUDIT Capability")
        self.validate(cap, Right.AUDIT, None, requester)
        what = p.get("what", "units")
        if what == "units":
            return {"units": [u.describe() for u in self._units.values()]}
        if what == "agents":
            agents = []
            for unit in self._units.values():
                if unit.kind != "agent":
                    continue
                # Containment is kernel truth. A Unit the core has frozen or
                # killed is not "running" merely because its arena still carries
                # that self-report; letting the arena win would hide containment
                # from /agents, which section 8 forbids. While the Unit is alive,
                # its own lifecycle state is the meaningful one to show.
                if unit.state in (UnitState.FROZEN, UnitState.DEAD):
                    state = unit.state.value
                else:
                    state = unit.arena.get("state", unit.state.value)
                rec = {
                    "unit_id": unit.unit_id,
                    "name": unit.name,
                    "kind": unit.kind,
                    "state": state,
                    "started_at": unit.arena.get("started_at"),
                    "last_heartbeat": unit.arena.get("last_heartbeat"),
                    "stopped_at": unit.arena.get("stopped_at"),
                    "failed_at": unit.arena.get("failed_at"),
                    "failure_reason": unit.arena.get("failure_reason"),
                    "recovered_at": unit.arena.get("recovered_at"),
                    "replaced_by": unit.arena.get("replaced_by", getattr(unit, "replaced_by", None)),
                    "tools": unit.arena.get("tools", []),
                    "memory_scope": unit.arena.get("memory_scope"),
                    "born_step": unit.born_step,
                    "messages_handled": unit.messages_handled,
                    "caps_held": len(unit.caps),
                }
                agents.append(rec)
            return {"agents": agents}
        if what == "caps":
            return {"capabilities": [r.describe() for r in self._caps.values()]}
        if what == "snapshot":
            return {
                "step": self._step,
                "sealed": self._sealed,
                "units": [u.describe() for u in self._units.values()],
                "capabilities": [r.describe() for r in self._caps.values()],
                "runnable": list(self._runnable),
            }
        if what == "audit":
            kinds = set(p["kinds"]) if p.get("kinds") else None
            entries = [e for e in self._audit if kinds is None or e.kind in kinds]
            limit = int(p.get("limit", 40))
            return {"audit": [{"step": e.step, "kind": e.kind, **e.detail} for e in entries[-limit:]]}
        if what == "messages":
            entries = [e for e in self._audit if e.kind == "route"]
            limit = int(p.get("limit", 40))
            return {"messages": [{"step": e.step, "kind": e.kind, **e.detail} for e in entries[-limit:]]}
        if what == "powers":
            return self.audit_powers()
        raise CapabilityError(f"unknown inspection target {what!r}")

    # =====================================================================
    # Power: handle basic interrupts
    # =====================================================================
    def interrupt(self, recipient: str, verb: str, payload: Any = None) -> Optional[Message]:
        """Originate a core signal. Closed vocabulary, enforced in send_message."""
        constitution.require(1, verb in CORE_SIGNALS, f"{verb!r} is not a core signal")
        if recipient not in self._units:
            return None
        return self.send_message(Message(sender=NUCLEUS, recipient=recipient, verb=verb, payload=payload))

    # =====================================================================
    # Power: freeze or kill Units
    # =====================================================================
    def freeze(self, unit_id: str, by: str, reason: str, *, authority: Optional[Capability] = None) -> None:
        """Contain a Unit. State and Capabilities are retained; it will not run."""
        if by not in (NUCLEUS, HUMAN):
            if authority is None:
                raise CapabilityError(f"{by} attempted freeze without a FREEZE Capability")
            self.validate(authority, Right.FREEZE, unit_id, by)
        unit = self._require_unit(unit_id)
        if unit.state is UnitState.DEAD:
            return
        unit.state = UnitState.FROZEN
        if unit_id in self._runnable:
            self._runnable.remove(unit_id)
        self._record("freeze", {"unit_id": unit_id, "name": unit.name, "by": by, "reason": reason})

    def kill(self, unit_id: str, by: str, reason: str, *, authority: Optional[Capability] = None) -> None:
        """Release a Unit's arena and drop it from every Capability it held.

        Undelivered messages in its inbox are recorded in the audit trail before
        being discarded, so the human can always see what was in flight. Anything
        the Unit wanted to survive should already have been appended to an
        Object — arena memory is private scratch space, not state.
        """
        if by not in (NUCLEUS, HUMAN):
            if authority is None:
                raise CapabilityError(f"{by} attempted kill without a KILL Capability")
            self.validate(authority, Right.KILL, unit_id, by)
        unit = self._require_unit(unit_id)
        dropped = [m.describe() for m in unit.inbox]
        for rec in self._caps.values():
            rec.holders.discard(unit_id)
        unit.caps.clear()
        unit.inbox.clear()
        unit.pending.clear()
        self._arenas.pop(unit_id, None)
        unit.arena.clear()
        unit.state = UnitState.DEAD
        if unit_id in self._runnable:
            self._runnable.remove(unit_id)
        # Only release the name if it still points here. A replacement Unit takes
        # over its predecessor's name at birth, so by the time the old one is
        # retired the name already belongs to the new one — popping it
        # unconditionally would orphan a live Unit.
        if self._names.get(unit.name) == unit_id:
            self._names.pop(unit.name, None)
        self._record(
            "kill",
            {"unit_id": unit_id, "name": unit.name, "by": by, "reason": reason, "dropped_messages": dropped},
        )

    def _require_unit(self, unit_id: str) -> Unit:
        unit = self._units.get(unit_id)
        if unit is None:
            raise UnitNotFound(unit_id)
        return unit

    # =====================================================================
    # Power: schedule Units preemptively
    # =====================================================================
    def current_step(self) -> int:
        return self._step

    def schedule(self, max_steps: int = 5000) -> int:
        """Run until every Unit is asleep or the step budget is exhausted.

        Round-robin over Units with non-empty inboxes, one message per turn, then
        the Unit goes to the back of the queue. Fair, deterministic, and
        inspectable — the same run twice produces the same trace.
        """
        executed = 0
        while self._runnable and executed < max_steps:
            self._tick()
            executed += 1
        return executed

    def _tick(self) -> None:
        unit_id = self._runnable.popleft()
        unit = self._units.get(unit_id)
        if unit is None or not unit.schedulable:
            return

        msg = unit.inbox.popleft()
        unit.state = UnitState.RUNNING
        unit.budget = 0
        ctx = UnitContext(unit=unit, _api=self)
        try:
            unit.deliver(ctx, msg)
        except Preempted as exc:
            unit.preemptions += 1
            self.freeze(unit_id, by=NUCLEUS, reason=f"preempted: {exc}")
            self._contain(unit, "preempted", str(exc))
            return
        except constitution.InvariantViolation as exc:
            unit.crashes += 1
            self.freeze(unit_id, by=NUCLEUS, reason=f"invariant {exc.invariant}: {exc.detail}")
            self._contain(unit, "invariant_violation", f"invariant {exc.invariant}: {exc.detail}")
            return
        except CapabilityError as exc:
            # A capability abuse attempt is a security event, not a bug. The
            # Unit is contained and the Watcher is told, per section 7.
            unit.crashes += 1
            self.freeze(unit_id, by=NUCLEUS, reason=f"capability abuse: {exc}")
            self._contain(unit, "capability_abuse", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - containment must not be selective
            unit.crashes += 1
            self.freeze(unit_id, by=NUCLEUS, reason=f"{type(exc).__name__}: {exc}")
            self._contain(unit, "crashed", f"{type(exc).__name__}: {exc}")
            return
        finally:
            if unit.state is UnitState.RUNNING:
                unit.state = UnitState.SLEEPING

        self._step += 1
        if unit.schedulable and unit_id not in self._runnable:
            self._runnable.append(unit_id)

    def _contain(self, unit: Unit, kind: str, detail: str) -> None:
        """Faulty Units are immediately contained, and someone is told (section 7)."""
        self._record(
            "contain",
            {"unit_id": unit.unit_id, "name": unit.name, "kind": kind, "detail": detail, "step": self._step},
        )
        for watcher in self._units_of_kind("watcher"):
            if watcher.unit_id != unit.unit_id:
                self.interrupt(
                    watcher.unit_id,
                    "unit.contained",
                    {
                        "unit_id": unit.unit_id,
                        "name": unit.name,
                        "kind": unit.kind,
                        "event": kind,
                        "detail": detail,
                        "crashes": unit.crashes,
                        "code_object_id": unit.code_object_id,
                        "code_seq": unit.code_seq,
                    },
                )

    # =====================================================================
    # Bootstrap and sealing
    # =====================================================================
    def seal(self) -> None:
        """Drop nearly all remaining privilege (sections 2 and 9).

        After this the core can still validate, route, schedule, freeze and kill
        — those are its enumerated powers and the system needs them forever. What
        it can no longer do is *originate* authority: minting requires a GRANT
        token, spawning requires a SPAWN token, and boot birth is spent.
        """
        if self._sealed:
            return
        self._sealed = True
        self._record("seal", {"step": self._step, "dropped": ["ambient_mint", "ambient_birth"]})
        for unit in self._units.values():
            self.interrupt(unit.unit_id, "nucleus.sealed", {"step": self._step})

    @property
    def sealed(self) -> bool:
        return self._sealed

    # =====================================================================
    # Inspection (requires AUDIT; enforced by the caller holding a token)
    # =====================================================================
    def _units_of_kind(self, kind: str) -> list[Unit]:
        return [u for u in self._units.values() if u.kind == kind and u.state is not UnitState.DEAD]

    def _unit_by_name(self, name: str) -> Optional[Unit]:
        uid = self._names.get(name)
        return self._units.get(uid) if uid else None

    def find_unit(self, name: str, authority: Capability, holder: str) -> Optional[dict]:
        """Look a Unit up by name, for a principal holding AUDIT.

        Unguarded name lookup is exactly what section 5 forbids — discovery of
        names without a relevant Capability must be impossible — so even this
        read-only helper demands a token.
        """
        self.validate(authority, Right.AUDIT, None, holder)
        unit = self._unit_by_name(name)
        return unit.describe() if unit else None

    def snapshot(self, authority: Capability, holder: str) -> dict:
        """Whole-system state, for a principal holding AUDIT.

        This is how section 8's promise is kept: the human can always inspect
        Objects, Units, Capabilities and Messages. Capability secrets are
        redacted by `CapabilityRecord.describe`, so transparency does not become
        a leak.
        """
        self.validate(authority, Right.AUDIT, None, holder)
        return {
            "step": self._step,
            "sealed": self._sealed,
            "units": [u.describe() for u in self._units.values()],
            "capabilities": [r.describe() for r in self._caps.values()],
            "runnable": list(self._runnable),
        }

    def audit(self, authority: Capability, holder: str, *, limit: int = 40, kinds: Optional[set[str]] = None) -> list[dict]:
        self.validate(authority, Right.AUDIT, None, holder)
        entries = [e for e in self._audit if kinds is None or e.kind in kinds]
        return [{"step": e.step, "kind": e.kind, **e.detail} for e in entries[-limit:]]

    @property
    def output(self) -> deque[dict]:
        """Messages addressed to the human, awaiting collection by main.py."""
        return self._output

    def drain_output(self) -> list[dict]:
        items = list(self._output)
        self._output.clear()
        return items

    def _record(self, kind: str, detail: dict) -> None:
        self._audit.append(AuditEntry(step=self._step, kind=kind, detail=detail))

    # =====================================================================
    # Invariant 1 / 6 self-check
    # =====================================================================
    #: Every public method of the Nucleus, mapped to the constitutional power it
    #: exercises. The test suite asserts this table covers the whole public
    #: surface, which turns "the Nucleus remains tiny" from an aspiration into a
    #: check that fails the build when someone adds a power.
    POWER_MAP = {
        "mint": "mint_capability",
        "validate": "validate_capability",
        "transfer": "transfer_capability",
        "revoke": "revoke_capability",
        "find_capability": "validate_capability",
        "describe_holdings": "validate_capability",
        "propose": "mint_capability",
        "birth": "birth_boot_units",
        "spawn": "birth_boot_units",
        "send_message": "route_message",
        "interrupt": "handle_interrupt",
        "freeze": "freeze_unit",
        "kill": "kill_unit",
        "schedule": "schedule",
        "seal": "birth_boot_units",
        "current_step": "schedule",
    }

    #: Read-only transparency surface (section 8). These are not powers over the
    #: system — `snapshot` and `audit` both demand an AUDIT token before they
    #: return anything, and `drain_output` only empties the human's own outbox.
    #: They are listed separately so the tiny-core check does not mistake
    #: transparency for authority.
    #:
    #: `describe` is here for the same reason `inspect` is an allowed verb: the
    #: Unit table, Capability registry and audit trail are core state that nothing
    #: else can report on, and section 8 requires the human to inspect them.
    INSPECTION = frozenset({"snapshot", "audit", "drain_output", "find_unit", "describe"})

    def audit_powers(self) -> dict:
        """Report whether the core's public surface still fits the Constitution.

        Invariant 1 says the Nucleus remains tiny; invariant 6 says the system
        stays comprehensible to one mind. Neither is enforceable by good
        intentions, so this method exists to be asserted against in the test
        suite: add a public method without mapping it to a constitutional power
        and the build fails.
        """
        public = {
            n
            for n in dir(self)
            if not n.startswith("_") and callable(getattr(self, n))
        }
        accounted = set(self.POWER_MAP) | self.INSPECTION | {"audit_powers"}
        unmapped = sorted(public - accounted)
        extra = sorted({p for p in self.POWER_MAP.values()} - constitution.NUCLEUS_POWERS)
        return {
            "public_methods": sorted(public),
            "unmapped": unmapped,
            "powers_outside_constitution": extra,
            "tiny": not unmapped and not extra,
        }

    def __repr__(self) -> str:
        return f"<Nucleus step={self._step} units={len(self._units)} caps={len(self._caps)} sealed={self._sealed}>"
