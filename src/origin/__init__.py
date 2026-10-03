"""Origin — a capability-based operating system for autonomous AI agents.

Everything in this package obeys CONSTITUTION.md, which is the source of truth.
`origin.constitution` restates that law in a form the Nucleus and the test suite
can actually enforce.
"""

__version__ = "0.1.0"

#: Single source of truth for the project's phase label.
#:
#: This used to be hardcoded in three places -- the CLI banner, the console help
#: and the argparse description -- which is how "Phase 1 (Pure Simulation)" kept
#: greeting users long after Phase 1 was finished. One definition, read everywhere,
#: means the label can no longer quietly drift out of date.
#:
#: PROGRESS.md is the checklist this label summarises; update both together.
__phase__ = "Phase 2 complete — Persistent Object Substrate"
