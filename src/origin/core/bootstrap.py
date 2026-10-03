"""Bootstrap — the human's one act of ambient authority, spent at seal() (step 1.6).

Section 9 fixes the boot order: Object store -> Naming/Intent -> Console/Studio ->
Watcher -> initial Improver. Everything in `boot()` happens *before* the Nucleus
seals itself, and it is the only moment in the system's life when authority is
created without a Capability authorising it. `Nucleus.mint` permits that pre-seal
and refuses it afterwards; `Nucleus.birth` is a bare core power pre-seal and is
reachable only through `spawn` afterwards. Bootstrap is therefore a fuse that
burns once: by the time `boot()` returns, the core has spent the ambient power it
was born with, and every further act of creation goes through the Capability
system like everything else.

The chicken-and-egg, and how it is resolved
-------------------------------------------
A Unit's code lives in a versioned Object so an Improver can propose a new version
and roll it back (section 7). But a code Object's payload carries the `services`
map — the ids of the Units this one talks to — and those ids do not exist until
the Units are born. Unit ids cannot be chosen in advance: `Unit.__init__` mints
its own with `new_id`, because an id a caller could pick would be an id a caller
could forge or collide. So bootstrap runs in two passes:

  1. birth the boot Units and spawn the demo Units with empty/placeholder params,
     which fixes every id;
  2. build the services map from those ids and write a *fresh copy* into each
     Unit's arena params. Sharing one dict across arenas would violate invariant
     5 (no unrestricted shared mutable state), so each Unit gets its own;
  3. create the demo code Objects, now that the store id is known;
  4. the demo Units already carry `code_object_id`/`code_seq` from spawn.

Only the demo Units get code Objects. The Nucleus has none (improver.py: its
unimprovability is that there is no id to read, not a deny rule), and neither do
the boot Units: object_store/improver/watcher are in the Improver's
PROTECTED_KINDS, and naming/console have no code Object, so if one ever crashed
the Watcher takes its "no versioned code to improve; left frozen" branch and hands
it to a human. That is the safe outcome. A boot Unit that *could* be auto-replaced
would be born again without the arena-injected validator or store it needs and
fail on its first message — deliberately creating a guaranteed-broken replacement
would be worse than leaving it frozen. The demo Units need nothing arena-injected
(they read `params["services"]`, which travels inside their code Object), so they
are exactly the set the Improver can repair.

Authority topology
------------------
The human mints one namespace-wide GUARDIAN for itself and nothing else for
itself; GUARDIAN is absolute, so that single token is the human's whole reach and
the escape hatch section 8 promises. Every other token is scoped as tightly as its
job allows, because scope follows the target, never the verb:

  object_store, naming : namespace SEND (a service must be able to answer whoever
                         legitimately reached it).
  console              : namespace SEND, RESOLVE, BIND, AUDIT, GRANT — the five
                         standing tokens its own docstring claims, and no more.
                         REVOKE and GUARDIAN are reserved, so no Console code path
                         can reach them.
  watcher              : namespace AUDIT and KILL, scoped SEND to nucleus/console/
                         improver.
  improver             : READ+APPEND scoped to each demo code Object, namespace
                         SPAWN (unavoidable — spawn validates SPAWN against target
                         None, so a scoped SPAWN token could never match), scoped
                         SEND to nucleus/console/watcher/object_store. It holds no
                         KILL, FREEZE, GRANT, REVOKE, PIN, AUDIT or BIND.
  demo units           : scoped SEND to object_store and console only. flaky also
                         holds READ+APPEND on its work log, so `/work` costs no new
                         authority and `_rehome` has something to inherit.

Demo data Objects are created by direct `store.create` calls under the Guardian,
which bypasses routing — appropriate during bootstrap, before there is anything to
route to. Names are bound by real `name.bind` Messages sent as the human with the
Guardian *attached*, so the naming Unit and the present-not-transfer path both get
exercised before seal.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import units  # noqa: F401  — importing units registers every handler
from . import constitution
from .capability import Capability, Right
from .ids import HUMAN, NUCLEUS
from .message import Message
from .nucleus import Nucleus
from .objects import CompactedError, ObjectNotFound, ObjectStore
from .unit import Unit

# Readable Object ids. An id is not a secret: discovering one needs AUDIT or a name
# binding, and reading the Object needs READ, so a guessable id leaks nothing
# (section 5). Readable ids make the audit trail followable, which section 8 wants.
BEACH_PHOTO = "obj_beach_photo"
SUNSET_PHOTO = "obj_sunset_photo"
TALLY = "obj_tally"
MAILBOX_DAVID = "obj_mailbox_david"
MAILBOX_ANNE = "obj_mailbox_anne"
FLAKY_LOG = "obj_flaky_log"
CODE_OBJECT = {kind: f"obj_code_{kind}" for kind in ("photo", "mail", "counter", "flaky")}


@dataclass
class System:
    """The handles main.py and the tests need to drive a booted system.

    Holds ids and the human's Guardian — never `Unit` objects. After boot nothing
    outside the core should be able to reach into an arena, and a leaked Unit
    reference is exactly such a reach. `nucleus` and `store` are here because the
    human principal legitimately drives the core and (in tests) inspects Objects
    directly; neither is an arena. The Guardian is the single most privileged value
    in the process, so main.py treats it accordingly.
    """

    nucleus: Nucleus
    store: ObjectStore
    guardian: Capability
    #: kind -> unit_id for every live Unit, boot and demo.
    services: dict[str, str]
    #: logical name -> object_id for every Object created at boot.
    objects: dict[str, str]

    @property
    def console(self) -> str:
        return self.services["console"]

    @property
    def naming(self) -> str:
        return self.services["naming"]


def boot(
    *, fail_every: int = 2, escalate_after: int = 1, token_lifetime: int = 24, storage_path=None
) -> System:
    """Bring the whole system up and seal the core. Returns a `System` of ids.

    `fail_every` makes the flaky Unit raise on every k-th invocation, which is what
    gives the self-healing loop something deterministic to heal. `escalate_after` is
    how many containments the Watcher waits before asking the Improver to act; 1
    means it escalates immediately, which is what the demo wants.
    """
    nucleus = Nucleus()

    # The human's one absolute token, minted pre-seal with grantor=HUMAN because
    # GUARDIAN is a reserved right and only the human principal may create one.
    guardian = nucleus.mint((Right.GUARDIAN,), None, HUMAN, HUMAN, label="human guardian")

    # The store is gated by the same validator the core uses, so it holds no ambient
    # authority either. This local reference is the only one that exists outside the
    # object_store Unit's arena; bootstrap publishes it on System for tests.
    store = ObjectStore(nucleus.validate, storage_path=storage_path)

    def genesis(*args, **kwargs) -> None:
        # With a durable store, a restart finds these Objects already present. Their
        # history is the truth, so booting must never overwrite or duplicate it.
        try:
            store.create(*args, **kwargs)
        except ObjectNotFound:
            if kwargs.get("object_id") not in store._objects:
                raise

    # --- pass 1: birth the boot Units, which fixes their ids ------------------
    boot: dict[str, Unit] = {kind: nucleus.birth(kind, kind, kind) for kind in constitution.BOOT_ORDER}

    # Inject the two things a Unit cannot mint for itself and cannot be handed as
    # versionable data: the validator (an unforgeable reference to exactly one core
    # operation — it can neither mint, revoke, nor route) and the live store object.
    # These go in the arena, not in params, because a bound method is not data.
    boot["object_store"].arena["store"] = store
    for kind in ("naming", "watcher", "improver"):
        boot[kind].arena["validator"] = nucleus.validate

    boot_ids = {kind: unit.unit_id for kind, unit in boot.items()}
    object_store_id = boot_ids["object_store"]
    console_id = boot_ids["console"]
    naming_id = boot_ids["naming"]
    watcher_id = boot_ids["watcher"]
    improver_id = boot_ids["improver"]

    # The demo Units only ever look up `object_store` in their services map, so the
    # boot ids are all their code Objects need to carry. This is what makes a demo
    # replacement work: everything it depends on travels inside the code payload.
    demo_services = dict(boot_ids)

    # --- demo data Objects ----------------------------------------------------
    genesis(HUMAN, "photo", {"title": "Beach photo", "brightness": 50, "width": 800, "height": 600},
                 guardian, note="genesis", object_id=BEACH_PHOTO)
    genesis(HUMAN, "photo", {"title": "Sunset photo", "brightness": 40, "width": 1024, "height": 768},
                 guardian, note="genesis", object_id=SUNSET_PHOTO)
    genesis(HUMAN, "counter", {"count": 0}, guardian, note="genesis", object_id=TALLY)
    genesis(HUMAN, "mailbox", {"owner": "david", "messages": []}, guardian, note="genesis", object_id=MAILBOX_DAVID)
    genesis(HUMAN, "mailbox", {"owner": "anne", "messages": []}, guardian, note="genesis", object_id=MAILBOX_ANNE)
    genesis(HUMAN, "log", {"work": 0}, guardian, note="genesis", object_id=FLAKY_LOG)

    # --- demo code Objects, then the demo Units themselves -------------------
    # The code payload is {entry, kind, name, params}: exactly what the Improver
    # reads when it proposes a new version and what spawn consumes on restart.
    demo_params: dict[str, dict] = {
        "photo": {"services": dict(demo_services)},
        "mail": {"services": dict(demo_services)},
        "counter": {"services": dict(demo_services)},
        "flaky": {"services": dict(demo_services), "fail_every": fail_every},
    }
    for kind, oid in CODE_OBJECT.items():
        genesis(
            HUMAN, "code",
            {"entry": kind, "kind": kind, "name": kind, "params": demo_params[kind]},
            guardian, note=f"{kind} unit code, seq 0", object_id=oid,
        )

    demo: dict[str, Unit] = {}
    for kind, oid in CODE_OBJECT.items():
        # Start from the *effective* version (preferred if set, else latest), so an
        # Improver's work and a human's re-preference both survive a restart.
        # Service ids are minted fresh every boot, so the stored copy is stale by
        # construction: overlay this boot's ids and keep everything else.
        params, seq = demo_params[kind], 0
        try:
            eff = store.read(HUMAN, oid, guardian)  # read() resolves preferred-else-latest
            stored = eff.payload.get("params") if isinstance(eff.payload, dict) else None
            if isinstance(stored, dict):
                params, seq = {**stored, "services": dict(demo_services)}, eff.seq
        except (ObjectNotFound, CompactedError):
            pass
        demo_params[kind] = params
        demo[kind] = nucleus.spawn(
            HUMAN, kind, kind, kind,
            params=params,
            code_object_id=oid,
            code_seq=seq,
            authority=guardian,
        )
    demo_ids = {kind: unit.unit_id for kind, unit in demo.items()}

    # --- pass 2: the full services map, a fresh copy per Unit ----------------
    all_services = {**boot_ids, **demo_ids}
    boot["object_store"].arena["params"] = {"services": dict(all_services)}
    boot["naming"].arena["params"] = {"services": dict(all_services)}
    boot["console"].arena["params"] = {
        "services": dict(all_services),
        "token_lifetime": token_lifetime,
        "work_object": FLAKY_LOG,
        "storage_path": str(store._storage_path) if store._storage_path is not None else None,
    }
    boot["watcher"].arena["params"] = {"services": dict(all_services), "escalate_after": escalate_after}
    boot["improver"].arena["params"] = {"services": dict(all_services), "max_attempts": 2}

    # --- standing authority, scoped as tightly as each job allows ------------
    def grant(rights, target, holder, label):
        nucleus.mint(rights, target, HUMAN, holder, label=label)

    # Services must be able to answer anyone who legitimately reached them.
    grant((Right.SEND,), None, object_store_id, "object_store: namespace SEND")
    grant((Right.SEND,), None, naming_id, "naming: namespace SEND")

    # The Console's five standing tokens, and no more.
    grant((Right.SEND,), None, console_id, "console: namespace SEND")
    grant((Right.RESOLVE,), None, console_id, "console: namespace RESOLVE")
    grant((Right.BIND,), None, console_id, "console: namespace BIND")
    grant((Right.AUDIT,), None, console_id, "console: namespace AUDIT")
    grant((Right.GRANT,), None, console_id, "console: namespace GRANT")

    # The Watcher holds KILL (separation of duties: the Improver does not) and AUDIT
    # to introspect, but may only talk to the three Units it coordinates with.
    grant((Right.AUDIT,), None, watcher_id, "watcher: namespace AUDIT")
    grant((Right.KILL,), None, watcher_id, "watcher: namespace KILL")
    grant((Right.SEND,), NUCLEUS, watcher_id, "watcher: SEND -> nucleus")
    grant((Right.SEND,), console_id, watcher_id, "watcher: SEND -> console")
    grant((Right.SEND,), improver_id, watcher_id, "watcher: SEND -> improver")

    # The Improver may read and extend Unit code, and spawn — but the SPAWN token is
    # namespace-wide because spawn() validates SPAWN against target None. It may
    # only message the four principals in the repair loop.
    #
    # Its write authority is scoped by kind, not by object id: one token covering
    # every `code` Object, whichever code Object exists now or is created by a
    # future demo Unit. Previously this was one token per known code Object, which
    # meant an Improver that could rewrite code could mechanically rewrite data too
    # -- the limitation section 7's own protection only papered over. Now the
    # token is refused outright on a `photo`, `mailbox` or `counter`.
    def grant_kind(rights, target_kind, holder, label):
        nucleus.mint(rights, None, HUMAN, holder, label=label, target_kind=target_kind)

    grant_kind((Right.READ, Right.APPEND), "code", improver_id, "improver: READ+APPEND -> code Objects")
    grant((Right.SPAWN,), None, improver_id, "improver: namespace SPAWN")
    grant((Right.SEND,), NUCLEUS, improver_id, "improver: SEND -> nucleus")
    grant((Right.SEND,), console_id, improver_id, "improver: SEND -> console")
    grant((Right.SEND,), watcher_id, improver_id, "improver: SEND -> watcher")
    grant((Right.SEND,), object_store_id, improver_id, "improver: SEND -> object_store")

    # Demo Units may only reach the store and the Console. flaky also holds its own
    # work log, so /work needs no fresh authority and _rehome has something to move.
    for kind, uid in demo_ids.items():
        grant((Right.SEND,), object_store_id, uid, f"{kind}: SEND -> object_store")
        grant((Right.SEND,), console_id, uid, f"{kind}: SEND -> console")
    grant((Right.READ, Right.APPEND), FLAKY_LOG, demo_ids["flaky"], "flaky: READ+APPEND -> work log")

    # --- bind names, as the human, presenting the Guardian -------------------
    # Attaching proves possession; it does not delegate (see Nucleus._assert_holds),
    # so the naming Unit never ends up holding the human's escape hatch.
    bindings = [
        ("beach photo", BEACH_PHOTO, "object", "a photo of the beach", ("the beach photo", "beach")),
        ("sunset photo", SUNSET_PHOTO, "object", "a photo of a sunset", ("the sunset photo", "sunset")),
        ("tally", TALLY, "object", "a running count", ("the tally",)),
        ("david", MAILBOX_DAVID, "contact", "David's mailbox", ()),
        ("anne", MAILBOX_ANNE, "contact", "Anne's mailbox", ()),
        ("object_store", object_store_id, "unit", "append-only Object store", ()),
        ("naming", naming_id, "unit", "name and intent resolver", ()),
        ("console", console_id, "unit", "the human interface", ()),
        ("watcher", watcher_id, "unit", "failure watcher", ()),
        ("improver", improver_id, "unit", "self-healing improver", ()),
        ("photo", demo_ids["photo"], "unit", "image editor", ()),
        ("mail", demo_ids["mail"], "unit", "mail deliverer", ()),
        ("counter", demo_ids["counter"], "unit", "tally counter", ()),
        ("flaky", demo_ids["flaky"], "unit", "fault-injected worker", ()),
    ]
    for name, target, kind, description, aliases in bindings:
        nucleus.send_message(
            Message(
                sender=HUMAN,
                recipient=naming_id,
                verb="name.bind",
                payload={"name": name, "target": target, "kind": kind, "description": description, "aliases": aliases},
                caps=(guardian,),
            )
        )

    # If the durable store had to repair itself while loading, say so through the
    # system's own channel. The store keeps no reference to the audit trail (that
    # would be ambient authority); boot, speaking as the human, reports its
    # findings to the Watcher like any other message, and routing audits it.
    if store.recovery_events or store.history_damage:
        nucleus.send_message(
            Message(
                sender=HUMAN,
                recipient=watcher_id,
                verb="store.recovery",
                payload={"recovery": list(store.recovery_events), "damage": list(store.history_damage)},
                caps=(guardian,),
            )
        )

    # --- spend the fuse -------------------------------------------------------
    # seal() drops ambient mint/birth and interrupts every Unit. schedule() then
    # flushes the queued name.bind messages and the sealed signals in one pass.
    nucleus.seal()
    nucleus.schedule()

    # Boot produced a burst of human-directed output (name.bound replies, the
    # Console's sealed announcement). Drain it so main.py starts from silence and
    # prints its own banner; none of it is state, only chatter.
    nucleus.drain_output()

    objects = {
        "beach_photo": BEACH_PHOTO,
        "sunset_photo": SUNSET_PHOTO,
        "tally": TALLY,
        "mailbox_david": MAILBOX_DAVID,
        "mailbox_anne": MAILBOX_ANNE,
        "flaky_log": FLAKY_LOG,
        **{f"code_{kind}": oid for kind, oid in CODE_OBJECT.items()},
    }
    return System(nucleus=nucleus, store=store, guardian=guardian, services=all_services, objects=objects)
