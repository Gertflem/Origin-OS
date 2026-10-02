"""The Constitution as machine-checkable law.

CONSTITUTION.md is prose written for humans. This module is the same law written
for the interpreter: the closed set of primitives, the exact list of powers the
Nucleus is permitted to hold, and the ten non-negotiable invariants.

Everything here is bound at import time and never reassigned. Invariant 1 says
the Nucleus is runtime-immutable; the mechanism that makes that true is simply
that no code path in the system writes to this module or to the Nucleus's rule
tables after bootstrap.
"""

from __future__ import annotations


class InvariantViolation(RuntimeError):
    """Raised when an action would break one of the ten non-negotiables.

    The Watcher Unit treats this as a containment event, not as an ordinary
    error: something in the system tried to exceed its authority.
    """

    def __init__(self, invariant: int, detail: str) -> None:
        self.invariant = invariant
        self.detail = detail
        super().__init__(f"Invariant {invariant} violated: {detail}")


# --- Section 1: Core Primitives -------------------------------------------
# The closed set. Adding a sixth primitive is a constitutional amendment, not a
# pull request.
PRIMITIVES = frozenset({"Object", "Unit", "Capability", "Message", "Nucleus"})

# --- Section 2: The Nucleus ------------------------------------------------
# The complete and exhaustive list of what the Nucleus may do. Any power not
# named here must be implemented as an ordinary Unit.
NUCLEUS_POWERS = frozenset(
    {
        "schedule",
        "enforce_arena",
        "mint_capability",
        "validate_capability",
        "transfer_capability",
        "revoke_capability",
        "route_message",
        "handle_interrupt",
        "freeze_unit",
        "kill_unit",
        "birth_boot_units",
    }
)

# --- Section 9: Bootstrap --------------------------------------------------
# The small ordered set of essential Units the Nucleus births before it drops
# its privilege. Order matters: each unit may depend on the ones before it.
BOOT_ORDER = (
    "object_store",
    "naming",
    "console",
    "watcher",
    "improver",
)

# --- Section 11: Explicitly Forbidden -------------------------------------
FORBIDDEN_AT_FOUNDATION = frozenset(
    {
        "hierarchical_file_system",
        "data_trapping_application_container",
        "save_button",
        "unsaved_state",
        "ambient_authority",
        "unrestricted_shared_mutable_state",
        "nucleus_self_modification",
        "true_ephemeral_object",
    }
)

# --- Section 10: Non-Negotiable Invariants --------------------------------
INVARIANTS = {
    1: "The Nucleus remains tiny and runtime-immutable.",
    2: "No ambient authority shall exist.",
    3: "All lasting state is append-only and versioned.",
    4: "Communication occurs only through Messages carrying explicit Capabilities.",
    5: "Units are isolated by default.",
    6: "The system must remain comprehensible in principle to a single skilled human mind.",
    7: "Nothing the human has been told exists may be silently lost.",
    8: "Self-improvement may never compromise the above rules.",
}


def require(invariant: int, condition: bool, detail: str) -> None:
    """Assert a constitutional invariant, raising InvariantViolation if it fails.

    Used at the boundaries where authority is exercised, so a violation is
    attributed to the act rather than surfacing later as corrupted state.
    """
    if not condition:
        raise InvariantViolation(invariant, detail)


def is_boot_unit(kind: str) -> bool:
    return kind in BOOT_ORDER
