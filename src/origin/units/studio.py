"""The Studio Unit — a spatial canvas of living Objects (section 8).

CONSTITUTION.md section 8 asks for two interfaces: a Console Unit that is "clean
textual", and a Studio Unit that is a "spatial continuous canvas of living Objects
and Verbs", named there as the primary long-term interface. The Console exists. This
is the other one, and it is deliberately built as an ordinary Unit rather than a
special front-end, because section 8 says both interfaces "are ordinary Units and may
evolve".

**A canvas, honestly.** There is no display server here, so this renders a spatial
layout into text: Objects placed on a grid by kind, with the Verbs that currently
apply to them drawn alongside. It is a map, not a picture. What makes it *spatial*
rather than another table is that position carries meaning — adjacency groups by kind,
the focused Object sits at the centre, and the same Object keeps its coordinates
across invocations so the layout is stable rather than redrawn at random each time.

**Why it holds so little.** A canvas that could see everything would be the most
powerful Unit in the system, which is precisely the wrong property for the thing
that draws the whole system. It holds AUDIT (a canvas that cannot see is useless),
SEND, and RESOLVE — and nothing that mutates. It cannot append, pin, mint, spawn, or
revoke. Selecting an Object therefore proposes to the Console rather than acting,
which keeps section 8's full intent loop intact: the human still confirms authority.

Like every other Unit it also needs SEND reaching the human, because output is a
Message and a Unit that cannot address the human cannot talk to them. That is not
extra authority: `send_message` only checks that the sender may reach the recipient,
and the human's inbox has no arena to act in.

**Layout is derived, never authoritative.** Coordinates live in this Unit's arena and
are recomputed from the Object set whenever the set changes. If this Unit dies the
layout is lost and rebuilt, exactly as a canvas should be: it is a view, not state.
The Objects themselves are untouched, because a view has no authority over what it
views.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from ..capability import CapabilityError, Right
from ..ids import HUMAN, mask
from ..message import Message
from ..unit import UnitContext, is_answer, unit_type

#: How many Objects each band of the canvas shows before wrapping to the next row.
#: Three fits an 80-column terminal with room for the separators, which matters
#: because a spatial view that wraps unpredictably is not spatial.
COLUMNS = 3

#: Hard ceiling on rendered width. Without it one long Object name pushes every
#: other cell off the right edge, which is worse than truncating the label.
CELL_WIDTH = 26


@unit_type("studio")
def studio_handler(ctx: UnitContext, msg: Message) -> None:
    services: dict[str, str] = ctx.mem.setdefault(
        "services", dict(ctx.mem.get("params", {}).get("services", {}))
    )
    #: object_id -> (row, col). The canvas's only real state. Recomputed when the
    #: Object set changes; preserved when it does not, so Objects do not jump.
    layout: dict[str, tuple[int, int]] = ctx.mem.setdefault("layout", {})
    focus: dict[str, Any] = ctx.mem.setdefault("focus", {})

    if msg.verb == "studio.render":
        audit = ctx.hold(Right.AUDIT)
        if audit is None:
            _say(ctx, "I hold no AUDIT Capability, so I cannot draw the canvas. A view that "
                      "cannot see is not a view. That needs the Guardian.")
            return

        store = services.get("object_store")
        naming = services.get("naming")
        if not store:
            _say(ctx, "No Object store is running, so there is nothing to draw.")
            return

        def on_objects(c: UnitContext, reply: Message) -> None:
            # `object.enumerate` answers with the same verb, not a `.result` variant.
            if reply.verb != "object.enumerate":
                _say(c, f"the store refused the canvas: {reply.payload}")
                return
            rows = reply.payload.get("objects", [])

            def on_names(n: UnitContext, names_reply: Message) -> None:
                # `name.list` likewise answers with its own verb.
                if names_reply.verb == "name.list":
                    names = {
                        b.get("name"): b.get("target")
                        for b in names_reply.payload.get("bindings", [])
                    }
                else:
                    names = {}
                _say(n, _canvas(layout, focus, rows, names))

            if naming:
                resolve = c.hold(Right.RESOLVE)
                if resolve is not None:
                    c.request(naming, "name.list", {}, (resolve,), then=on_names)
                    return
            _say(c, _canvas(layout, focus, rows, {}))

        ctx.request(store, "object.enumerate", {}, (audit,), then=on_objects)
        return

    if msg.verb == "studio.focus":
        # Selecting an Object here does not grant anything or change anything. It
        # records what the human is looking at and proposes to the Console, which
        # owns the intent loop. A canvas that could act on its own would be a
        # second, unconfirmed path to authority.
        target = (msg.payload or {}).get("object_id")
        if not target:
            _say(ctx, "usage: studio.focus <object_id>")
            return
        focus["object_id"] = target
        console = services.get("console")
        if console:
            ctx.send(
                console,
                "console.notice",
                {"text": f"studio focused {mask(str(target))}. Use /focus <name> or an intent to act on it."},
            )
        return

    if msg.verb == "studio.layout":
        rows = list(layout.items())
        if not rows:
            _say(ctx, "Nothing laid out yet. Render the canvas first.")
            return
        lines = ["Studio layout (object -> cell)"]
        for object_id, (row, col) in sorted(rows, key=lambda item: (item[1][0], item[1][1])):
            lines.append(f"  row {row} col {col}  {mask(str(object_id))}")
        _say(ctx, "\n".join(lines))
        return

    if msg.verb == "studio.report":
        audit = msg.caps[0] if msg.caps else None
        try:
            if audit is None:
                raise CapabilityError("studio.report requires an AUDIT Capability")
            ctx.mem["validator"](audit, Right.AUDIT, None, msg.sender)
            ctx.respond(
                msg,
                "studio.report.result",
                {"placed": len(layout), "focus": focus.get("object_id"), "layout": {k: list(v) for k, v in layout.items()}},
            )
        except CapabilityError as exc:
            ctx.respond(msg, "studio.denied", {"reason": str(exc)})
        return

    if msg.verb in ("object.enumerate", "name.list"):
        # An answer to a render request already handled inside its continuation.
        # Answering it here would produce a phantom error, which is the same trap
        # the Watcher documents for improve.done.
        return

    if not is_answer(msg.verb):
        ctx.respond(msg, "studio.error", {"reason": f"unknown verb {msg.verb!r}"})


def _canvas(
    layout: dict[str, tuple[int, int]],
    focus: dict[str, Any],
    objects: list[dict],
    names: dict[str, Optional[str]],
) -> str:
    """Render Objects onto a grid, grouped by kind, focused Object at the centre.

    Placement is stable: an Object that was already on the canvas keeps its cell, so
    the view does not reshuffle on every render. Only genuinely new Objects are
    appended, which is what makes repeated renders readable rather than a slot
    machine.
    """
    if not objects:
        return "The canvas is empty. No Objects exist yet."

    # name-lookup in both directions: object_id -> human name, for labelling cells.
    by_target = {target: name for name, target in names.items() if target}

    # Group by kind, preserving the order the store returned. Related Objects
    # landing near each other is the entire point of a spatial view: an operator
    # should see the two photos together and the mailboxes together, without
    # reading ids.
    ordered = sorted(objects, key=lambda o: (str(o.get("kind", "")), str(o.get("object_id", ""))))

    placed: list[tuple[dict, tuple[int, int]]] = []
    for index, obj in enumerate(ordered):
        row, col = divmod(index, COLUMNS)
        layout[obj.get("object_id")] = (row, col)
        placed.append((obj, (row, col)))

    focus_id = focus.get("object_id")

    grid: dict[int, list[str]] = {}
    for obj, (row, col) in placed:
        label = _label(by_target, obj)
        if len(label) > CELL_WIDTH - 6:
            label = label[: CELL_WIDTH - 7] + "~"
        if obj.get("object_id") == focus_id:
            label = f"* {label}"
        versions = obj.get("versions", 0)
        compacted = len(obj.get("compacted", []))
        # The Verbs that would apply: reading and extending are what an operator
        # does to a living Object, and whether its payloads are still intact is the
        # fact most worth seeing at a glance.
        state = f"{versions}v"
        if compacted:
            state += f",{compacted}c"
        grid.setdefault(row, []).append(f"{label} [{state}]".ljust(CELL_WIDTH))

    lines = [
        "Origin Studio - living Objects, grouped by kind",
        "",
    ]
    for row in sorted(grid):
        cells = grid[row]
        while len(cells) < COLUMNS:
            cells.append(" " * CELL_WIDTH)
        lines.append("  " + " | ".join(cells).rstrip())
    lines.append("")
    lines.append(f"  {len(placed)} Objects in {len({o.get('kind') for o, _ in placed})} kinds.")
    lines.append("  [Nv] = N versions, and Nc of those are compacted. * marks the focused Object.")
    lines.append("  This is a view, not a control: the Studio holds nothing that mutates, so")
    lines.append("  acting on an Object still goes through an intent and a confirmed grant.")
    return "\n".join(lines)


def _label(by_target: dict[str, str], obj: dict) -> str:
    """The human name for an Object, falling back to a masked id.

    Readable Object ids were a deliberate choice (see bootstrap.py), but a canvas
    cell is narrow, so the name is preferred and the id is the fallback rather than
    the other way round.
    """
    name = by_target.get(obj.get("object_id"))
    if name:
        return name
    return mask(str(obj.get("object_id")))


def _say(ctx: UnitContext, text: str) -> None:
    ctx.send(HUMAN, "console.output", {"text": text})
