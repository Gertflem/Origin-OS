"""The Console Unit — the first human interface (step 1.7).

Section 8 promises the human can always see what the system is doing. Section 6
promises that an intent becomes an action only by going through express ->
resolve -> propose capabilities -> confirm -> execute -> inspectable result.
This Unit is where both promises are kept, and it is the only place in Phase 1
where a human typing a sentence turns into Messages on the wire.

**It is deliberately not privileged.** Five standing tokens: SEND so it can talk
at all, RESOLVE and BIND so it can work with names, AUDIT so it can answer
inspection, GRANT so it can propose intent-time Capabilities. That is the whole
list. The Console cannot read a photo, freeze a Unit or kill one. For each of
those it mints a narrow token — one target, a handful of steps to live — and only
after the human has confirmed. Authority is borrowed per action and decays by
itself, so a Console that goes wrong is a short-lived problem rather than a
permanent one.

Two things it cannot do even in principle:

REVOKE and GUARDIAN are reserved to the human principal (see
capability.RESERVED_RIGHTS), so no Console code path can withdraw a Capability or
wield the escape hatch. `/revoke` is intercepted in main.py and sent to the
Nucleus *as the human*, never through here — the Guardian Capability is held by
no Unit, literally rather than by convention.

Everything below is ordinary: it receives Messages, holds tokens, and sends
Messages. If this Unit is wrong, the Watcher can contain it and the Improver can
replace it, and the human loses nothing but the ability to type until a new one
is running.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from ..capability import RESERVED_RIGHTS, Capability, Right
from ..ids import HUMAN, NUCLEUS, mask
from ..message import Message
from ..unit import UNIT_TYPES, UnitContext, is_answer, unit_type
from .improver import PROTECTED_KINDS

#: Words that enact a proposal. Kept short and unambiguous on purpose: nothing
#: that could plausibly be part of a natural-language intent.
CONFIRM = frozenset({"y", "yes", "confirm", "ok", "okay", "go", "do it", "sure"})
CANCEL = frozenset({"n", "no", "cancel", "stop", "abort", "never mind", "nevermind"})

HELP = """Origin — Phase 1 console. Say what you want, or use a command.

  intents (plain language, resolved by the Naming Unit)
    brighten the beach photo by 20
    resize it to 50%
    send the beach photo to David
    show the beach photo
    count the tally

  inspection (costs nothing new; the Console holds AUDIT)
    /help              this text
    /names             every binding the Naming Unit holds
    /objects           every Object in the store
    /units             every Unit, its state and its crash count
    /agents            the agent registry: lifecycle state, heartbeat and tools
    /caps              every Capability ever minted, redacted
    /audit [n]         the last n core events
    /messages [n]      the last n routed Messages
    /log [n]           the last n notices and console events
    /watcher [n]       the last n containment and escalation events
    /improver [n]      the last n repair decisions and attempts
    /powers            the Nucleus's own account of its powers

  actions (proposed first, executed only on 'confirm')
    /focus [name]      show or set what "this" and "it" refer to
    /show <name>       read an Object under a freshly minted READ token
    /history <name>    version metadata for an Object
    /spawn <kind> <name> [entry]      birth a new Unit under a temporary SPAWN token
    /agent <name> [start|status|heartbeat|stop]   manage a simple agent lifecycle
    /freeze <name>     contain a Unit
    /kill <name>       retire a Unit and release its arena
    /rollback <name> [seq]   ask the Improver to restore an older code version
    /grant <right[,right]> <target|-> <unit|-> [steps]   delegate a Capability
    /work [n]          poke the fault-injected Unit, so self-healing has work

  /revoke does not go through this Unit. It is reserved to the Guardian.
