# Origin: an AI-Agent Operating System

## One-sentence vision

Origin is a capability-based operating system simulation designed as a safe runtime for autonomous AI agents, where authority is explicit, memory is persistent, and every action is inspectable and recoverable.

## Why this is not just an app

This project is not a conventional application with an AI feature bolted on. It is an operating environment where agents can live as first-class system actors.

The defining properties are:

- Messages are the only communication mechanism.
- Units are isolated execution actors.
- Authority exists only as explicit Capabilities.
- Objects are persistent, versioned, and durable.
- The Nucleus remains tiny and privileged.
- Self-healing and self-improvement are built into the system.
- Human oversight remains possible through clear inspection and revocation flows.

That makes the project a runtime for agentic software, not a consumer app shell.

## Core design idea

The OS is built around a simple rule:

No agent receives ambient authority.

Every action requires a capability, a message, and a valid target. This creates a runtime where agents can coordinate, cooperate, and fail without silently taking over the system.

## Agent model

An AI agent in Origin is a Unit with:

- a unique identity
- a private execution arena
- explicit capability grants
- a message inbox
- access to persistent object memory
- a safe lifecycle: sleep, run, fail, restart, replace

Agents do not share global mutable memory. They interact by sending Messages and operating on Objects for which they hold the required capability.

## Why this is an ideal host for AI agents

### 1. Safe autonomy
Agents can act without ambient authority because capability grants are explicit and bounded.

### 2. Auditable behavior
Every message, capability, object mutation, and system event is inspectable.

### 3. Recovery and replacement
A failed agent can be isolated, contained, restarted, or replaced without taking down the whole system.

### 4. Persistent memory
Objects are durable and versioned, allowing agents to carry memory across time without ad hoc file-system assumptions.

### 5. Human authority remains meaningful
The human can inspect capabilities, grant or revoke authority, and observe system behavior in a clear and deterministic way.

### 6. Multi-agent coordination
Agents communicate by Messages rather than shared state, which is a better substrate for trustable coordination and protocol design.

## What the system looks like in practice

A typical runtime is composed of:

- a tiny privileged kernel
- a naming and discovery service
- an object store
- a console or human control surface
- watcher and improver units
- user agents and worker agents
- message routing and capability enforcement

The core is intentionally small. Most intelligence and behavior lives in ordinary Units.

## Project narrative

Origin is the home for a new class of software: agents that do not live inside a single app, but inside a safe operating environment.

This is closer to a secure agent platform than to a normal user application.

## Short pitch version

Origin is a capability-based operating system for autonomous agents. It gives each agent explicit authority, persistent memory, message-driven coordination, and recovery-safe execution. The result is an environment where AI can operate with accountability, not unchecked ambient power.

## Future direction

The long-term direction is to turn the project into a layered OS runtime that supports:

- agent identity and registration
- capability granting and revocation
- persistent agent memory
- object-backed tool use
- auditing and security review
- human oversight and safe delegation
- self-improving agents constrained by policy

This is the future home for AI agents: a runtime where they can act, learn, fail, adapt, and remain accountable.

## Current engineering posture

Origin is already beyond a toy prototype. The repository now contains:

- a constitution-first Python package layout
- capability-gated kernel and message routing
- durable object history with compaction and retention metadata
- a human console with status, audit, watcher, and improver views
- a green regression suite proving the runtime is stable enough to extend

The system is not "complete" as a real operating system yet, but it is stable enough to move deliberately toward higher-level agent infrastructure without breaking the safety model.

## Agent operating rules

Any future work on Origin should obey these rules:

1. Keep all code inside the package layout. No loose modules in a root directory.
2. Preserve the constitution as the source of truth. If it conflicts with a convenient shortcut, the shortcut loses.
3. Add a failing regression before a fix whenever behavior changes.
4. Keep human visibility first-class. Every agent action must be inspectable.
5. Prefer message-driven execution over ambient authority.
6. Commit checkpoints after stable milestones, not after speculative work.

This is a project for safe autonomy, not for unchecked automation.

## Next 30-day roadmap

### Phase A — harden the runtime substrate (0-10 days)

- tighten persistence and crash recovery edge cases
- add explicit durability audits and rejection paths
- improve object retention policies and operator reporting
- leave the system observable under stress

### Phase B — make agent workflow natural (10-20 days)

- add richer naming and intent handling for real tasks
- support safe delegation patterns for agent tools and resources
- improve the human approval flow for complex actions
- make the console read like a true operator surface, not a debug shell

### Phase C — first real agent lifecycle (20-30 days)

- define a standard agent Unit template with startup, heartbeat, and shutdown
- add recovery/replacement flows for failed agent workers
- model explicit tool access and bounded memory scopes
- expose a simple agent registry and runtime status for live monitoring

## Success criteria for the next milestone

A strong near-term target is:

- a human can start the system, inspect runtime state, spawn an agent, grant a bounded capability, and watch the agent act under policy
- object memory survives restart boundaries with a clear audit trail
- failed agents are contained and can be replaced without global disruption

That is the next credible step from the current state: safe agent execution inside a durable, inspectable platform.
