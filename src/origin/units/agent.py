"""A minimal agent lifecycle template.

This is intentionally small: it gives a spawned agent a running state, a
heartbeat, and a clean shutdown path. That is enough to model the first real
agent lifecycle without pretending the system has a full runtime scheduler or a
heap of tool access policies yet.
"""

from __future__ import annotations

from ..unit import unit_type


@unit_type("agent")
def agent_handler(ctx, msg):
    """Support a very small lifecycle contract for agent Units."""
    verb = msg.verb

    if verb == "agent.start":
        ctx.mem["state"] = "running"
        ctx.mem["started_at"] = ctx.step
        ctx.mem["last_heartbeat"] = ctx.step
        if msg.reply_to is not None:
            ctx.respond(msg, "agent.started", {"state": "running", "started_at": ctx.mem["started_at"]})
        return

    if verb == "agent.heartbeat":
        ctx.mem["state"] = "running"
        ctx.mem["last_heartbeat"] = ctx.step
        if msg.reply_to is not None:
            ctx.respond(msg, "agent.heartbeat", {"state": "running", "last_heartbeat": ctx.mem["last_heartbeat"]})
        return

    if verb == "agent.stop":
        ctx.mem["state"] = "stopped"
        ctx.mem["stopped_at"] = ctx.step
        if msg.reply_to is not None:
            ctx.respond(msg, "agent.stopped", {"state": "stopped", "stopped_at": ctx.mem["stopped_at"]})
        return

    if verb == "agent.status":
        payload = {
            "state": ctx.mem.get("state", "born"),
            "started_at": ctx.mem.get("started_at"),
            "last_heartbeat": ctx.mem.get("last_heartbeat"),
            "stopped_at": ctx.mem.get("stopped_at"),
        }
        if msg.reply_to is not None:
            ctx.respond(msg, "agent.status", payload)
        return

    if msg.reply_to is not None:
        ctx.respond(msg, "agent.error", {"reason": f"unknown verb {verb!r}"})