"""


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _say(ctx: UnitContext, text: str) -> None:
    """Talk to the human. Output is a Message like any other; it simply has no
    arena to land in, so the Nucleus parks it where main.py can drain it."""
    ctx.send(HUMAN, "console.output", {"text": text})


def _render(value: Any) -> str:
    """Compact one-line rendering of a payload.

    Hand-rolled rather than `json.dumps` because this text is read by a human on
    a console, and because ASCII-only output has to survive a raw Windows code
    page — the same reason ids.mask avoids a typographic ellipsis.
    """
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_render(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_render(v) for v in value) + "]"
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        return f"{value:g}"
    return repr(value)


def _lifetime(ctx: UnitContext) -> int:
    return int(ctx.mem["params"].get("token_lifetime", 24))


def _services(ctx: UnitContext) -> dict[str, str]:
    """The live role -> unit_id table.

    Kept in the arena rather than read from `params` each time, because an
    Improver replacing a Unit mints a *new* unit_id: the table has to move with
    it, and the versioned code Object that seeded it must not.
    """
    return ctx.mem.setdefault("services", dict(ctx.mem["params"].get("services", {})))


def _set_focus(ctx: UnitContext, target: str, name: str) -> None:
    ctx.mem["focus"]["object_id"] = target
    ctx.mem["focus"]["name"] = name


# ---------------------------------------------------------------------------
# Propose / confirm
# ---------------------------------------------------------------------------
def _propose(ctx: UnitContext, summary: str, cost: list[str], action: dict) -> None:
    """Section 5's proposal step, in words a human can actually read.

    The proposal names the authority it will mint, who will hold it, and when it
    expires. Nothing happens until the human says so, and the pending action is
    plain data in the arena — inspectable, not a closure waiting to fire.
    """
    ctx.mem["pending"] = {"summary": summary, "cost": cost, "action": action}
    lines = [f"PROPOSED: {summary}", "  authority cost:"]
    lines += [f"    - {c}" for c in cost] if cost else ["    - none (standing authority only)"]
    lines.append("  type 'confirm' to proceed or 'cancel' to drop it.")
    _say(ctx, "\n".join(lines))


def _propose_choice(ctx: UnitContext, plan: dict, amb: dict) -> None:
    """Section 6: ambiguity is resolved by presenting suggestions, never by
    picking the top guess and hoping."""
    cands = amb.get("candidates") or []
    role = amb.get("role", "target")
    phrase = amb.get("phrase", "")
    if not cands:
        _say(ctx, f"Nothing in the namespace matches {phrase!r}. Bind a name to it first, or rephrase.")
        return
    lines = [f"I am not sure what {phrase!r} refers to. The closest matches are:"]
    lines += [
        f"  {n}. {c['name']}  ({c['kind']}, score {c['score']}) {c.get('description', '')}".rstrip()
        for n, c in enumerate(cands, 1)
    ]
    lines.append("Choose by number, or type 'cancel'.")
    _say(ctx, "\n".join(lines))

    index = 0
    for i, step in enumerate(plan["steps"]):
        if step.get(role) is None and step.get(f"{role}_phrase") == phrase:
            index = i
            break
    ctx.mem["pending"] = {
        "action": {"op": "intent", "plan": plan, "i": 0},
        "choices": cands,
        "patch": {"step": index, "role": role},
    }


def _choose(ctx: UnitContext, pending: dict, picked: dict) -> None:
    patch = pending.get("patch") or {}
    plan = pending["action"]["plan"]
    step = plan["steps"][patch.get("step", 0)]
    step[patch.get("role", "target")] = picked["target"]
    step["candidates"] = []
    plan["ambiguities"] = []
    plan["resolved"] = True
    _propose(ctx, _summarise(plan), _cost_of(ctx, plan), {"op": "intent", "plan": plan, "i": 0})


# ---------------------------------------------------------------------------
# Minting: how a Unit that holds nothing gets exactly enough
# ---------------------------------------------------------------------------
def _mint(
    ctx: UnitContext,
    rights: tuple[Right, ...],
    target: Optional[str],
    label: str,
    holder: str,
    then: Callable[[UnitContext, Capability], None],
    *,
    permanent: bool = False,
) -> None:
    """Ask the core for one narrow token, then continue when it arrives.

    This is a real Message to the Nucleus under the `mint` core verb, with the
    Console's GRANT token attached, so the grant is in the audit trail with the
    step it happened at and the reason it was given. Post-seal the core cannot
    mint without that token, which is what makes the Console's GRANT the single
    place intent-time authority enters the system.

    `permanent` is off by default: intent-time authority expires. It is set only
    for standing infrastructure a Unit needs for its whole life — a reply channel
    — which mirrors the permanent scoped tokens bootstrap grants the boot Units.
    """
    grant = ctx.hold(Right.GRANT, target)
    if grant is None:
        _say(ctx, f"I hold no GRANT authority reaching {mask(target) if target else 'the namespace'}, "
                  f"so I cannot propose that Capability.")
        return
    ctx.request(
        NUCLEUS,
        "mint",
        {
            "rights": [r.value for r in rights],
            "target": target,
            "holder": holder,
            "expires_in": None if permanent else _lifetime(ctx),
            "label": label,
        },
        (grant,),
        then=lambda c, reply: _minted(c, reply, then),
    )


def _minted(ctx: UnitContext, reply: Message, then: Callable[[UnitContext, Capability], None]) -> None:
    if reply.verb != "mint.result":
        _say(ctx, f"REFUSED: the Nucleus would not mint — {(reply.payload or {}).get('reason', reply.payload)}")
        return
    then(ctx, Capability(reply.payload["cap_id"]))


def _mint_all(ctx: UnitContext, grants: list[dict], holder: str, then: Callable[[UnitContext], None]) -> None:
    remaining = list(grants)

    def next_one(c: UnitContext) -> None:
        if not remaining:
            then(c)
            return
        g = remaining.pop(0)
        _mint(c, g["rights"], g["target"], g["label"], holder, lambda c2, _cap: next_one(c2))

    next_one(ctx)


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------
@unit_type("console")
def console_handler(ctx: UnitContext, msg: Message) -> None:
    _services(ctx)
    ctx.mem.setdefault("focus", {})
    ctx.mem.setdefault("pending", None)
    ctx.mem.setdefault("log", [])

    if msg.verb == "console.input":
        _input(ctx, (msg.payload or {}).get("text", ""))
        return

    if msg.verb == "console.notice":
        # The Watcher and the Improver report through here. Forwarded and never
        # answered: a notice is a fact about the system, and replying to it would
        # start a conversation nobody asked for.
        text = str((msg.payload or {}).get("text", ""))
        ctx.mem["log"].append({"event": "notice", "text": text, "at_step": ctx.step})
        _say(ctx, f"! {text}")
        return

    if msg.verb == "console.replaced":
        _replaced(ctx, msg.payload or {})
        return

    if msg.verb == "flaky.done":
        # Sent fire-and-forget by /work, so it has no continuation waiting.
        _say(ctx, f"work: {_render(msg.payload)}")
        return

    if msg.verb == "nucleus.sealed":
        _say(
            ctx,
            "The Nucleus has sealed itself. Its bootstrap authority is spent, not merely unused: "
            "from here every action — including the core's own — happens through a Capability.",
        )
        return

    if not is_answer(msg.verb):
        ctx.respond(msg, "console.error", {"reason": f"unknown verb {msg.verb!r}"})


def _replaced(ctx: UnitContext, p: dict) -> None:
    """A Unit was improved. Re-point the service table, then the human-facing name.

    The Improver cannot bind names — it holds no BIND token — so the Console does
    it. That is a deliberate split: the agent that rewrote the code is not the
    agent that decides what the name now means.
    """
    kind = p.get("kind")
    new_id = p.get("new_unit_id")
    if kind and new_id:
        _services(ctx)[kind] = new_id
    name = p.get("name")
    bind = ctx.hold(Right.BIND, name)
    if not name or not new_id or bind is None:
        _say(ctx, f"! {name or 'a Unit'} was replaced but I could not rebind its name.")
        return

    def on_bound(c: UnitContext, reply: Message) -> None:
        if reply.verb != "name.bound":
            _say(c, f"! {name} was replaced but the name bind did not confirm.")
            return
        _set_focus(c, new_id, name)
        _say(c, f"! {name} now refers to {mask(new_id)} — {p.get('reason', '')}".rstrip(" —"))

    ctx.request(
        _services(ctx).get("naming", ""),
        "name.bind",
        {
            "name": name,
            "target": new_id,
            "kind": "unit",
            "description": f"{kind} Unit, code seq {p.get('code_seq')} ({p.get('reason', '')})",
        },
        (bind,),
        then=on_bound,
    )


def _agent_result(ctx: UnitContext, reply: Message, name: str, action: str) -> None:
    payload = reply.payload or {}
    if reply.verb == "agent.error":
        _say(ctx, f"{name} rejected the {action} request: {payload.get('reason', payload)}")
        return
    if reply.verb not in {"agent.started", "agent.stopped", "agent.heartbeat", "agent.failed", "agent.recovered", "agent.status"}:
        _say(ctx, f"{name} replied with {reply.verb!r} while I was handling {action!r}.")
        return
    state = payload.get("state", "unknown")
    if action == "status":
        started = payload.get("started_at")
        heartbeat = payload.get("last_heartbeat")
        stopped = payload.get("stopped_at")
        failed = payload.get("failed_at")
        reason = payload.get("failure_reason")
        tools = payload.get("tools") or []
        scope = payload.get("memory_scope")
        parts = [f"{name}: state={state}"]
        if started is not None:
            parts.append(f"started={started}")
        if heartbeat is not None:
            parts.append(f"heartbeat={heartbeat}")
        if stopped is not None:
            parts.append(f"stopped={stopped}")
        if failed is not None:
            parts.append(f"failed={failed}")
        if reason is not None:
            parts.append(f"reason={reason}")
        if tools:
            parts.append(f"tools={','.join(tools)}")
        if scope is not None:
            parts.append(f"scope={scope}")
        _say(ctx, ", ".join(parts))
        return
    tools = payload.get("tools") or []
    scope = payload.get("memory_scope")
    reason = payload.get("failure_reason")
    if tools or scope is not None or reason is not None:
        detail = ""
        if tools:
            detail += f" tools={','.join(tools)}"
        if scope is not None:
            detail += f" scope={scope}"
        if reason is not None:
            detail += f" reason={reason}"
        _say(ctx, f"{name} is {state}.{detail}")
        return
    _say(ctx, f"{name} is {state}.")


def _agent_command(ctx: UnitContext, rest: list[str]) -> None:
    if not rest:
        _say(ctx, "Use '/agent <name> [start|status|heartbeat|fail|recover|stop]' to manage a simple agent lifecycle.")
        return
    name = rest[0]
    action = (rest[1].lower() if len(rest) > 1 else "status")
    if action not in {"start", "status", "heartbeat", "fail", "recover", "stop"}:
        _say(ctx, f"{action!r} is not a valid agent action. Use start, status, heartbeat, fail, recover, or stop.")
        return

    def on_unit(c: UnitContext, unit: dict) -> None:
        target = unit.get("unit_id")
        send_cap = c.hold(Right.SEND, target)
        if send_cap is None:
            _say(c, f"I hold no SEND authority for {unit.get('name', target)}.")
            return
        c.request(target, f"agent.{action}", {"name": unit.get("name", "")}, (send_cap,), then=lambda c2, reply: _agent_result(c2, reply, unit.get("name", ""), action))

    _with_unit(ctx, name, on_unit)


def _input(ctx: UnitContext, text: str) -> None:
    text = (text or "").strip()
    pending = ctx.mem["pending"]
    low = text.lower()

    if pending is not None:
        if low in CONFIRM:
            ctx.mem["pending"] = None
            _execute(ctx, pending["action"])
            return
        if low in CANCEL:
            ctx.mem["pending"] = None
            _say(ctx, "Dropped. Nothing was minted and nothing ran.")
            return
        if pending.get("choices") and text.isdigit():
            choices = pending["choices"]
            index = int(text) - 1
            if 0 <= index < len(choices):
                ctx.mem["pending"] = None
                _choose(ctx, pending, choices[index])
                return
            _say(ctx, f"{text} is not one of the options. Try 1-{len(choices)}, or 'cancel'.")
            return
        # Anything else supersedes the proposal. Silently keeping a stale one
        # around would mean a later 'confirm' enacting something the human has
        # already moved on from.
        ctx.mem["pending"] = None

    if not text:
        return
    if text.startswith("/"):
        _command(ctx, text)
    else:
        _express(ctx, text)


def _command(ctx: UnitContext, text: str) -> None:
    parts = text[1:].split()
    cmd = parts[0].lower() if parts else ""
    rest = parts[1:]
    arg = rest[0] if rest else ""

    if cmd == "help":
        _say(ctx, HELP)

    elif cmd == "names":
        _list_names(ctx)

    elif cmd == "objects":
        _list_objects(ctx)

    elif cmd == "agent":
        _agent_command(ctx, rest)

    elif cmd == "status":
        _status(ctx)

    elif cmd in ("units", "caps", "audit", "messages", "powers", "agents"):
        _inspect_core(ctx, cmd, rest)

    elif cmd == "log":
        _log(ctx, int(rest[0]) if rest and rest[0].isdigit() else 20)

    elif cmd in ("watcher", "improver"):
        _inspect_unit(ctx, cmd, rest)

    elif cmd == "focus":
        if not arg:
            _say(ctx, f"focus: {_render(ctx.mem['focus']) or 'nothing'}")
        else:
            _resolve(ctx, arg, "object", _focus_on)

    elif cmd in ("show", "history"):
        if not arg:
            _say(ctx, f"usage: /{cmd} <name>")
        else:
            _resolve(ctx, arg, "object", lambda c, target, name: _propose_read(c, target, name, cmd))

    elif cmd == "spawn":
        if len(rest) < 2:
            _say(ctx, "usage: /spawn <kind> <name> [entry]")
        else:
            kind = rest[0]
            name = rest[1]
            if kind not in UNIT_TYPES:
                _say(ctx, f"{kind!r} is not a registered Unit kind. Choices: {', '.join(sorted(UNIT_TYPES))}.")
                return
            entry = rest[2] if len(rest) > 2 else kind
            _propose(
                ctx,
                f"spawn {kind} {name}",
                [
                    f"SPAWN on the namespace, granted to me, expires in {_lifetime(ctx)} steps",
                    f"SEND to the console, granted to {name}, so it can answer me (permanent, revocable)",
                ],
                {"op": "spawn", "kind": kind, "name": name, "entry": entry, "params": {}},
            )

    elif cmd in ("freeze", "kill"):
        if not arg:
            _say(ctx, f"usage: /{cmd} <unit name or id prefix>")
        else:
            _with_unit(ctx, arg, lambda c, unit: _propose_core(c, unit, cmd))

    elif cmd == "rollback":
        if not arg:
            _say(ctx, "usage: /rollback <unit name> [seq]")
        else:
            _with_unit(ctx, arg, lambda c, unit: _propose_rollback(c, unit, rest[1:]))

    elif cmd == "grant":
        if len(rest) < 3:
            _say(ctx, "usage: /grant <right[,right...]> <target|-> <unit|-> [steps]")
        else:
            _grant_flow(ctx, rest)

    elif cmd == "work":
        _work(ctx, int(rest[0]) if rest and rest[0].isdigit() else 1)

    elif cmd == "revoke":
        _say(
            ctx,
            "Revocation is reserved to the human principal and does not pass through this Unit. "
            "main.py sends /revoke to the Nucleus as the Guardian.",
        )

    else:
        _say(ctx, f"I do not know the command {cmd!r}. Type /help.")


# ---------------------------------------------------------------------------
# Read-only inspection: section 8, served with the Console's standing AUDIT
# ---------------------------------------------------------------------------
def _audit_cap(ctx: UnitContext) -> Optional[Capability]:
    cap = ctx.hold(Right.AUDIT)
    if cap is None:
        _say(ctx, "I hold no AUDIT Capability, so I cannot inspect anything. That needs the Guardian.")
    return cap


def _list_names(ctx: UnitContext) -> None:
    naming = _services(ctx).get("naming")
    cap = ctx.hold(Right.RESOLVE)
    if naming is None or cap is None:
        _say(ctx, "I cannot list names: no Naming Unit is running, or I hold no RESOLVE Capability.")
        return

    def listed(c: UnitContext, reply: Message) -> None:
        p = reply.payload or {}
        if reply.verb != "name.list":
            _say(c, f"Naming refused: {p.get('reason', reply.verb)}")
            return
        rows = p.get("bindings", [])
        lines = [f"{len(rows)} bindings (a name is a hint, never authority — each still needs a Capability):"]
        for b in rows:
            lines.append(
                f"  {b.get('name',''):<20} {b.get('kind',''):<8} -> {mask(str(b.get('target','')))}  "
                f"{b.get('description','')}".rstrip()
            )
        _say(c, "\n".join(lines))

    ctx.request(naming, "name.list", {}, (cap,), then=listed)


def _list_objects(ctx: UnitContext) -> None:
    store = _services(ctx).get("object_store")
    cap = _audit_cap(ctx)
    if store is None or cap is None:
        return

    def listed(c: UnitContext, reply: Message) -> None:
        p = reply.payload or {}
        if reply.verb != "object.enumerate":
            _say(c, f"the store refused: {p.get('reason', reply.verb)}")
            return
        rows = p.get("objects", [])
        lines = [f"{len(rows)} Objects. There is no way to reach any of these by guessing its id:"]
        for o in rows:
            lines.append(
                f"  {mask(o['object_id']):<20} {o['kind']:<10} versions {o['versions']:>3}  "
                f"latest {o['latest_seq']:>3}  preferred {o['preferred']}  pins {o['pins']}"
            )
        _say(c, "\n".join(lines))

    ctx.request(store, "object.enumerate", {}, (cap,), then=listed)


def _inspect_core(ctx: UnitContext, what: str, rest: list[str]) -> None:
    cap = _audit_cap(ctx)
    if cap is None:
        return
    payload: dict[str, Any] = {"what": what}
    if what == "audit":
        payload["limit"] = int(rest[0]) if rest and rest[0].isdigit() else 20

    def shown(c: UnitContext, reply: Message) -> None:
        p = reply.payload or {}
        if reply.verb != "inspect.result":
            _say(c, f"the Nucleus refused inspection: {p.get('reason', p)}")
            return
        _show_inspection(c, what, p)

    ctx.request(NUCLEUS, "inspect", payload, (cap,), then=shown)


def _inspect_unit(ctx: UnitContext, what: str, rest: list[str]) -> None:
    cap = _audit_cap(ctx)
    if cap is None:
        return

    service_name = "watcher" if what == "watcher" else "improver"
    service = _services(ctx).get(service_name)
    if service is None:
        _say(ctx, f"No {what} Unit is running, so there is nothing to inspect.")
        return

    verb = "watcher.report" if what == "watcher" else "improve.report"
    limit = int(rest[0]) if rest and rest[0].isdigit() else 20

    def shown(c: UnitContext, reply: Message) -> None:
        p = reply.payload or {}
        if reply.verb.endswith(".denied"):
            _say(c, f"{what} denied the report: {p.get('reason', p)}")
            return
        if reply.verb != f"{verb}.result":
            _say(c, f"{what} report failed: {reply.verb} {p}")
            return

        lines = [f"Origin {what}"]
        if what == "watcher":
            events = p.get("events", [])
            escalations = p.get("escalations", {})
            lines.append(f"  {len(events)} events")
            if escalations:
                lines.append(f"  escalations: {escalations}")
            for entry in events[-limit:]:
                detail = entry.get("detail")
                detail_text = _render(detail) if detail is not None else ""
                lines.append(f"  step {entry.get('at_step', 0):>4}  {entry.get('event', 'event'):<20} {detail_text}".rstrip())
        else:
            decisions = p.get("decisions", [])
            attempts = p.get("attempts", {})
            lines.append(f"  {len(decisions)} decisions")
            if attempts:
                lines.append(f"  attempts: {attempts}")
            for entry in decisions[-limit:]:
                reason = entry.get("reason")
                lines.append(
                    f"  step {entry.get('at_step', 0):>4}  {entry.get('event', 'event'):<16} {entry.get('kind', '')} "
                    f"{entry.get('unit_id', '')} {reason or ''}".rstrip()
                )
        _say(c, "\n".join(lines))

    ctx.request(service, verb, {"limit": limit}, (cap,), then=shown)


def _status(ctx: UnitContext) -> None:
    cap = _audit_cap(ctx)
    if cap is None:
        return

    def units_done(c: UnitContext, reply: Message) -> None:
        p = reply.payload or {}
        if reply.verb != "inspect.result":
            _say(c, f"the Nucleus refused status: {p.get('reason', p)}")
            return

        rows = p.get("units", [])
        focus = c.mem.get("focus", {})
        focus_name = focus.get("name") or focus.get("object_id") or focus.get("recipient_name") or "none"
        storage_path = c.mem.get("params", {}).get("storage_path")
        temp_snapshot = False
        if storage_path:
            temp_snapshot = __import__("pathlib").Path(storage_path).with_suffix(f"{__import__('pathlib').Path(storage_path).suffix}.tmp").exists()
        agent_count = sum(1 for row in rows if row.get("kind") == "agent")
        lines = [
            "Origin status",
            f"  step: {c.step}",
            f"  units: {len(rows)}",
            f"  agents: {agent_count}",
            f"  focus: {focus_name}",
            f"  storage: {storage_path or 'memory'}",
            f"  temp snapshot: {'present' if temp_snapshot else 'clean'}",
        ]

        store = _services(c).get("object_store")
        if store is None:
            _say(c, "\n".join(lines))
            return

        def objects_done(c2: UnitContext, reply2: Message) -> None:
            p2 = reply2.payload or {}
            if reply2.verb != "object.enumerate":
                _say(c2, f"the Object store refused status: {p2.get('reason', p2)}")
                return
            objects = p2.get("objects", [])
            compacted = sum(len(obj.get("compacted", [])) for obj in objects)
            pinned = sum(len(obj.get("pins", [])) for obj in objects)
            durable = sum(obj.get("versions", 0) for obj in objects)
            lines2 = lines + [
                f"  objects: {len(objects)}",
                f"  durable versions: {durable}",
                f"  compacted versions: {compacted}",
                f"  pinned versions: {pinned}",
            ]
            _say(c2, "\n".join(lines2))

        c.request(store, "object.enumerate", {}, (cap,), then=objects_done)

    ctx.request(NUCLEUS, "inspect", {"what": "units"}, (cap,), then=units_done)


def _log(ctx: UnitContext, limit: int) -> None:
    entries = list(ctx.mem.get("log", []))[-max(1, min(limit, 100)):]
    if not entries:
        _say(ctx, "Console log is empty.")
        return
    lines = [f"Console log: {len(entries)} recent events"]
    for entry in entries:
        step = entry.get("at_step", "-")
        text = str(entry.get("text", entry.get("event", ""))).strip()
        lines.append(f"  step {step}  {text}")
    _say(ctx, "\n".join(lines))


def _show_inspection(ctx: UnitContext, what: str, p: dict) -> None:
    if what == "units":
        rows = p.get("units", [])
        lines = [f"{len(rows)} Units:"]
        for u in sorted(rows, key=lambda r: r.get("born_step", 0)):
            lines.append(
                f"  {u.get('name',''):<12} {u.get('kind',''):<12} {u.get('state',''):<8} "
                f"born {u.get('born_step',0):>3}  crashes {u.get('crashes',0)}  "
                f"preempted {u.get('preemptions',0)}  caps {u.get('caps_held',0)}  "
                f"inbox {u.get('inbox_depth',0)}  {mask(str(u.get('unit_id','')))}"
            )
        _say(ctx, "\n".join(lines))
        return

    if what == "agents":
        rows = p.get("agents", [])
        lines = [f"{len(rows)} Agents:"]
        for u in sorted(rows, key=lambda r: r.get("born_step", 0)):
            lines.append(
                f"  {u.get('name',''):<12} {u.get('state',''):<8} "
                f"started {u.get('started_at', '-') if u.get('started_at') is not None else '-'}  "
                f"heartbeat {u.get('last_heartbeat', '-') if u.get('last_heartbeat') is not None else '-'}  "
                f"{mask(str(u.get('unit_id','')))}"
            )
        _say(ctx, "\n".join(lines))
        return

    if what == "caps":
        rows = p.get("capabilities", [])
        lines = [f"{len(rows)} Capabilities minted so far. Handles are inert; this is all anyone can see:"]
        for r in rows:
            holders = ", ".join(mask(str(h)) for h in r.get("holders", [])) or "-"
            expiry = r.get("expires_at_step")
            lines.append(
                f"  {r.get('id',''):<16} {'+'.join(r.get('rights', [])):<22} "
                f"target {mask(str(r['target'])) if r.get('target') else '(namespace)':<16} "
                f"from {r.get('grantor','')}  to {holders}  "
                f"{'expires ' + str(expiry) if expiry is not None else 'permanent'}"
                f"{'  REVOKED by ' + str(r.get('revoked_by')) if r.get('revoked') else ''}"
                f"  {r.get('label','')}".rstrip()
            )
        _say(ctx, "\n".join(lines))
        return

    if what == "audit":
        rows = p.get("audit", [])
        lines = [f"the last {len(rows)} core events:"]
        for e in rows:
            detail = {k: v for k, v in e.items() if k not in ("step", "kind")}
            lines.append(f"  step {e.get('step',0):>4}  {e.get('kind',''):<22} {_render(detail)}")
        _say(ctx, "\n".join(lines))
        return

    if what == "messages":
        rows = p.get("messages", [])
        lines = [f"Origin messages: {len(rows)} routed messages"]
        for e in rows:
            sender = e.get("sender", "")
            recipient = e.get("recipient", "")
            verb = e.get("verb", "")
            caps = len(e.get("caps", []) or [])
            lines.append(
                f"  step {e.get('step',0):>4}  sender={sender} recipient={recipient} verb={verb} caps={caps}"
            )
        _say(ctx, "\n".join(lines))
        return

    if what == "powers":
        lines = ["The Nucleus's own account of its powers (section 2, invariant 1):"]
        lines += [f"  {n}" for n in p.get("public_methods", [])]
        lines.append(f"  public methods not mapped to a constitutional power: {p.get('unmapped') or 'none'}")
        lines.append(f"  powers claimed outside the Constitution: {p.get('powers_outside_constitution') or 'none'}")
        lines.append(f"  still tiny: {p.get('tiny')}")
        _say(ctx, "\n".join(lines))


# ---------------------------------------------------------------------------
# Name and Unit lookup
# ---------------------------------------------------------------------------
def _resolve(
    ctx: UnitContext, phrase: str, kind: str, then: Callable[[UnitContext, str, str], None]
) -> None:
    """Ask Naming what a phrase refers to. Never guesses."""
    naming = _services(ctx).get("naming")
    cap = ctx.hold(Right.RESOLVE)
    if naming is None or cap is None:
        _say(ctx, "I cannot resolve names: no Naming Unit is running, or I hold no RESOLVE Capability.")
        return

    def resolved(c: UnitContext, reply: Message) -> None:
        p = reply.payload or {}
        if reply.verb != "name.resolved":
            _say(c, f"Naming refused: {p.get('reason', reply.verb)}")
            return
        if p.get("target"):
            then(c, p["target"], phrase)
            return
        cands = p.get("candidates") or []
        if not cands:
            _say(c, f"Nothing in the namespace matches {phrase!r}.")
            return
        lines = [f"I am not sure what {phrase!r} refers to:"]
        lines += [f"  {n}. {x['name']}  ({x['kind']}, score {x['score']})" for n, x in enumerate(cands, 1)]
        lines.append("Try one of those names exactly.")
        _say(c, "\n".join(lines))

    ctx.request(naming, "name.resolve", {"phrase": phrase, "kind": kind}, (cap,), then=resolved)


def _focus_on(ctx: UnitContext, target: str, name: str) -> None:
    _set_focus(ctx, target, name)
    _say(ctx, f"'this' and 'it' now refer to {name} ({mask(target)}).")


def _with_unit(ctx: UnitContext, name: str, then: Callable[[UnitContext, dict], None]) -> None:
    """Find a Unit by exact name or by id prefix. Costs AUDIT, which is standing."""
    cap = _audit_cap(ctx)
    if cap is None:
        return

    def found(c: UnitContext, reply: Message) -> None:
        p = reply.payload or {}
        if reply.verb != "inspect.result":
            _say(c, f"the Nucleus would not list Units: {p.get('reason', p)}")
            return
        units = p.get("units", [])
        hits = [
            u
            for u in units
            if u.get("name") == name or u.get("unit_id") == name or str(u.get("unit_id", "")).startswith(name)
        ]
        if not hits:
            _say(c, f"No Unit is named or identified by {name!r}. Try /units.")
            return
        if len(hits) > 1:
            _say(c, f"{name!r} matches {len(hits)} Units: " + ", ".join(u.get("name", "?") for u in hits))
            return
        then(c, hits[0])

    ctx.request(NUCLEUS, "inspect", {"what": "units"}, (cap,), then=found)


# ---------------------------------------------------------------------------
# Proposals that cost new authority
# ---------------------------------------------------------------------------
def _propose_read(ctx: UnitContext, target: str, name: str, cmd: str) -> None:
    right = Right.HISTORY if cmd == "history" else Right.READ
    _propose(
        ctx,
        f"{cmd} {name}",
        [f"{right.value.upper()} on {mask(target)}, granted to me, expires in {_lifetime(ctx)} steps"],
        {"op": "read" if cmd == "show" else "history", "target": target, "name": name},
    )


def _propose_core(ctx: UnitContext, unit: dict, verb: str) -> None:
    right = Right.FREEZE if verb == "freeze" else Right.KILL
    done = (
        "is now frozen — it keeps its state and Capabilities but will not run"
        if verb == "freeze"
        else "has been killed — its arena is released and it is dropped from every Capability"
    )
    _propose(
        ctx,
        f"{verb} {unit.get('name')} ({unit.get('kind')}, currently {unit.get('state')})",
        [f"{right.value.upper()} on {mask(str(unit['unit_id']))}, granted to me, expires in {_lifetime(ctx)} steps"],
        {
            "op": "core",
            "verb": verb,
            "right": right.value,
            "name": unit.get("name", ""),
            "target": unit["unit_id"],
            "payload": {"unit_id": unit["unit_id"], "reason": f"{verb} by the human through the Console"},
            "done": done,
        },
    )


def _propose_rollback(ctx: UnitContext, unit: dict, rest: list[str]) -> None:
    """Reversibility is a constitutional property, not a courtesy.

    Every improvement appends a version and moves the `preferred` pointer;
    rolling back is moving it back. Nothing is ever overwritten, so there is
    always something to return to.
    """
    name = unit.get("name", "")
    if unit.get("kind") in PROTECTED_KINDS:
        _say(ctx, f"{name} is kind {unit.get('kind')}, which is protected. No Improver may rewrite it.")
        return
    if not unit.get("code_object_id"):
        _say(ctx, f"{name} has no versioned code Object, so there is no earlier version to prefer.")
        return
    if unit.get("state") != "frozen":
        _say(
            ctx,
            f"{name} is {unit.get('state')}, not frozen. A replacement can only inherit Capabilities from a "
            f"contained predecessor of the same kind, so /freeze it first — that keeps the rollback safe.",
        )
        return
    seq = int(rest[0]) if rest and rest[0].isdigit() else max(0, int(unit.get("code_seq") or 1) - 1)
    _propose(
        ctx,
        f"roll {name} back to code seq {seq} (it is at {unit.get('code_seq')}) and restart it",
        [
            "no new authority — the Improver appends and spawns with what it already holds",
            f"the frozen {name} hands its Capabilities to its replacement, and the Watcher retires it",
        ],
        {"op": "rollback", "unit": unit, "seq": seq},
    )


def _grant_flow(ctx: UnitContext, rest: list[str]) -> None:
    rights_arg, target_arg, holder_arg = rest[0], rest[1], rest[2]
    steps = int(rest[3]) if len(rest) > 3 and rest[3].isdigit() else _lifetime(ctx)
    try:
        rights = [Right(r.strip().lower()) for r in rights_arg.split(",") if r.strip()]
    except ValueError:
        _say(ctx, f"{rights_arg!r} is not a right. Choices: {', '.join(r.value for r in Right)}.")
        return
    if not rights:
        _say(ctx, "Name at least one right to grant.")
        return
    reserved = sorted(r.value for r in rights if r in RESERVED_RIGHTS)
    if reserved:
        _say(
            ctx,
            f"{', '.join(reserved)} are reserved to the human principal. No Unit can delegate them, "
            f"including this one — attenuation only ever runs downward.",
        )
        return
    names = "+".join(r.value.upper() for r in rights)

    def with_holder(c: UnitContext, holder_id: str, holder_name: str) -> None:
        def propose_at(c2: UnitContext, target: Optional[str], label: str) -> None:
            _propose(
                c2,
                f"grant {names} on {label} to {holder_name} for {steps} steps",
                [f"GRANT on {mask(target) if target else 'the namespace'}, spent now"],
                {
                    "op": "grant",
                    "rights": [r.value for r in rights],
                    "target": target,
                    "holder": holder_id,
                    "expires_in": steps,
                    "label": "delegated by the human through the Console",
                },
            )

        if target_arg == "-":
            propose_at(c, None, "the whole namespace")
        else:
            _resolve(c, target_arg, "object", lambda c3, target, name: propose_at(c3, target, name))

    if holder_arg == "-":
        with_holder(ctx, ctx.id, "me")
    else:
        _with_unit(ctx, holder_arg, lambda c, unit: with_holder(c, unit["unit_id"], unit.get("name", "")))


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _execute(ctx: UnitContext, action: dict) -> None:
    op = action["op"]
    if op == "intent":
        _run_intent(ctx, action["plan"], action.get("i", 0), [])
    elif op == "core":
        _run_core(ctx, action)
    elif op == "spawn":
        _run_spawn(ctx, action)
    elif op in ("read", "history"):
        _run_read(ctx, action, history=op == "history")
    elif op == "grant":
        _run_grant(ctx, action)
    elif op == "rollback":
        _run_rollback(ctx, action)
    else:
        _say(ctx, f"I do not know how to perform {op!r}.")


def _run_core(ctx: UnitContext, action: dict) -> None:
    right = Right(action["right"])

    def armed(c: UnitContext, cap: Capability) -> None:
        c.request(
            NUCLEUS,
            action["verb"],
            action["payload"],
            (cap,),
            then=lambda c2, reply: _core_result(c2, reply, action),
        )

    _mint(ctx, (right,), action["target"], f"console: {action['verb']} {action['name']}", ctx.id, armed)


def _run_spawn(ctx: UnitContext, action: dict) -> None:
    def armed(c: UnitContext, cap: Capability) -> None:
        c.request(
            NUCLEUS,
            "spawn",
            {
                "kind": action["kind"],
                "name": action["name"],
                "entry": action["entry"],
                "params": action.get("params", {}),
            },
            (cap,),
            then=lambda c2, reply: _core_result(c2, reply, {**action, "verb": "spawn", "right": "spawn", "target": None, "name": action["name"], "done": "is now running"}),
        )

    _mint(ctx, (Right.SPAWN,), None, f"console: spawn {action['name']}", ctx.id, armed)


def _endow_reply_channel(ctx: UnitContext, unit_id: str, name: str) -> None:
    """Hand a freshly spawned Unit a scoped SEND token back to the Console.

    A Unit born at runtime holds nothing. The first time it answers a
    request/reply Message — `/agent <name> start`, say — its `respond` presents
    no SEND Capability reaching the Console, the core refuses to route it, and the
    Unit is contained for capability abuse: spawning it and then asking it a
    question would kill it. The Console already holds namespace SEND, so minting a
    SEND scoped to *itself* and handing it to the newborn only attenuates
    authority the Console already has. It is granted through GRANT, named in the
    audit trail, and revocable by the Guardian — the same standing reply channel
    bootstrap gives the demo Units ("{kind}: SEND -> console"). Permanent because
    it is infrastructure for the Unit's whole life, not intent-time authority for
    one action; a reply channel that expired would strand the Unit mid-lifecycle.
    """
    _mint(
        ctx,
        (Right.SEND,),
        ctx.id,
        f"console: reply channel for {name}",
        unit_id,
        lambda c, _cap: _say(c, f"{name} can now answer me (SEND scoped to the console)."),
        permanent=True,
    )


def _core_result(ctx: UnitContext, reply: Message, action: dict) -> None:
    p = reply.payload or {}
    if reply.verb != f"{action['verb']}.result":
        _say(ctx, f"REFUSED: the Nucleus would not {action['verb']} {action['name']} — {p.get('reason', p)}")
        return
    if action.get("verb") == "spawn":
        unit_id = p.get("unit_id")
        name = p.get("name")
        if unit_id and name:
            _set_focus(ctx, unit_id, name)
            _say(ctx, f"'this' and 'it' now refer to {name} ({mask(unit_id)}).")
            _endow_reply_channel(ctx, unit_id, name)
    _say(ctx, f"{action['name']} {action['done']}.")


def _run_read(ctx: UnitContext, action: dict, history: bool) -> None:
    store = _services(ctx).get("object_store")
    if store is None:
        _say(ctx, "No Object store is running.")
        return
    right = Right.HISTORY if history else Right.READ
    verb = "object.history" if history else "object.read"

    def armed(c: UnitContext, cap: Capability) -> None:
        c.request(
            store,
            verb,
            {"object_id": action["target"]},
            (cap,),
            then=lambda c2, reply: _show_object(c2, reply, action, history),
        )

    _mint(ctx, (right,), action["target"], f"console: {verb}", ctx.id, armed)


def _show_object(ctx: UnitContext, reply: Message, action: dict, history: bool) -> None:
    p = reply.payload or {}
    expected = "object.history" if history else "object.read"
    if reply.verb != expected:
        _say(ctx, f"REFUSED by the store: {p.get('reason', p)}")
        return
    if history:
        rows = p.get("versions", [])
        lines = [f"{action['name']} — {len(rows)} versions. Nothing here was ever overwritten:"]
        for v in rows:
            flags = (" pinned" if v.get("pinned") else "") + (" compacted" if v.get("compacted") else "")
            lines.append(
                f"  seq {v.get('seq',0):>3}  step {v.get('step',0):>4}  by {mask(str(v.get('author','')))}"
                f"  {v.get('note','')}{flags}".rstrip()
            )
        _say(ctx, "\n".join(lines))
        return
    v = p.get("version", {})
    _set_focus(ctx, action["target"], action["name"])
    _say(
        ctx,
        f"{action['name']} @ seq {v.get('seq')} (step {v.get('step')}, by {mask(str(v.get('author','')))}, "
        f"note: {v.get('note') or '-'}):\n  {_render(v.get('payload'))}",
    )


def _run_grant(ctx: UnitContext, action: dict) -> None:
    grant = ctx.hold(Right.GRANT, action["target"])
    if grant is None:
        _say(ctx, "I hold no GRANT authority for that target. Only the Guardian can delegate here.")
        return

    def done(c: UnitContext, reply: Message) -> None:
        if reply.verb != "mint.result":
            _say(c, f"REFUSED: {(reply.payload or {}).get('reason', reply.payload)}")
            return
        _say(
            c,
            f"granted {reply.payload['cap']} to {mask(str(action['holder']))} — "
            f"expires in {action['expires_in']} steps, revocable by the Guardian at any time.",
        )

    ctx.request(
        NUCLEUS,
        "mint",
        {
            "rights": action["rights"],
            "target": action["target"],
            "holder": action["holder"],
            "expires_in": action["expires_in"],
            "label": action["label"],
        },
        (grant,),
        then=done,
    )


def _run_rollback(ctx: UnitContext, action: dict) -> None:
    improver = _services(ctx).get("improver")
    unit = action["unit"]
    if improver is None:
        _say(ctx, "No Improver is running, so there is nothing to ask.")
        return
    # No Capability attached: the Improver acts with what it already holds, and
    # the Console cannot hand it anything it does not have.
    ctx.request(
        improver,
        "improve.rollback",
        {
            "unit_id": unit["unit_id"],
            "name": unit.get("name"),
            "kind": unit.get("kind"),
            "code_object_id": unit.get("code_object_id"),
            "seq": action["seq"],
        },
        (),
        then=lambda c, reply: _say(
            c,
            f"rollback {'succeeded' if reply.verb == 'improve.done' else 'failed'}: {_render(reply.payload)}",
        ),
    )


def _work(ctx: UnitContext, n: int) -> None:
    """Poke the fault-injected Unit so self-healing has something to heal.

    Costs no new authority: the Unit already holds APPEND on its own data Object
    from birth. What it exercises is section 7 end to end — the Nucleus detects
    and contains, the Watcher escalates, the Improver appends a fixed version and
    restarts, and the human watches the whole thing in the audit trail.
    """
    flaky = _services(ctx).get("flaky")
    if flaky is None:
        _say(ctx, "No Unit of kind 'flaky' is running. It may have been replaced already — try /units.")
        return
    for _ in range(max(1, min(n, 20))):
        ctx.send(flaky, "work", {"object_id": ctx.mem["params"].get("work_object")})
    _say(ctx, f"sent {max(1, min(n, 20))} work request(s) to {mask(flaky)}.")


# ---------------------------------------------------------------------------
# Natural-language intent: the full section 6 loop
# ---------------------------------------------------------------------------
def _express(ctx: UnitContext, text: str) -> None:
    """Loop step 1: express. The Console understands nothing itself; it forwards
    the words to Naming, which is an ordinary Unit and can be wrong and fixed."""
    naming = _services(ctx).get("naming")
    cap = ctx.hold(Right.RESOLVE)
    if naming is None or cap is None:
        _say(ctx, "I cannot resolve intent: no Naming Unit is running, or I hold no RESOLVE Capability.")
        return
    ctx.request(
        naming,
        "intent.parse",
        {"text": text, "focus": dict(ctx.mem["focus"])},
        (cap,),
        then=lambda c, reply: _planned(c, reply),
    )


def _planned(ctx: UnitContext, reply: Message) -> None:
    """Loop step 2 and 3: resolve, then propose the authority the answer costs."""
    if reply.verb != "intent.plan":
        _say(ctx, f"Naming could not parse that: {(reply.payload or {}).get('reason', reply.verb)}")
        return
    plan = reply.payload or {}
    if plan.get("unrecognised"):
        _say(ctx, "I did not recognise: " + "; ".join(f"{u!r}" for u in plan["unrecognised"]) + ". Type /help.")
    if not plan.get("steps"):
        if not plan.get("unrecognised"):
            _say(ctx, "I could not find an action in that. Type /help for the verbs I understand.")
        return
    if plan.get("ambiguities"):
        _propose_choice(ctx, plan, plan["ambiguities"][0])
        return
    services = _services(ctx)
    for step in plan["steps"]:
        kind = step.get("unit_kind")
        if kind and kind not in services:
            _say(ctx, f"note: nothing of kind {kind!r} is running, so that step will fail.")
    _propose(ctx, _summarise(plan), _cost_of(ctx, plan), {"op": "intent", "plan": plan, "i": 0})


def _summarise(plan: dict) -> str:
    parts = []
    for s in plan["steps"]:
        bits = [s.get("action", "?")]
        if s.get("target_phrase"):
            bits.append(s["target_phrase"])
        if s.get("modifier") is not None:
            bits.append(f"by {s['modifier']:g}")
        if s.get("recipient_phrase"):
            bits.append(f"to {s['recipient_phrase']}")
        parts.append(" ".join(bits))
    return "; ".join(parts) or plan.get("text", "")


def _cost_of(ctx: UnitContext, plan: dict) -> list[str]:
    """The authority this intent will cost, in words. Shown before confirmation
    because a proposal the human cannot read is not a proposal."""
    lifetime = _lifetime(ctx)
    cost = []
    for s in plan["steps"]:
        rights = "+".join(r.upper() for r in s.get("rights", []))
        who = "me" if not s.get("unit_kind") else f"the {s['unit_kind']} Unit"
        cost.append(
            f"{rights} on {s.get('target_phrase') or 'the target'}, granted to {who}, "
            f"expires in {lifetime} steps"
        )
        if s.get("recipient"):
            cost.append(
                f"APPEND on the mailbox for {s.get('recipient_phrase')}, granted to {who}, "
                f"expires in {lifetime} steps"
            )
    return cost


def _grants_for(step: dict) -> list[dict]:
    """Every token one intent step needs.

    A step can need more than one: mailing a photo appends to the recipient's
    mailbox *and* reads the attachment, and those are two different Objects, so
    they are two different tokens. Scope follows the target, never the verb.
    """
    rights = tuple(Right(r) for r in step.get("rights", ()))
    grants = [{"rights": rights, "target": step.get("target"), "label": f"intent: {step.get('action')}"}]
    if step.get("recipient"):
        grants.append(
            {"rights": (Right.APPEND,), "target": step["recipient"], "label": "intent: deliver to mailbox"}
        )
    return grants


def _run_intent(ctx: UnitContext, plan: dict, i: int, results: list[dict]) -> None:
    """Loop steps 4 and 5: execute, one step at a time, authority first."""
    steps = plan.get("steps", [])
    if i >= len(steps):
        _intent_report(ctx, results)
        return
    step = steps[i]
    kind = step.get("unit_kind")

    if not kind:
        # `show` is the Console reading for itself, under a token minted for this
        # one ask. It holds no standing READ on anything.
        _mint(
            ctx,
            (Right.READ,),
            step.get("target"),
            "intent: show",
            ctx.id,
            lambda c, cap: _show_intent(c, cap, step, plan, i, results),
        )
        return

    actor = _services(ctx).get(kind)
    if actor is None:
        results.append({"step": step, "error": f"no Unit of kind {kind!r} is running"})
        _run_intent(ctx, plan, i + 1, results)
        return
    _mint_all(
        ctx,
        _grants_for(step),
        actor,
        lambda c: _act(c, step, plan, i, results, actor),
    )


def _show_intent(
    ctx: UnitContext, cap: Capability, step: dict, plan: dict, i: int, results: list[dict]
) -> None:
    store = _services(ctx).get("object_store")
    if store is None:
        results.append({"step": step, "error": "no Object store is running"})
        _run_intent(ctx, plan, i + 1, results)
        return
    ctx.request(
        store,
        "object.read",
        {"object_id": step.get("target")},
        (cap,),
        then=lambda c, reply: _acted(c, reply, step, plan, i, results),
    )


def _act(ctx: UnitContext, step: dict, plan: dict, i: int, results: list[dict], actor: str) -> None:
    """Authority is minted and held by the actor. Now ask it to work.

    Nothing is attached, because attaching only proves possession — the actor
    finds its own freshly minted tokens with `ctx.hold()`. That is the difference
    between presenting a Capability and giving one away.
    """
    payload: dict[str, Any] = {"object_id": step.get("target")}
    modifier = step.get("modifier")
    action = step.get("action")
    if action == "brighten":
        payload["amount"] = int(modifier) if modifier is not None else 10
    elif action == "resize":
        payload["scale"] = float(modifier) if modifier is not None else 0.5
    elif action == "count":
        payload["by"] = int(modifier) if modifier is not None else 1
    if step.get("recipient"):
        payload["mailbox"] = step["recipient"]
        payload["to"] = step.get("recipient_phrase")
        payload["body"] = plan.get("text", "")
    ctx.request(
        actor,
        step["verb"],
        payload,
        (),
        then=lambda c, reply: _acted(c, reply, step, plan, i, results),
    )


def _acted(ctx: UnitContext, reply: Message, step: dict, plan: dict, i: int, results: list[dict]) -> None:
    results.append({"step": step, "reply": reply.verb, "payload": reply.payload})
    # Loop step 6 begins here: the result is inspectable, and it also becomes the
    # context for whatever the human says next.
    if step.get("target"):
        _set_focus(ctx, step["target"], step.get("target_phrase") or "")
    if step.get("recipient"):
        ctx.mem["focus"]["recipient"] = step["recipient"]
        ctx.mem["focus"]["recipient_name"] = step.get("recipient_phrase") or ""
    _run_intent(ctx, plan, i + 1, results)


def _intent_report(ctx: UnitContext, results: list[dict]) -> None:
    """Loop step 6: an inspectable result. Ids, sequence numbers and before/after
    values — never just "done"."""
    if not results:
        _say(ctx, "Nothing to do.")
        return
    lines = ["RESULT:"]
    for r in results:
        step = r["step"]
        head = f"  {step.get('action')}"
        if r.get("error"):
            lines.append(f"{head}: FAILED — {r['error']}")
            continue
        verb = r.get("reply", "")
        p = r.get("payload") or {}
        if verb == "photo.done":
            lines.append(f"{head}: seq {p.get('previous_seq')} -> {p.get('new_seq')} ({p.get('note')})")
            lines.append(f"      before {_render(p.get('before'))}")
            lines.append(f"      after  {_render(p.get('after'))}")
        elif verb == "object.read":
            v = p.get("version", {})
            lines.append(f"{head}: seq {v.get('seq')} — {_render(v.get('payload'))}")
        elif verb == "mail.sent":
            lines.append(f"{head}: delivered to {p.get('to')} at mailbox seq {p.get('seq')}")
        elif verb == "counter.done":
            lines.append(f"{head}: count is now {p.get('count')} (seq {p.get('seq')})")
        elif verb == "object.appended":
            lines.append(f"{head}: appended seq {p.get('seq')}")
        elif verb.endswith((".denied", ".error", ".rejected", ".compacted_away")):
            lines.append(f"{head}: REFUSED — {p.get('reason', p)}")
        else:
            lines.append(f"{head}: {verb} {_render(p)}")
    _say(ctx, "\n".join(lines))
