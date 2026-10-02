# Origin Architecture Overview

## 1. System model

Origin is a capability-based operating system simulation. It is designed around a small trusted kernel and a larger set of ordinary runtime units.

The system is shaped by five foundational primitives:

- Objects: persistent, append-only, versioned state
- Units: isolated execution actors
- Capabilities: explicit authority tokens
- Messages: the only communication channel
- The Nucleus: the minimal privileged runtime

## 2. Core principles

The design follows the Constitution directly:

- no ambient authority
- no shared global mutable state
- all lasting state is versioned and durable
- communication happens by message-passing
- units are isolated and replaceable
- self-healing is a normal part of the system
- the human remains a trusted authority via the Guardian

## 3. Layered architecture

### 3.1 Kernel / Nucleus

The Nucleus is intentionally tiny. It enforces lifecycle rules, routing, validation, scheduling, and the authority model. It does not become a giant application layer.

Responsibilities:

- create and validate capabilities
- schedule units
- route messages
- seal the system after bootstrap
- handle the privileged lifecycle operations

The Nucleus is the only trusted core.

### 3.2 Capabilities

Capabilities are how authority is represented.

A unit only acts when it holds a relevant token. There is no implicit access to object state, services, or system actions.

This creates an effective trust boundary:

- the human has a Guardian capability
- services receive scoped rights
- agents hold only what they need
- revocation is explicit and inspectable

### 3.3 Objects and persistence

Origin treats persistence as a first-class system property.

Objects are not files in the usual sense. They are persistent, append-only, versioned entities that hold durable state. The system keeps history and can recover or inspect previous state.

This makes the system suitable for:

- long-lived memory
- immutable event logs
- version recovery
- agent memory and tool state
- consistent inspection and replay

### 3.4 Messages

All communication flows through Messages.

This is a critical design decision. The system acts like a message-passing operating environment rather than a shared-memory app runtime.

This gives Origin several benefits:

- explicit trust boundaries
- easy audit trails
- unit isolation
- recoverable protocol flow
- natural coordination for multiple agents

### 3.5 Units and services

Units are the operating system’s ordinary actors.

Examples in the current project include:

- naming
- console
- object store
- watcher
- improver
- demo worker units

Each unit is reactive, isolated, and service-like. They are not “apps” in the consumer sense; they are system services and runtime actors.

### 3.6 Human control surface

The Console and the REPL act as the human control plane. They are not special-case application code; they are ordinary units with authority and message channels.

This preserves the architecture principle that the human interacts with the system through the same message-driven model as any other actor.

## 4. AI-agent runtime model

An AI agent in Origin is modeled as a Unit with:

- an identity
- a private execution context
- a message inbox
- persistent memory objects
- a capability budget
- a bounded lifecycle

This is the correct agent runtime model because it matches the principles of:

- isolation
- explicit authority
- traceability
- restartability
- safety

## 5. Self-healing model

Origin includes a watchdog-improver model.

When a unit fails or violates invariants:

- the watcher detects the issue
- the system contains the failure
- the improver may propose a replacement or improved version
- the system can restart or restore from history

This is essential for an autonomous runtime, because agents must be recoverable and replaceable without destroying the system.

## 6. Relationship to the future of AI agents

Origin is designed to be a host environment for agentic software, not an end-user app.

The system is aligned with the future direction of AI infrastructure because it gives agents:

- clear permissions
- durable memory
- recoverable execution
- auditability
- safe collaboration
- human oversight

This is much closer to an agent operating system than to a traditional application platform.

## 7. Summary

Origin is a minimal core with a message-driven, capability-scoped runtime. It keeps the trust boundary small while allowing complex behavior to emerge via units and services.

That makes it an ideal substrate for AI agents: a safe, inspectable, recoverable operating environment in which autonomous software can operate without ambient authority.
