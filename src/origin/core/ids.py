"""Identifier minting.

Origin has no hierarchical file system and no ambient authority (Constitution
sections 5 and 11). That means identifiers cannot be guessable paths like
`/home/david/photo.jpg` — knowing a name must not confer the ability to reach
the thing. Every id here is drawn from a cryptographic source, so possessing one
is itself a secret, and the system additionally requires a Capability before an
id can be used.
"""

from __future__ import annotations

import secrets

#: The principal outside the system. Not a Unit: it has no arena, is never
#: scheduled, and cannot be frozen or killed by anything. It is where the
#: Guardian Capability lives (Constitution section 7).
HUMAN = "human"

#: The Nucleus as a message principal. It originates only core signals.
NUCLEUS = "nucleus"


def new_id(prefix: str = "") -> str:
    """Return a 128-bit unguessable identifier, optionally namespaced by prefix.

    The prefix is for human readability in logs and traces only. It carries no
    authority and confers no ability to enumerate: `obj_` tells you nothing
    about which objects exist.
    """
    token = secrets.token_hex(16)
    return f"{prefix}_{token}" if prefix else token


def mask(secret: str, keep: int = 6) -> str:
    """Render a secret for display without revealing it.

    Transparency (section 8) means the human can always inspect Capabilities.
    It does not mean the Console should print full secrets into a scrollback
    that can be screenshotted, so inspection shows a stable fingerprint instead.
    """
    if len(secret) <= keep:
        return "*" * len(secret)
    # ASCII-only on purpose: the Console Unit must stay legible on a raw Windows
    # code page, where a typographic ellipsis renders as mojibake.
    return f"{secret[:keep]}..({len(secret)})"
