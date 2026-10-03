# Vision

Origin is a capability-based operating system for autonomous AI agents.

It is not a conventional app, desktop, or process model. It is a runtime where
authority is explicit, memory is durable, and every action is inspectable. The
system is built around persistent Objects, message-driven Units, and
unforgeable Capabilities. Humans do not hand over ambient power; they grant
limited authority with clear scope, expiry, and auditability.

The central design principle is simple: a system for safe autonomy must make
authority visible, reversible, and recoverable. That means the human remains in
the loop, the system can reason about what an agent is allowed to do, and failed
or misbehaving actors can be contained, replaced, or improved without destroying
the whole runtime.

## Hardware stops being infrastructure

Hardware resources are not hidden global infrastructure. They become
capability-mediated resources: CPU time, memory, sensors, actuators, storage, and
network access are granted purposefully, limited in time and scope, and visible
to the human operator. The result is an environment where software acts with
accountability rather than ambient privilege.

## A different default mental model

A truly new operating system would no longer be organized around "run programs"
and "save files". It would be organized around living Objects, explicit authority,
auditable actions, and continuous continuity.

The loop is: the user expresses intent, the system resolves it, proposes the
authority required, and the human confirms. Then execution proceeds under
explicit control, with history, provenance, and recovery preserved.

## Where each idea lives

- `CONSTITUTION.md` — the rules this vision has to satisfy
- `ARCHITECTURE.md` — how the rules map onto real modules
- `PROGRESS.md` — what is built so far
- `README.md` — how to run it
- `CONTRIBUTING.md` — how to work on it

This is the direction Origin is moving toward: a readable, runnable model of
what a safe AI-native operating environment could be.
