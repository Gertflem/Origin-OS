"""main.py — the human's window into Origin (step 1.8).

Everything before this file builds a system that runs on Messages alone. This is
the one place where a person typing a sentence becomes a Message on the wire, and
where the system's replies become text on a terminal. It is deliberately thin: it
boots the system, hands the human's input to the Console as a `console.input`
Message, runs the scheduler until the system is quiescent, and prints whatever the
system addressed to `HUMAN`.

Two things live here rather than in a Unit, and both are the human principal acting
from *outside* the capability system — the same layer that ran bootstrap:

**`/revoke` is intercepted and sent to the Nucleus as the Guardian.** REVOKE is a
RESERVED_RIGHT (capability.py): no Unit can hold or delegate it, so the Console
cannot process a revocation even in principle. The escape hatch section 7 promises
has to be wielded by the human directly, or it is not an escape hatch. main.py
therefore routes `/revoke` to the core with the Guardian attached and never through
the Console.

**Reading full Capability ids.** Inspection output masks ids (ids.mask) so secrets
never spray into scrollback that could be screenshotted. But the human is the trust
root and already holds the Guardian — for the deliberate act of choosing a token to
revoke, main.py reads the registry directly instead of adding a public method to the
Nucleus. That keeps invariant 1 (the core stays tiny) true while still letting the
one principal who is allowed to revoke actually name what to revoke.

The interactive REPL is the default. `--demo` runs a scripted tour of the whole
loop — intent, ambiguity, mail, revocation, self-healing — so the system can be
watched end to end without typing.
"""

from __future__ import annotations

import argparse
import sys

from . import __phase__
from .bootstrap import System, boot
from .capability import Right
from .ids import HUMAN, NUCLEUS, mask
from .message import Message

#: Printed once at startup. States the two facts a newcomer needs: the core is
#: sealed (its bootstrap authority is spent, not merely unused), and every action
#: from here is capability-mediated.
BANNER = f"""\
Origin — {__phase__}

A capability-based kernel: five primitives, no ambient authority, append-only
Objects. Everything you type becomes Messages on a wire, and nothing happens
without a Capability authorising it.

The Nucleus is sealed. Its bootstrap authority is spent — from here every action,
including the core's own, goes through a Capability.

  Say what you want:   brighten the beach photo by 20
  Inspect freely:      /status  /objects  /reclaimable  /sweep  /history  /units  /agents  /names  /caps  /audit  /log  /watcher  /improver  /powers
  Borrow authority:    /show <name>   /grant <right> <target> <unit>   /spawn <kind> <name>
  The escape hatch:    /revoke        (answered here as the Guardian, not by a Unit)
  Everything else:     /help

Ctrl-D or /quit to leave.
"""


