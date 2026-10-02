"""The ordinary Units of Origin.

Constitution section 4: drivers, services, naming, intent resolution, human
interfaces, and Improvers are all ordinary Units. Nothing in this package is
privileged. Every Unit here talks to the rest of the system through Messages and
acts only under Capabilities it was given, exactly like a Unit spawned five
minutes ago by the Improver.

The dependency graph points one way — units depend on unit/message/objects, and
never on nucleus internals — so no Unit can reach the core except by addressing
a Message to it.

Importing the package registers every handler. Registration happens as a side
effect of the `@unit_type` decorator, which is the whole point of the table: a
Unit kind exists because some module said so, not because the core was told
about it. `bootstrap` only has to import this package once.
"""

from ..unit import UNIT_TYPES, unit_type
from . import console, demo, improver, naming, object_store, watcher  # noqa: F401

__all__ = [
    "UNIT_TYPES",
    "unit_type",
    "console",
    "demo",
    "improver",
    "naming",
    "object_store",
    "watcher",
]
