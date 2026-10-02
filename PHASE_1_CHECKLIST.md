# Phase 1 — Pure Simulation Checklist

This document tracks the Phase 1 steps required by the Constitution and keeps the project moving in the intended order.

## Checklist

- [x] 1.1 Project structure + Constitution file
- [x] 1.2 Capability (unforgeable token)
- [x] 1.3 Message
- [x] 1.4 Object + ObjectStore (append-only versioning)
- [x] 1.5 Unit (basic structure)
- [x] 1.6 Nucleus (minimal core + bootstrap)
- [x] 1.7 Console Unit (first human interface)
- [x] 1.8 Interactive loop and REPL/test path
- [x] 1.9 Basic Naming support
- [x] 1.10 Demo Units + first Improver experiments

## Status

Phase 1 is now complete enough to treat the simulation as a stable operating model. The project has proven the core primitives and the bootstrap flow in a working Python package layout.

## Next step

The project should now move to Phase 2: Persistent Object Substrate.

### Phase 2 focus areas

- real durability guarantees
- stronger crash recovery semantics
- compaction and retention policy
- better persistence guarantees across process boundaries
- deeper validation of acknowledged object versions

## Working rule

Do not jump ahead into Phase 2 until the working Phase 1 model is stable and inspected. The constitution is the contract; we keep the sequence intact.