def _reconfigure_stdio() -> None:
    """Emit UTF-8 regardless of the platform's default code page.

    On Windows the console defaults to a legacy code page (cp1252, cp437, ...),
    which turns the em-dashes this system writes into mojibake. Reconfiguring the
    streams once, here, fixes every string the system produces without forcing
    ASCII-only prose on the Units. `errors="replace"` means a stray unencodable
    character degrades to a placeholder instead of crashing output mid-sentence.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass  # a stream that cannot be reconfigured is not worth dying for


def _print_output(nucleus) -> None:
    """Print everything the system addressed to the human since the last drain.

    Output to `HUMAN` is a Message like any other; the Nucleus parks it because
    there is no arena to deliver it to. Nearly all of it is the Console speaking
    (`console.output`); core replies to a `/revoke` arrive under their own verb and
    fall through to the generic rendering.
    """
    for item in nucleus.drain_output():
        verb = item.get("verb")
        payload = item.get("payload")
        if verb == "console.output" and isinstance(payload, dict) and "text" in payload:
            print(payload["text"])
        else:
            print(f"[{verb}] {payload}")


def _settle(system: System, msg: Message) -> None:
    """Route one human Message, run the system to quiescence, print the replies.

    A refusal to route (no SEND authority, dead recipient) is printed, not raised:
    the REPL must survive anything the human types. Scheduling cannot raise either
    — the Nucleus contains every Unit exception — but it is guarded all the same so
    a surprise in the core surfaces as text rather than a dead prompt.
    """
    nucleus = system.nucleus
    try:
        nucleus.send_message(msg)
    except Exception as exc:  # noqa: BLE001 - the human's terminal must not die
        print(f"! the Nucleus refused to route that: {type(exc).__name__}: {exc}")
    try:
        nucleus.schedule()
    except Exception as exc:  # noqa: BLE001
        print(f"! scheduling stopped: {type(exc).__name__}: {exc}")
    _print_output(nucleus)


def _to_console(system: System, text: str) -> None:
    """Send the human's words to the Console.

    No Capability is attached: routing authority comes from the Guardian the human
    already holds (found by the core via `find_capability`), and attaching the
    Guardian here would present the escape hatch to a Unit for no reason.
    Presentation never transfers, but minimal privilege says do not present it.
    """
    _settle(
        system,
        Message(sender=HUMAN, recipient=system.console, verb="console.input", payload={"text": text}),
    )


def _revocable(nucleus) -> list[tuple[str, object]]:
    """Live, non-absolute Capabilities, oldest first, as (full_id, record) pairs.

    The Guardian is omitted so a stray `/revoke 1` cannot disarm the human; it can
    still be revoked deliberately, by full id. Reading `_caps` directly is the
    human trust root inspecting its own system — see the module docstring.
    """
    step = nucleus.current_step()
    out = [
        (cid, rec)
        for cid, rec in nucleus._caps.items()
        if not rec.revoked and rec.live_at(step) and Right.GUARDIAN not in rec.rights
    ]
    out.sort(key=lambda pair: (pair[1].created_step, pair[0]))
    return out


def _revoke(system: System, args: list[str]) -> None:
    """The Guardian escape hatch: withdraw a Capability from every holder at once."""
    nucleus = system.nucleus
    caps = _revocable(nucleus)

    if not args:
        if not caps:
            print("No live, revocable Capability right now (the Guardian is never listed).")
            return
        print(f"{len(caps)} live Capabilities you may revoke. Full ids are shown here, and only")
        print("here, because choosing what to revoke is the one act that needs them:")
        for i, (cid, rec) in enumerate(caps, 1):
            rights = "+".join(sorted(r.value for r in rec.rights))
            holders = ",".join(mask(str(h)) for h in sorted(rec.holders)) or "-"
            # Both scopes, so the revoke list never understates what a token covers.
            target = mask(str(rec.target)) if rec.target else "(namespace)"
            if rec.target_kind:
                target = f"{rec.target_kind} objects"
            life = f"expires {rec.expires_at_step}" if rec.is_temporary() else "permanent"
            print(f"  {i:>2}. {cid}  {rights:<16} target {target:<18} to {holders:<12} {life:<12} {rec.label}")
        print("Revoke with: /revoke <number|cap_id|last> [reason]   ('last' = most recent borrowed token)")
        return

    selector, reason = args[0], " ".join(args[1:]) or "revoked by the human through the Guardian"
    target: str | None = None

    if selector == "last":
        # The most recent *temporary* token is an intent-time Capability the Console
        # minted for one action. Revoking that is always safe; revoking the newest
        # permanent token could disable a boot service mid-session.
        temps = [cid for cid, rec in caps if rec.is_temporary()]
        if not temps:
            print("No temporary (intent-time) Capability is live, so 'last' has nothing safe to pick.")
            return
        target = temps[-1]
    elif selector.isdigit():
        index = int(selector)
        if not 1 <= index <= len(caps):
            print(f"{selector} is out of range (1-{len(caps)}). Run /revoke to list them.")
            return
        target = caps[index - 1][0]
    else:
        matches = [cid for cid, _ in caps if cid == selector or cid.startswith(selector)]
        guardian_id = system.guardian.cap_id
        if not matches and (guardian_id == selector or guardian_id.startswith(selector)):
            matches = [guardian_id]  # the human may disarm even the Guardian, deliberately
        if not matches:
            print(f"No Capability matches {selector!r}. Run /revoke to list them.")
            return
        if len(matches) > 1:
            print(f"{selector!r} matches {len(matches)} Capabilities; be more specific.")
            return
        target = matches[0]

    _settle(
        system,
        Message(
            sender=HUMAN,
            recipient=NUCLEUS,
            verb="revoke",
            payload={"cap_id": target, "reason": reason},
            caps=(system.guardian,),
        ),
    )


def _handle(system: System, line: str) -> bool:
    """Act on one line of human input. Returns False when the human wants to leave."""
    text = line.strip()
    if not text:
        return True
    if text.lower() in ("/quit", "/exit", "/q", "quit", "exit"):
        return False
    parts = text.split()
    if parts[0].lower() == "/status":
        _status(system)
        return True
    if parts[0].lower() == "/units":
        _units(system)
        return True
    if parts[0].lower() == "/objects":
        _objects(system)
        return True
    if parts[0].lower() == "/reclaimable":
        _reclaimable(system, parts[1:])
        return True
    if parts[0].lower() == "/sweep":
        _sweep(system, parts[1:])
        return True
    if parts[0].lower() == "/names":
        _names(system)
        return True
    if parts[0].lower() == "/caps":
        _caps(system, parts[1:])
        return True
    if parts[0].lower() == "/audit":
        _audit(system, parts[1:])
        return True
    if parts[0].lower() == "/messages":
        _messages(system, parts[1:])
        return True
    if parts[0].lower() in ("/watcher", "/improver"):
        _to_console(system, " ".join(parts))
        return True
    if parts[0].lower() == "/history":
        _history(system)
        return True
    if parts[0].lower() == "/focus":
        _focus(system, " ".join(parts[1:]) or None)
        return True
    if parts[0].lower() == "/show":
        _show(system, " ".join(parts[1:]) or None)
        return True
    if parts[0].lower() == "/grant":
        _grant(system, parts[1:])
        return True
    if parts[0].lower() == "/freeze":
        _freeze(system, " ".join(parts[1:]) or None)
        return True
    if parts[0].lower() == "/spawn":
        _spawn(system, " ".join(parts[1:]) or None)
        return True
    if parts[0].lower() == "/kill":
        _kill(system, " ".join(parts[1:]) or None)
        return True
    if parts[0].lower() == "/rollback":
        if len(parts) > 1:
            _rollback(system, parts[1], parts[2] if len(parts) > 2 else None)
        else:
            _rollback(system)
        return True
    if parts[0].lower() == "/work":
        _work(system, parts[1] if len(parts) > 1 else None)
        return True
    if parts[0].lower() == "/powers":
        _powers(system)
        return True
    if parts[0].lower() == "/help":
        print(BANNER)
        return True
    if parts[0].lower() == "/revoke":
        _revoke(system, parts[1:])
        return True
    _to_console(system, text)
    return True


#: The scripted tour. Each chapter is a title and the exact lines fed to the Console
#: as the human. Intents need a following "confirm" because nothing runs until the
#: human approves the proposed authority — the demo honours that gate rather than
#: bypassing it, because bypassing it would demo a different system.
DEMO_SCRIPT: list[tuple[str, list[str]]] = [
    (
        "starting state: data and code Objects, nine Units asleep, the core sealed",
        ["/objects", "/units"],
    ),
    (
        "read an Object: express -> resolve -> propose -> confirm -> execute -> inspectable result",
        ["show the beach photo", "confirm"],
    ),
    (
        "change an Object: the result carries the seq and before/after, never just 'done'",
        ["brighten the beach photo by 20", "confirm", "show the beach photo", "confirm"],
    ),
    (
        "ambiguity is escalated, never guessed: 'the photo' matches two Objects",
        ["brighten the photo", "1", "confirm"],
    ),
    (
        "one intent, two Objects, two tokens: mailing a photo reads it AND appends to a mailbox",
        ["send the beach photo to David", "confirm", "/objects"],
    ),
    (
        "the escape hatch: revoke the most recent borrowed token; it dies for every holder at once",
        ["/revoke", "/revoke last revoked during the demo", "/caps"],
    ),
    (
        "inspection costs no new authority: the Console already holds AUDIT",
        ["/names"],
    ),
    (
        "self-healing (section 7): poke the fault-injected Unit and watch "
        "detect -> contain -> escalate -> improve -> restart",
        ["/work 3", "/units", "/audit 30"],
    ),
    (
        "invariant 1: the Nucleus's own account of its powers, and whether it is still tiny",
        ["/powers"],
    ),
]


def _demo(system: System) -> None:
    print(BANNER)
    print("(scripted tour — every line below is fed to the Console as the human)\n")
    for title, lines in DEMO_SCRIPT:
        print(f"\n{'=' * 78}\n=== {title}\n{'=' * 78}")
        for line in lines:
            print(f"\nyou> {line}")
            _handle(system, line)
    print(f"\n{'=' * 78}")
    print(f"End of tour at step {system.nucleus.current_step()}. Every action above is in the audit trail.")


def _repl(system: System) -> None:
    print(BANNER)
    while True:
        try:
            line = input("you> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not _handle(system, line):
            break
    print(f"step {system.nucleus.current_step()}. Every action above is in the audit trail.")


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n / (1024 * 1024):.1f} MiB"


def _reclaimable(system: System, args: list[str]) -> None:
    """Show what compaction could reclaim, per Object and store-wide.

    Read-only: this never compacts anything. Usage: /reclaimable [keep_recent]
    """
    keep_recent = 3
    if args:
        try:
            keep_recent = int(args[0])
        except ValueError:
            print("Usage: /reclaimable [keep_recent]   (keep_recent is a whole number, default 3)")
            return
    report = system.store.reclaimable_report(HUMAN, system.guardian, keep_recent=keep_recent)
    totals = report["totals"]
    print(f"Reclaimable history (keeping the {report['keep_recent']} most recent versions per Object)")
    print(
        f"  store-wide: {totals['reclaimable_versions']} of {totals['total_versions']} versions, "
        f"{_fmt_bytes(totals['reclaimable_bytes'])} reclaimable "
        f"({_fmt_bytes(totals['retained_bytes'])} stays), "
        f"across {totals['objects_with_reclaimable']} of {totals['objects']} Objects"
    )
    for row in report["objects"]:
        if not row["reclaimable_versions"]:
            continue
        prot = row["protected"]
        print(
            "  "
            f"{mask(str(row['object_id'])):<20} {row['kind']:<10} "
            f"reclaim {row['reclaimable_versions']:>3} of {row['total_versions']:>3}  "
            f"{_fmt_bytes(row['reclaimable_bytes']):>9}  seqs {row['reclaimable_seqs']}  "
            f"protected: pins {prot['pinned']} preferred {prot['preferred']}"
        )
    if not totals["reclaimable_versions"]:
        print("  nothing to reclaim at this window.")
    print("  This is a preview. Nothing was compacted; compaction stays an explicit request.")


def _sweep(system: System, args: list[str]) -> None:
    """Apply the retention policy across the whole store.

    `/sweep [keep_recent] [min_versions]` previews; add `apply` to act. This is
    section 3's tiering running as policy rather than as a comment, but it stays
    a dry run until explicitly told otherwise.
    """
    keep_recent, min_versions, apply = 3, 32, False
    positional: list[int] = []
    for arg in args:
        if arg.lower() == "apply":
            apply = True
            continue
        try:
            positional.append(int(arg))
        except ValueError:
            print("Usage: /sweep [keep_recent] [min_versions] [apply]")
            print("       keep_recent   versions to keep per Object (default 3)")
            print("       min_versions  skip Objects shorter than this (default 32)")
            print("       apply         actually compact; without it this is a preview")
            return
    if positional:
        keep_recent = positional[0]
    if len(positional) > 1:
        min_versions = positional[1]

    report = system.store.sweep(
        HUMAN,
        system.guardian,
        keep_recent=keep_recent,
        min_versions=min_versions,
        dry_run=not apply,
    )
    head = "Retention sweep" if apply else "Retention sweep (preview, nothing touched)"
    print(f"{head}: keeping the {report['keep_recent']} most recent versions per Object, "
          f"skipping Objects under {report['min_versions']} versions")
    if not report["candidates"]:
        print("  no Object has outgrown its window yet.")
        return
    for row in report["candidates"]:
        print(
            "  "
            f"{mask(str(row['object_id'])):<20} {row['kind']:<10} "
            f"{len(row['reclaimable']):>3} of {row['versions']:>4} versions  "
            f"{_fmt_bytes(row['reclaimable_bytes']):>9}  seqs {row['reclaimable']}"
        )
    if apply:
        print(
            f"  compacted {report['objects_swept']} Objects, reclaimed "
            f"{report['versions_reclaimed']} version payloads ({_fmt_bytes(report['bytes_reclaimed'])}). "
            "Metadata for every reclaimed version was kept."
        )
    else:
        print("  Nothing was compacted. Re-run with 'apply' to reclaim these.")


def _status(system: System) -> None:
    units = getattr(system.nucleus, "_units", {})
    objects = getattr(system.store, "_objects", {})
    caps = getattr(system.nucleus, "_caps", {})
    durable_versions = 0
    compacted_versions = 0
    storage_path = getattr(system.store, "_storage_path", None)
    temp_snapshot = False
    if storage_path is not None:
        temp_snapshot = storage_path.with_suffix(f"{storage_path.suffix}.tmp").exists()
    pinned_versions = 0
    for obj in objects.values():
        durable_versions += sum(1 for version in obj.versions if version.acked)
        compacted_versions += len(obj.compacted)
        pinned_versions += len(obj.pins)
    print("Origin status")
    print(f"  step: {system.nucleus.current_step()}")
    print(f"  units: {len(units)}")
    print(f"  objects: {len(objects)}")
    print(f"  durable versions: {durable_versions}")
    print(f"  compacted versions: {compacted_versions}")
    print(f"  pinned versions: {pinned_versions}")
    totals = system.store.reclaimable_report(HUMAN, system.guardian)["totals"]
    print(
        f"  reclaimable history: {totals['reclaimable_versions']} versions, "
        f"{_fmt_bytes(totals['reclaimable_bytes'])} (see /reclaimable)"
    )
    print(f"  storage: {storage_path or 'memory'}")
    print(f"  temp snapshot: {'present' if temp_snapshot else 'clean'}")
    events = getattr(system.store, "recovery_events", [])
    print(f"  recovery events: {', '.join(e['kind'] for e in events) if events else 'none'}")
    console_unit = getattr(system.nucleus, "_units", {}).get(system.console)
    focus = getattr(console_unit, "arena", {}).get("focus", {}) if console_unit is not None else {}
    focus_name = focus.get("name") or focus.get("object_id") or focus.get("recipient_name") or "none"
    print(f"  focus: {focus_name}")
    print(f"  capabilities: {len(caps)}")
    print(f"  console: {system.console}")
    print(f"  naming: {system.naming}")
    print(f"  guardian: {system.guardian}")


def _units(system: System) -> None:
    rows = [unit.describe() for unit in sorted(getattr(system.nucleus, "_units", {}).values(), key=lambda u: (u.born_step, u.name))]
    print("Origin units")
    print(f"  total: {len(rows)}")
    for unit in rows:
        print(
            "  "
            f"{unit.get('name', ''):<12} {unit.get('kind', ''):<12} {unit.get('state', ''):<8} "
            f"born {unit.get('born_step', 0):>3}  crashes {unit.get('crashes', 0)}  "
            f"preempted {unit.get('preemptions', 0)}  caps {unit.get('caps_held', 0)}  "
            f"inbox {unit.get('inbox_depth', 0)}  {mask(str(unit.get('unit_id', '')))}"
        )


def _objects(system: System) -> None:
    rows = system.store.enumerate(HUMAN, system.guardian)
    print("Origin objects")
    print(f"  total: {len(rows)}")
    for obj in sorted(rows, key=lambda item: (item.get("kind", ""), item.get("object_id", ""))):
        compacted = obj.get("compacted", [])
        print(
            "  "
            f"{mask(str(obj.get('object_id', ''))):<20} {obj.get('kind', ''):<10} "
            f"versions {obj.get('versions', 0):>3}  latest {obj.get('latest_seq', 0):>3}  "
            f"preferred {obj.get('preferred')}  pins {obj.get('pins', [])}  compacted {list(compacted)}"
        )


def _names(system: System) -> None:
    naming = getattr(system.nucleus, "_units", {}).get(system.naming)
    bindings = getattr(naming, "mem", {}).get("bindings", {}) if naming is not None else {}
    rows = []
    for entry in bindings.values():
        if hasattr(entry, "describe"):
            rows.append(entry.describe())
        elif isinstance(entry, dict):
            rows.append(entry)
    print("Origin names")
    print(f"  total: {len(rows)} bindings")
    for binding in sorted(rows, key=lambda item: str(item.get("name", "")).lower()):
        print(
            "  "
            f"{binding.get('name', ''):<20} {binding.get('kind', ''):<8} -> {mask(str(binding.get('target', '')))}  "
            f"{binding.get('description', '')}".rstrip()
        )


def _caps(system: System, args: list[str] | None = None) -> None:
    rows = list(getattr(system.nucleus, "_caps", {}).values())
    filter_tokens: list[str] = []
    live_only = False
    revoked_only = False
    for arg in args or []:
        key = arg.lower()
        if key in {"live", "active"}:
            live_only = True
        elif key == "revoked":
            revoked_only = True
        elif key not in {"all", "allcaps"}:
            filter_tokens.append(arg)

    if live_only:
        rows = [rec for rec in rows if not rec.revoked and rec.live_at(system.nucleus.current_step())]
    if revoked_only:
        rows = [rec for rec in rows if rec.revoked]
    if filter_tokens:
        rows = [
            rec
            for rec in rows
            if all(
                any(
                    right.value.lower() == token.lower() or right.value.lower().startswith(token.lower())
                    for right in rec.rights
                )
                or any(holder.lower() == token.lower() or holder.lower().startswith(token.lower()) for holder in rec.holders)
                or (rec.target is not None and (rec.target.lower() == token.lower() or token.lower() in rec.target.lower()))
                for token in (part.lower() for part in filter_tokens)
            )
        ]

    print("Origin caps")
    print("  capability registry")
    mode = "all"
    if live_only and not revoked_only:
        mode = "live"
    elif revoked_only and not live_only:
        mode = "revoked"
    print(f"  mode: {mode}")
    print(f"  total: {len(rows)} capabilities")
    for rec in sorted(rows, key=lambda item: (item.created_step, item.cap_id)):
        rights = "+".join(sorted(r.value for r in rec.rights)) or "-"
        holders = ", ".join(mask(str(holder)) for holder in sorted(rec.holders)) or "-"
        target = mask(str(rec.target)) if rec.target else "(namespace)"
        if rec.target_kind:
            target = f"{rec.target_kind} objects"
        grantor = mask(str(rec.grantor))
        lifetime = f"expires {rec.expires_at_step}" if rec.expires_at_step is not None else "permanent"
        if rec.revoked:
            state = "revoked"
            revoker = f" by {mask(str(rec.revoked_by))}"
            reason = f" reason={rec.revoked_reason or 'n/a'}"
        elif rec.live_at(system.nucleus.current_step()):
            state = "live"
            revoker = ""
            reason = ""
        else:
            state = "expired"
            revoker = ""
            reason = ""
        print(
            "  "
            f"{mask(str(rec.cap_id)):<20} {rights:<20} target {target:<18} to {holders:<18} {lifetime:<12} {state:<8} grantor {grantor:<12}{revoker}{reason}  {rec.label}"
        )


def _audit(system: System, args: list[str] | None = None) -> None:
    limit = int((args or ["20"])[0]) if (args and (args[0].isdigit() or args[0].startswith("-"))) else 20
    rows = system.nucleus.audit(system.guardian, HUMAN, limit=max(0, limit))
    print("Origin audit")
    print(f"  last {len(rows)} core events")
    for entry in rows:
        detail = {k: v for k, v in entry.items() if k not in ("step", "kind")}
        print(f"  step {entry.get('step', 0):>4}  {entry.get('kind', 'event'):<22} {detail}")


def _messages(system: System, args: list[str] | None = None) -> None:
    limit = int((args or ["20"])[0]) if (args and (args[0].isdigit() or args[0].startswith("-"))) else 20
    rows = system.nucleus.describe(HUMAN, system.guardian, {"what": "messages", "limit": max(0, limit)}).get("messages", [])
    print("Origin messages")
    print(f"  last {len(rows)} routed messages")
    for entry in rows:
        print(
            f"  step {entry.get('step', 0):>4}  sender={entry.get('sender', '')} recipient={entry.get('recipient', '')} "
            f"verb={entry.get('verb', '')} caps={len(entry.get('caps', []) or [])}"
        )


def _history(system: System) -> None:
    rows = system.store.enumerate(HUMAN, system.guardian)
    print("Origin history")
    print(f"  total: {len(rows)} objects")
    if not rows:
        print("  no object history yet")
        return
    for obj in sorted(rows, key=lambda item: item.get("kind", "")):
        object_id = obj.get("object_id")
        print(f"  {mask(str(object_id))}  kind={obj.get('kind', '')}  versions={obj.get('versions', 0)}")
        history = system.store.history(HUMAN, object_id, system.guardian)
        for version in history:
            print(
                "    "
                f"seq {version.get('seq', 0):>3}  step {version.get('step', 0):>4}  "
                f"author {mask(str(version.get('author', ''))):<12}  acked {str(version.get('acked', False)).lower()}"
                f"  note {version.get('note') or '-'}"
            )


def _focus(system: System, name: str | None = None) -> None:
    text = "/focus" if name is None else f"/focus {name}"
    _to_console(system, text)


def _show(system: System, name: str | None = None) -> None:
    text = "/show" if name is None else f"/show {name}"
    _to_console(system, text)


def _grant(system: System, args: list[str] | None = None) -> None:
    text = "/grant" if not args else "/grant " + " ".join(args)
    _to_console(system, text)


def _freeze(system: System, name: str | None = None) -> None:
    text = "/freeze" if name is None else f"/freeze {name}"
    _to_console(system, text)


def _kill(system: System, name: str | None = None) -> None:
    text = "/kill" if name is None else f"/kill {name}"
    _to_console(system, text)


def _spawn(system: System, spec: str | None = None) -> None:
    text = "/spawn" if spec is None else f"/spawn {spec}"
    _to_console(system, text)


def _rollback(system: System, name: str | None = None, seq: str | None = None) -> None:
    parts = ["/rollback", name] if name else ["/rollback"]
    if seq is not None:
        parts.append(seq)
    _to_console(system, " ".join(parts))


def _work(system: System, count: int | str | None = None) -> None:
    arg = "" if count is None else str(count)
    _to_console(system, "/work " + arg if arg else "/work")


def _powers(system: System) -> None:
    report = system.nucleus.audit_powers()
    print("Origin powers")
    print("  Nucleus account of its constitutional powers")
    for name in report.get("public_methods", []):
        print(f"  {name}")
    print(f"  public methods not mapped to a constitutional power: {report.get('unmapped') or 'none'}")
    print(f"  powers claimed outside the Constitution: {report.get('powers_outside_constitution') or 'none'}")
    print(f"  still tiny: {report.get('tiny')}")


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    parser = argparse.ArgumentParser(
        prog="origin",
        description=f"Origin ({__phase__}) — a capability-based OS kernel for AI agents, simulated in pure Python.",
    )
    parser.add_argument("--demo", action="store_true", help="run a scripted tour instead of the interactive REPL")
    parser.add_argument("--status", action="store_true", help="print a compact runtime summary without entering the interactive REPL")
    parser.add_argument("--fail-every", type=int, default=2,
                        help="the fault-injected Unit raises on every k-th invocation (default 2)")
    parser.add_argument("--escalate-after", type=int, default=1,
                        help="containments the Watcher waits before asking the Improver to act (default 1)")
    parser.add_argument("--token-lifetime", type=int, default=24,
                        help="steps an intent-time Capability lives before it expires (default 24)")
    parser.add_argument("--storage", metavar="PATH", default=None,
                        help="persist Objects to this file; state survives restarts (default: in-memory)")
    args = parser.parse_args(argv)

    system = boot(
        fail_every=args.fail_every,
        escalate_after=args.escalate_after,
        token_lifetime=args.token_lifetime,
        storage_path=args.storage,
    )
    if args.demo:
        _demo(system)
    elif args.status:
        _status(system)
    else:
        _repl(system)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
