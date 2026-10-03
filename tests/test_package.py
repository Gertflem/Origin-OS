import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from origin import __phase__  # noqa: E402

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class TestPackage(unittest.TestCase):
    def test_package_imports(self):
        import origin
        import origin.main
        import origin.core.bootstrap

        self.assertTrue(hasattr(origin, "__version__"))
        self.assertTrue(callable(origin.main.main))

    def test_cli_module_exists(self):
        import origin.cli

        self.assertTrue(callable(origin.cli.main))

    def test_os_packages_import(self):
        import origin.capabilities
        import origin.human
        import origin.kernel
        import origin.messages
        import origin.objects
        import origin.services
        import origin.simulator

        self.assertTrue(hasattr(origin.kernel, "Nucleus"))
        self.assertTrue(hasattr(origin.simulator, "boot"))

    def test_object_durability_contract(self):
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        store = ObjectStore(validator)
        cap = Capability("cap-durability")

        obj = store.create("alice", "journal", {"text": "first"}, cap, step=1)
        self.assertTrue(obj.versions[0].acked)

        v2 = store.append("alice", obj.object_id, {"text": "second"}, cap, step=2)
        self.assertTrue(v2.acked)

        store.acknowledge("alice", obj.object_id, v2.seq, cap, acked=False, note="not yet durable")
        self.assertFalse(store.read("alice", obj.object_id, cap, seq=v2.seq).acked)
        self.assertEqual(len(store.durable_versions("alice", obj.object_id, cap)), 1)

    def test_compaction_never_reclaims_preferred_and_preserves_current_state(self):
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        store = ObjectStore(lambda *a: None)
        cap = Capability("cap-compact")
        a = store.create("alice", "journal", {"t": 0}, cap, step=1)
        for i in range(1, 7):
            store.append("alice", a.object_id, {"t": i}, cap, step=i + 1)
        store.prefer("alice", a.object_id, 1, cap)
        result = store.compact("alice", a.object_id, cap, keep_recent=2)
        self.assertNotIn(1, result["reclaimed"])  # preferred is what consumers run
        self.assertEqual(store.read("alice", a.object_id, cap).payload, {"t": 1})

        b = store.create("alice", "journal", {"t": 0}, cap, step=1)
        for i in range(1, 7):
            store.append("alice", b.object_id, {"t": i}, cap, step=i + 1)
        store.compact("alice", b.object_id, cap, keep_recent=2)
        # Bookkeeping must not replace the Object's actual current state.
        self.assertEqual(store.read("alice", b.object_id, cap).payload, {"t": 6})
        note = store.history("alice", b.object_id, cap)[-1]["note"]
        self.assertIn("compacted", note)
        self.assertIn("0", note)

    def test_store_recovery_events_reach_watcher_and_audit_trail(self):
        from origin.core.bootstrap import boot
        from origin.core.ids import HUMAN

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "origin.json"
            boot(storage_path=path)
            path.write_text("garbage", encoding="utf-8")
            system = boot(storage_path=path)

            watcher = system.nucleus._units[system.services["watcher"]]
            kinds = [e.get("event") for e in watcher.arena["events"]]
            self.assertIn("store.recovery", kinds)
            ev = next(e for e in watcher.arena["events"] if e.get("event") == "store.recovery")
            self.assertEqual([x["kind"] for x in ev["recovery"]], ["snapshot.quarantined", "snapshot.restored_from_backup"])

            audit = system.nucleus.audit(system.guardian, HUMAN, limit=4096, kinds={"route"})
            self.assertTrue(any(r.get("verb") == "store.recovery" for r in audit))

            clean = boot()  # a clean boot reports nothing
            w2 = clean.nucleus._units[clean.services["watcher"]]
            self.assertNotIn("store.recovery", [e.get("event") for e in w2.arena["events"]])

    def test_restart_respawns_units_from_preferred_code_version(self):
        from origin.core.bootstrap import boot, CODE_OBJECT

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "origin.json"
            first = boot(storage_path=path)
            oid = CODE_OBJECT["flaky"]
            code = dict(first.store.read("human", oid, first.guardian).payload)
            code["params"] = {**code["params"], "fail_every": 99}
            v = first.store.append("human", oid, code, first.guardian, note="improved", step=50)
            first.store.prefer("human", oid, v.seq, first.guardian)

            second = boot(storage_path=path)
            flaky = second.nucleus._units[second.nucleus._names["flaky"]]
            self.assertEqual(flaky.code_seq, v.seq)
            self.assertEqual(flaky.arena["params"]["fail_every"], 99)
            # Services must be this boot's ids, never the stale ones carried in old code.
            self.assertEqual(flaky.arena["params"]["services"]["object_store"], second.services["object_store"])

            # Un-preferring (re-preferring seq 0) is the reversible path and must be honoured too.
            second.store.prefer("human", oid, 0, second.guardian)
            third = boot(storage_path=path)
            flaky3 = third.nucleus._units[third.nucleus._names["flaky"]]
            self.assertEqual(flaky3.code_seq, 0)

    def test_boot_with_storage_path_survives_restart(self):
        from origin.core.bootstrap import boot, TALLY
        from origin.main import main

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "origin.json"
            first = boot(storage_path=path)
            first.store.append("human", TALLY, {"count": 41}, first.guardian, note="persist me", step=99)

            second = boot(storage_path=path)  # must not collide on genesis objects
            self.assertEqual(second.store.read("human", TALLY, second.guardian).payload, {"count": 41})
            history = second.store.history("human", TALLY, second.guardian)
            self.assertEqual(len(history), 2)  # genesis not duplicated, nothing lost

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(main(["--status", "--storage", str(path)]), 0)
            self.assertIn(str(path), buffer.getvalue())

    def test_object_store_crash_matrix_never_loses_acknowledged_history(self):
        from unittest.mock import patch
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore
        import origin.core.objects as objmod

        class Crash(BaseException):
            pass

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-crash")
        real_fsync, real_replace, real_copy = os.fsync, os.replace, objmod.shutil.copy2

        def run(point):
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "objects.json"
                store = ObjectStore(validator, storage_path=path)
                obj = store.create("alice", "journal", {"t": 0}, cap, step=1)
                store.append("alice", obj.object_id, {"t": 1}, cap, step=2)
                calls = {"fsync": 0}

                def fsync(fd):
                    calls["fsync"] += 1
                    if point == "during_tmp_write":
                        # tmp left half-written
                        raise Crash()
                    return real_fsync(fd)

                def copy(*a, **k):
                    if point == "mid_backup":
                        raise Crash()
                    return real_copy(*a, **k)

                def replace(*a, **k):
                    if point == "before_replace":
                        raise Crash()
                    return real_replace(*a, **k)

                def flush_dir(*a, **k):
                    # "after_replace" is the window between the atomic rename and
                    # the directory flush that makes it durable. Injecting here by
                    # patching the flush seam keeps the test portable: counting
                    # fsync calls only works on POSIX, where the directory flush is
                    # an fsync, and silently skipped the case on Windows.
                    if point == "after_replace":
                        raise Crash()
                    return real_flush_dir(*a, **k)

                real_flush_dir = objmod._flush_directory
                with patch.object(objmod.os, "fsync", fsync), patch.object(objmod.shutil, "copy2", copy), \
                        patch.object(objmod.os, "replace", replace), \
                        patch.object(objmod, "_flush_directory", flush_dir):
                    with self.assertRaises(Crash):  # the injected crash must really fire
                        store.append("alice", obj.object_id, {"t": 2}, cap, step=3)
                reopened = ObjectStore(validator, storage_path=path)
                return reopened, obj.object_id

        for point in ("during_tmp_write", "mid_backup", "before_replace", "after_replace"):
            with self.subTest(point=point):
                reopened, oid = run(point)
                self.assertEqual(reopened.history_damage, [])
                # Everything acknowledged before the crash must survive.
                self.assertEqual(reopened.read("alice", oid, cap, seq=0).payload, {"t": 0})
                self.assertEqual(reopened.read("alice", oid, cap, seq=1).payload, {"t": 1})
                latest = reopened.read("alice", oid, cap).payload
                self.assertIn(latest, ({"t": 1}, {"t": 2}))
                if point == "after_replace":
                    self.assertEqual(latest, {"t": 2})

    def test_directory_flush_actually_runs_on_this_platform(self):
        """The rename must really be made durable, not quietly skipped.

        A directory flush used to be wrapped in `except OSError: pass`. On
        Windows `os.open(dir)` always raises PermissionError, so the guarantee
        was silently absent on the platform the project actually runs on.

        Asserting only "does not raise" is what let a second regression hide here:
        stray lines from another function were left in `_flush_directory`, which
        the Windows early-return masked and which raised `NameError` on POSIX.
        So this asserts the syscall actually happened, on both platform branches.
        """
        from unittest.mock import patch
        import origin.core.objects as objmod

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "sentinel.txt"
            target.write_text("durable", encoding="utf-8")
            objmod._flush_directory(Path(tmpdir))  # must not raise on any platform

        # The POSIX branch cannot run natively on Windows, so drive it with the
        # directory primitives supplied. This is the branch that was silently
        # broken and is the one that has never been exercised on the author's
        # machine.
        calls: list[tuple] = []
        with patch.object(objmod.os, "open", lambda *a: 42), \
                patch.object(objmod.os, "fsync", lambda fd: calls.append(("fsync", fd))), \
                patch.object(objmod.os, "close", lambda fd: calls.append(("close", fd))), \
                patch.object(objmod.os, "name", "posix"):
            objmod._flush_directory(Path("/tmp/does-not-need-to-exist"))

        self.assertIn(("fsync", 42), calls, "the POSIX branch must actually fsync the directory")
        self.assertIn(("close", 42), calls, "the directory handle must be closed, or it leaks")

    def test_json_default_never_silently_returns_none(self):
        """A payload the encoder cannot handle must not be written as null.

        `_json_default` is `json.dump`'s last resort. Returning None there makes
        the snapshot disagree with memory: the version was acknowledged as durable,
        the caller was told so, and the file on disk says `null`. That is the exact
        failure this store exists to prevent, produced by its own plumbing.
        """
        import json
        from origin.core.objects import _json_default

        class Payload:
            def __init__(self):
                self.note = "real"
                self.count = 7

        self.assertEqual(_json_default(Payload()), {"note": "real", "count": 7})
        self.assertEqual(_json_default({3, 1, 2}), [1, 2, 3])
        # A bare object has no __dict__; it must still produce its repr, never None.
        self.assertIsInstance(_json_default(object()), str)

        # And end to end: a payload object survives a real write/reload cycle.
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-native")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "objects.json"
            store = ObjectStore(validator, storage_path=path)
            obj = store.create("human", "thing", {"note": Payload()}, cap, step=1)
            reopened = ObjectStore(validator, storage_path=path)
            payload = reopened.read("human", obj.object_id, cap).payload
            self.assertEqual(payload, {"note": {"note": "real", "count": 7}})

            # The snapshot is keyed by object id at the top level.
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            stored = on_disk[obj.object_id]["versions"][0]["payload"]
            self.assertIsNotNone(stored["note"], "an acknowledged payload must not serialise to null")
            # The payload object became a real dict on disk, not null. (json.dump
            # sorts keys, so this compares parsed content rather than ordering.)
            self.assertEqual(stored["note"], {"note": "real", "count": 7})

    def test_failed_directory_flush_is_reported_not_swallowed(self):
        """A weakened durability guarantee has to reach the operator.

        The acknowledged version still survives -- the rename is atomic and the
        file contents were fsynced -- so this is a weaker promise, not data loss.
        Silently swallowing it would violate invariant 7: nothing the human has
        been told exists may be silently lost.
        """
        from unittest.mock import patch
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore
        import origin.core.objects as objmod

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-degraded")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "objects.json"
            store = ObjectStore(validator, storage_path=path)

            with patch.object(objmod, "_flush_directory", side_effect=OSError("no flush here")):
                obj = store.create("alice", "journal", {"t": 0}, cap, step=1)

            degraded = [e for e in store.recovery_events if e["kind"] == "durability.degraded"]
            self.assertEqual(len(degraded), 1, store.recovery_events)
            self.assertEqual(degraded[0]["reason"], "directory_flush_failed")

            # The data itself must still be intact and still readable.
            reopened = ObjectStore(validator, storage_path=path)
            self.assertEqual(reopened.read("alice", obj.object_id, cap).payload, {"t": 0})

    def test_sweep_is_a_dry_run_until_told_otherwise(self):
        """Automatic retention must not be triggerable by accident.

        Section 3 states tiering as policy, but a policy that quietly deletes
        durable history is worse than no policy. So `sweep` previews by default
        and only reclaims when explicitly applied.
        """
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-sweep")

        store = ObjectStore(validator)
        obj = store.create("alice", "journal", {"t": 0}, cap, step=1)
        for t in range(1, 12):
            store.append("alice", obj.object_id, {"t": t}, cap, step=t + 1)
        store.pin("alice", obj.object_id, 0, cap)
        store.prefer("alice", obj.object_id, 1, cap)

        preview = store.sweep("alice", cap, keep_recent=3, min_versions=5)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["versions_reclaimed"], 0)
        self.assertEqual(preview["objects_swept"], 0)
        self.assertEqual(len(preview["candidates"]), 1, "the long history should be a candidate")
        # Nothing touched: every payload is still readable.
        self.assertEqual(store.read("alice", obj.object_id, cap, seq=0).payload, {"t": 0})
        self.assertEqual(store.read("alice", obj.object_id, cap, seq=2).payload, {"t": 2})

    def test_sweep_applies_tiered_policy_and_spares_protected_versions(self):
        """Applying the sweep must respect pins and the preferred version.

        Section 3: pins are never auto-removed, and the preferred version is the
        one a restart respawns from -- reclaiming it would break recovery.
        Metadata must survive even when payloads do not (invariant 7).
        """
        from origin.core.capability import Capability
        from origin.core.objects import CompactedError, ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-sweep-apply")

        store = ObjectStore(validator)
        obj = store.create("alice", "journal", {"t": 0}, cap, step=1)
        for t in range(1, 12):
            store.append("alice", obj.object_id, {"t": t}, cap, step=t + 1)
        store.pin("alice", obj.object_id, 0, cap)
        store.prefer("alice", obj.object_id, 2, cap)

        result = store.sweep("alice", cap, keep_recent=3, min_versions=5, dry_run=False)
        self.assertEqual(result["objects_swept"], 1)
        self.assertGreater(result["versions_reclaimed"], 0)

        # The three most recent stay at full fidelity.
        self.assertEqual(store.read("alice", obj.object_id, cap, seq=11).payload, {"t": 11})
        # The pinned version survives.
        self.assertEqual(store.read("alice", obj.object_id, cap, seq=0).payload, {"t": 0})
        # The preferred version survives -- reading it must not raise.
        self.assertEqual(store.read("alice", obj.object_id, cap, seq=2).payload, {"t": 2})

        # Something was actually reclaimed.
        with self.assertRaises(CompactedError):
            store.read("alice", obj.object_id, cap, seq=5)

        # But its metadata was kept forever.
        history = {v["seq"]: v for v in store.history("alice", obj.object_id, cap)}
        self.assertIn(5, history)
        self.assertEqual(history[5]["author"], "alice")
        self.assertTrue(history[5]["acked"])

    def test_sweep_leaves_short_histories_alone(self):
        """A short history is usually worth more whole, and saves nothing."""
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-sweep-short")

        store = ObjectStore(validator)
        obj = store.create("alice", "note", {"t": 0}, cap, step=1)
        for t in range(1, 4):
            store.append("alice", obj.object_id, {"t": t}, cap, step=t + 1)

        report = store.sweep("alice", cap, keep_recent=1, min_versions=32, dry_run=False)
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["objects_swept"], 0)
        self.assertEqual(store.read("alice", obj.object_id, cap, seq=0).payload, {"t": 0})

    def test_confirming_an_ambiguous_intent_does_not_execute_it(self):
        """An unresolved ambiguity is not confirmable.

        Confirming used to execute the plan with its target still unset, which came
        back as "REFUSED — no APPEND Capability for this target". That is a
        capability error standing in for "you never picked one", so the human was
        told they lacked authority for a choice they had not been asked to make.
        """
        from origin.core.bootstrap import boot
        from origin.main import _handle

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            # "the" is deliberately ambiguous across the seeded photo bindings.
            self.assertTrue(_handle(system, "count the photo"))
        first = buffer.getvalue()
        self.assertIn("not sure what", first)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertTrue(_handle(system, "confirm"))
        output = buffer.getvalue()
        self.assertIn("still ambiguous", output)
        # Crucially: no capability refusal masquerading as the answer.
        self.assertNotIn("REFUSED", output)
        self.assertNotIn("no APPEND Capability", output)

    def test_studio_renders_a_canvas_grouped_by_kind(self):
        """Section 8's Studio: a spatial view of living Objects.

        Asserted on behaviour that matters rather than exact glyphs: every Object
        appears, kinds cluster into adjacent cells, and a second render keeps the
        same assignment so the view does not reshuffle under the reader.
        """
        from origin.core.bootstrap import boot
        from origin.main import _handle

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertTrue(_handle(system, "/studio"))
        canvas = buffer.getvalue()
        self.assertIn("Origin Studio", canvas)

        studio_id = system.services["studio"]
        layout = dict(system.nucleus._arenas[studio_id]["layout"])
        self.assertTrue(layout, "the canvas should have placed Objects")

        # Kinds cluster: two mailboxes must be adjacent, not scattered.
        cells = {oid: (r, c) for oid, (r, c) in layout.items()}
        by_kind: dict[str, list[tuple[int, int]]] = {}
        for oid, cell in cells.items():
            kind = system.store._objects[oid].kind
            by_kind.setdefault(kind, []).append(cell)
        # Same-kind Objects must land in the same band, so reading down a column
        # keeps you within one kind. Adjacent rows is the real property.
        mailboxes = sorted(by_kind["mailbox"])
        self.assertLessEqual(mailboxes[1][0] - mailboxes[0][0], 1, mailboxes)

        # Stable across renders.
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _handle(system, "/studio")
        self.assertEqual(system.nucleus._arenas[studio_id]["layout"], layout)

    def test_studio_holds_no_mutating_authority(self):
        """A canvas that draws everything must not also be able to change it.

        This is the constraint that keeps section 8's Studio a *view*. If it could
        append or spawn it would be the most powerful Unit in the runtime, and
        acting through it would bypass the propose-then-confirm loop entirely.
        """
        from origin.core.bootstrap import boot
        from origin.core.capability import Right

        system = boot()
        studio_id = system.services["studio"]
        held = set()
        for rec in system.nucleus._caps.values():
            if studio_id in rec.holders:
                held |= rec.rights

        self.assertIn(Right.AUDIT, held)
        self.assertIn(Right.SEND, held)
        for forbidden in (Right.APPEND, Right.READ, Right.PIN, Right.HISTORY,
                          Right.GRANT, Right.REVOKE, Right.SPAWN, Right.KILL,
                          Right.FREEZE, Right.BIND, Right.GUARDIAN):
            self.assertNotIn(forbidden, held, f"studio must not hold {forbidden}")

    def test_studio_is_an_ordinary_unit(self):
        """Section 8: both human interfaces are ordinary Units that may evolve.

        So the Studio must not be in the constitutional boot set -- a canvas that
        cannot be killed is a canvas that cannot be redesigned.
        """
        from origin.core.bootstrap import boot
        from origin.core.constitution import BOOT_ORDER, is_boot_unit

        self.assertNotIn("studio", BOOT_ORDER)
        self.assertFalse(is_boot_unit("studio"))
        system = boot()
        self.assertIn("studio", system.services)

    def test_retention_unit_reclaims_history_without_being_asked(self):
        """Section 3's tiering policy must actually run, unattended.

        Until the Retention Unit existed the policy only ran when a human typed
        `/sweep apply`, so in practice it never ran. This asserts the automatic
        path: appends happen, the store ticks the Retention Unit, and payloads are
        reclaimed with no operator involvement.
        """
        from origin.core.bootstrap import boot, TALLY
        from origin.core.ids import HUMAN
        from origin.message import Message

        system = boot(retention_min_versions=5, retention_keep_recent=2)
        store_id = system.services["object_store"]
        retention_id = system.services["retention"]

        for t in range(1, 14):
            system.nucleus.send_message(
                Message(HUMAN, store_id, "object.append",
                        {"object_id": TALLY, "payload": {"count": t}}, caps=(system.guardian,))
            )
        system.nucleus.schedule()

        obj = system.store._objects[TALLY]
        self.assertTrue(obj.compacted, "retention should have reclaimed something")
        # Current state is intact and the newest version survives at full fidelity.
        self.assertEqual(system.store.read(HUMAN, TALLY, system.guardian).payload, {"count": 13})
        self.assertEqual(
            system.store.read(HUMAN, TALLY, system.guardian, seq=obj.latest_seq).payload, {"count": 13}
        )
        # Reclaimed payloads are gone but their metadata is retained (invariant 7).
        history = {v["seq"]: v for v in system.store.history(HUMAN, TALLY, system.guardian)}
        self.assertTrue(history[0]["compacted"])
        self.assertEqual(history[0]["author"], HUMAN)
        # And it happened through the Unit, which recorded what it did.
        log = system.nucleus._arenas[retention_id]["log"]
        self.assertTrue(any(e.get("versions_reclaimed") for e in log), log)

    def test_retention_is_dry_run_unless_apply_is_set(self):
        """Guards the polarity of the `apply` flag.

        `apply` maps to sweep's `dry_run` parameter, inverted. Getting that backwards
        would make an automated sweep silently reclaim nothing while reporting
        success -- a failure that looks like success, which is the worst kind.
        """
        from origin.core.bootstrap import boot, TALLY
        from origin.core.ids import HUMAN
        from origin.message import Message

        system = boot(retention_min_versions=5, retention_keep_recent=2)
        store_id = system.services["object_store"]
        for t in range(1, 14):
            system.nucleus.send_message(
                Message(HUMAN, store_id, "object.append",
                        {"object_id": TALLY, "payload": {"count": t}}, caps=(system.guardian,))
            )
        system.nucleus.schedule()

        # Enabled by default and it really did reclaim.
        self.assertTrue(system.store._objects[TALLY].compacted)

        # Disabled: appends must leave history untouched.
        quiet = boot(retention_enabled=False, retention_min_versions=5, retention_keep_recent=2)
        for t in range(1, 14):
            quiet.nucleus.send_message(
                Message(HUMAN, quiet.services["object_store"], "object.append",
                        {"object_id": TALLY, "payload": {"count": t}}, caps=(quiet.guardian,))
            )
        quiet.nucleus.schedule()
        self.assertEqual(quiet.store._objects[TALLY].compacted, set())

    def test_retention_holds_no_authority_beyond_reclaiming(self):
        """Section 5: authority exists only as explicit, narrow Capabilities.

        The Retention Unit may reclaim payloads and report on itself. It must not
        be able to inspect the system, mint authority, kill Units, or spawn them --
        a housekeeping Unit with any of those is a much larger blast radius than
        its job requires.
        """
        from origin.core.bootstrap import boot
        from origin.core.capability import Right

        system = boot()
        retention_id = system.services["retention"]
        held = set()
        for rec in system.nucleus._caps.values():
            if retention_id in rec.holders:
                held |= rec.rights

        self.assertIn(Right.PIN, held)
        self.assertIn(Right.SEND, held)
        for forbidden in (Right.AUDIT, Right.GRANT, Right.REVOKE, Right.KILL, Right.FREEZE,
                          Right.SPAWN, Right.GUARDIAN):
            self.assertNotIn(forbidden, held, f"retention must not hold {forbidden}")

    def test_retention_is_an_ordinary_unit_not_a_boot_unit(self):
        """It must be killable, replaceable, and absent from the Constitution's set.

        Section 9 fixes the boot set as the Units the system cannot run without.
        Housekeeping is not that: if this Unit dies the system still works, history
        just stops being reclaimed. Growing BOOT_ORDER would make it
        unkillable-by-accident.
        """
        from origin.core.bootstrap import boot
        from origin.core.constitution import BOOT_ORDER, is_boot_unit

        self.assertNotIn("retention", BOOT_ORDER)
        self.assertFalse(is_boot_unit("retention"))
        system = boot()
        self.assertIn("retention", system.services)


class TestAuthorityBoundaries(unittest.TestCase):
    """Phase 4: the trust boundaries, asserted as behaviour rather than prose.

    Each test here is an attack that must fail. They are written adversarially on
    purpose: the value of a security claim is entirely in whether it holds when
    someone tries to break it, and a test that only exercises the happy path proves
    nothing about the guarantee.
    """

    def _boot(self):
        from origin.core.bootstrap import boot, TALLY
        return boot(), TALLY

    # --- escalation ---------------------------------------------------------

    def test_a_delegate_cannot_widen_its_own_grant(self):
        """Attenuation must hold in every scope, or delegation is just a slow grant.

        The Improver holds a token scoped to `code` Objects. If it could mint itself
        an unrestricted APPEND, kind scoping would be decorative and the "cannot
        rewrite data" guarantee would rest on the Unit's good behaviour.
        """
        from origin.core.capability import Capability, CapabilityError, Right
        from origin.core.bootstrap import boot

        system = boot()
        improver = system.services["improver"]
        narrow = next(
            r for r in system.nucleus._caps.values()
            if improver in r.holders and r.target_kind == "code"
        )
        with self.assertRaises((CapabilityError, Exception)):
            system.nucleus.mint(
                (Right.READ, Right.APPEND), None, improver, improver,
                authority=Capability(narrow.cap_id), target_kind=None,
            )

    def test_only_the_human_can_mint_reserved_rights(self):
        """GUARDIAN, GRANT and REVOKE are authority over authority.

        If a delegate could mint them, "Improvers cannot self-grant" would be a
        policy rather than a fact, and one confused Unit could hand out permanent
        power that the human never agreed to.
        """
        from origin.core.capability import Right
        from origin.core.bootstrap import boot
        from origin.core.constitution import InvariantViolation

        system = boot()
        for right in (Right.GUARDIAN, Right.GRANT, Right.REVOKE):
            with self.subTest(right=right):
                with self.assertRaises(InvariantViolation):
                    system.nucleus.mint((right,), None, "some_unit", "some_unit",
                                        authority=system.guardian)

    # --- possession vs delegation ------------------------------------------

    def test_presenting_another_principals_token_is_refused(self):
        """Presenting a Capability must not delegate it.

        Without this, any service could accumulate ambient authority as a side
        effect of being talked to, and attaching the Guardian to a Unit-directed
        Message would hand that Unit the human's escape hatch.
        """
        from origin.core.capability import Capability, CapabilityError, Right
        from origin.core.bootstrap import boot, TALLY
        from origin.core.ids import HUMAN
        from origin.message import Message

        system, tally = self._boot()
        console = system.services["console"]
        improver = system.services["improver"]
        store = system.services["object_store"]

        cap = system.nucleus.mint((Right.READ,), tally, HUMAN, console, authority=system.guardian)
        with self.assertRaises(CapabilityError):
            system.nucleus.send_message(
                Message(improver, store, "object.read", {"object_id": tally},
                        caps=(Capability(cap.cap_id),))
            )

    # --- liveness -----------------------------------------------------------

    def test_revocation_kills_copies_already_held(self):
        """Revocation must be immediate and total, including for stale handles.

        A Unit may have stored the handle long ago. If a revoked token still worked
        through an old copy, revocation would be advisory rather than effective.
        """
        from origin.core.capability import Capability, CapabilityError, Right
        from origin.core.ids import HUMAN

        system, tally = self._boot()
        unit = system.services["improver"]
        cap = system.nucleus.mint((Right.READ,), tally, HUMAN, unit, authority=system.guardian)

        stale_copy = Capability(cap.cap_id)  # as if saved in an arena long ago
        system.nucleus.validate(stale_copy, Right.READ, tally, unit)

        system.nucleus.revoke(cap, by=HUMAN, reason="probe", authority=system.guardian)
        with self.assertRaises(CapabilityError):
            system.nucleus.validate(stale_copy, Right.READ, tally, unit)

    def test_expired_capability_stops_working(self):
        """Section 5 prefers temporary authority; that only means something if it dies."""
        from origin.core.capability import Capability, CapabilityError, Right
        from origin.core.ids import HUMAN

        system, tally = self._boot()
        unit = system.services["improver"]
        cap = system.nucleus.mint((Right.READ,), tally, HUMAN, unit,
                                  expires_in=1, authority=system.guardian)
        handle = Capability(cap.cap_id)
        system.nucleus.validate(handle, Right.READ, tally, unit)

        system.nucleus._step += 5
        with self.assertRaises(CapabilityError):
            system.nucleus.validate(handle, Right.READ, tally, unit)

    def test_forged_capability_is_refused(self):
        """An unissued id must not work, however plausible it looks."""
        from origin.core.capability import Capability, CapabilityError, Right

        system, tally = self._boot()
        with self.assertRaises(CapabilityError):
            system.nucleus.validate(Capability("cap_not_issued_anywhere"), Right.READ, tally, "human")

    # --- authority re-homing ------------------------------------------------

    def test_authority_cannot_be_inherited_across_kinds(self):
        """A replacement inherits its predecessor's tokens only when it is the same kind.

        Otherwise spawning a harmless-looking Unit of another kind would be a way to
        collect a Watcher's KILL.
        """
        from origin.core.bootstrap import boot

        system = boot()
        watcher = system.services["watcher"]
        imposter = system.nucleus.spawn("human", "photo", "imposter", "photo",
                                        authority=system.guardian)
        moved = system.nucleus._rehome(system.nucleus._units[watcher], imposter)
        self.assertEqual(moved, [], "a photo Unit must not inherit the Watcher's authority")

    def test_a_running_units_authority_is_never_moved_behind_its_back(self):
        """Re-homing is for Units that can no longer act for themselves."""
        from origin.core.bootstrap import boot

        system = boot()
        first = system.services["watcher"]
        second = system.nucleus.spawn("human", "watcher", "watcher2", "watcher",
                                      authority=system.guardian)
        moved = system.nucleus._rehome(system.nucleus._units[first], second)
        self.assertEqual(moved, [], "a sleeping predecessor's authority is still its own")

    def test_a_frozen_units_authority_does_transfer_to_its_replacement(self):
        """The positive case, or containment would strand authority permanently.

        Without this, a contained Unit keeps the only token reaching its data and
        its replacement is born unable to do its job.
        """
        from origin.core.bootstrap import boot
        from origin.core.capability import Right

        system = boot()
        first = system.services["watcher"]
        second = system.nucleus.spawn("human", "watcher", "watcher2", "watcher",
                                      authority=system.guardian)
        system.nucleus.freeze(first, by="nucleus", reason="probe")

        moved = system.nucleus._rehome(system.nucleus._units[first], second)
        self.assertTrue(moved, "a frozen Unit's authority should reach its replacement")
        rights = set()
        for handle in moved:
            rights |= system.nucleus._caps[handle.cap_id].rights
        self.assertIn(Right.KILL, rights, "the Watcher's replacement needs its KILL")

    # --- the seal -----------------------------------------------------------

    def test_the_sealed_core_refuses_to_mint_without_a_grant(self):
        """Sealing must be real, not decorative.

        Section 2 says the Nucleus drops nearly all power after bootstrap. If minting
        still worked without a token, that would be theatre.
        """
        from origin.core.capability import CapabilityError, Right
        from origin.core.ids import HUMAN
        from origin.core.constitution import InvariantViolation

        system, tally = self._boot()
        self.assertTrue(system.nucleus.sealed)
        with self.assertRaises(InvariantViolation):
            system.nucleus.mint((Right.READ,), tally, HUMAN, "human", authority=None)

    def test_rejected_authority_attempts_reach_the_audit_trail(self):
        """Section 8: nothing the human would want to know may be silent.

        A refusal that leaves no trace is a refusal the operator cannot investigate.
        """
        from origin.core.capability import Capability, CapabilityError, Right

        system, tally = self._boot()
        before = len([e for e in system.nucleus._audit if e.kind == "capability.rejected"])
        with self.assertRaises(CapabilityError):
            system.nucleus.validate(Capability("cap_bogus"), Right.READ, tally, "human")
        after = [e for e in system.nucleus._audit if e.kind == "capability.rejected"]
        self.assertEqual(len(after), before + 1, "the rejection must be recorded")

        reasons = [e.detail.get("reason") for e in after[-1:]]
        self.assertIn("unknown token", reasons)


class TestVersionedIntents(unittest.TestCase):
    """Phase 3: history, undo and restore, reachable by plain language.

    The point of these verbs is that they are not a special "undo" feature. They
    are the versioned substrate the Improver and restart already rely on, exposed
    to the human through the same propose-then-confirm loop as everything else.
    """

    def _system(self):
        from origin.core.bootstrap import boot, BEACH_PHOTO
        return boot(), BEACH_PHOTO

    def _run(self, system, lines: list[str]) -> str:
        from origin.main import _handle
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            for line in lines:
                _handle(system, line)
        return buffer.getvalue()

    def test_history_undo_and_restore_end_to_end(self):
        """The full arc: edit, inspect history, undo, then actually restore."""
        system, photo = self._system()

        out = self._run(system, ["brighten the beach photo by 20", "confirm"])
        self.assertIn("brightened by 20", out)

        out = self._run(system, ["history the beach photo", "confirm"])
        self.assertIn("2 versions", out)
        self.assertIn("genesis", out)

        # Undo reads the earlier version without changing anything.
        out = self._run(system, ["undo the beach photo", "confirm"])
        self.assertIn("undo: seq 0", out)
        self.assertIn("brightness: 50", out)

        # Restore moves the preferred pointer, so the effective version is seq 0.
        out = self._run(system, ["restore version 0 of the beach photo", "confirm"])
        self.assertIn("preferred version", out)
        # Restore moves a pointer; the superseded version is still there, which is
        # what makes it reversible rather than destructive.
        self.assertEqual(system.store._objects[photo].preferred, 0)
        self.assertEqual(len(system.store._objects[photo].versions), 2)

        out = self._run(system, ["show the beach photo", "confirm"])
        self.assertIn("brightness: 50", out)

    def test_undo_costs_no_authority_over_the_current_state(self):
        """Undo must not quietly become a write.

        Section 3 makes history the mechanism for reversal, so undo only ever *reads*
        an earlier version. If it could write, "undo" would be a destructive
        operation wearing a reassuring name.
        """
        from origin.units.naming import INTENT_VERBS

        by_action = {v.action: v for v in INTENT_VERBS}
        from origin.core.capability import Right

        undo_rights = set(by_action["undo"].rights)
        self.assertIn(Right.READ, undo_rights)
        self.assertNotIn(Right.APPEND, undo_rights)
        self.assertNotIn(Right.PIN, undo_rights)

        system, photo = self._system()
        self._run(system, ["brighten the beach photo by 20", "confirm"])
        before = len(system.store._objects[photo].versions)
        self._run(system, ["undo the beach photo", "confirm"])
        self.assertEqual(len(system.store._objects[photo].versions), before,
                         "undo must not append a version")

    def test_undo_refuses_a_version_that_does_not_exist(self):
        """Refuse rather than clamp.

        Silently returning the oldest version for "undo to version 9" of a
        two-version Object would be inventing history, and the human would have no
        way to tell it apart from a real answer.
        """
        system, _ = self._system()
        self._run(system, ["brighten the beach photo by 20", "confirm"])
        out = self._run(system, ["undo to version 9 of the beach photo", "confirm"])
        self.assertIn("no version", out)
        self.assertIn("seq 0 to 1", out)

    def test_restore_and_undo_are_distinct_actions(self):
        """"restore" listed as an undo synonym made restore report itself as an undo.

        They are different: undo reads an earlier version, restore moves the
        preferred pointer. Conflating them meant the human was told one thing and
        the system did another.
        """
        from origin.units.naming import INTENT_VERBS

        undo = next(v for v in INTENT_VERBS if v.action == "undo")
        restore = next(v for v in INTENT_VERBS if v.action == "restore")
        self.assertNotIn("restore", undo.synonyms)
        self.assertNotIn("undo", restore.synonyms)

    def test_ordinal_and_positional_version_references(self):
        """The phrasings a person actually types, not just "version N"."""
        from origin.units.naming import _parse

        def first_step(text):
            plan = _parse(text, None, lambda p, k: (None, 0.0, []))
            self.assertTrue(plan["steps"], text)
            return plan["steps"][0]

        cases = {
            "restore the last version of the beach photo": ("the beach photo", "last"),
            "restore version 2 of the beach photo": ("the beach photo", "2"),
            "undo the second version of the beach photo": ("the beach photo", "second"),
            "go back to version 1 of the beach photo": ("the beach photo", "1"),
        }
        for text, (target, seq) in cases.items():
            with self.subTest(text=text):
                step = first_step(text)
                self.assertEqual(step["target_phrase"], target)
                self.assertEqual(step["seq_reference"], seq)

    def test_version_reference_does_not_swallow_the_target(self):
        """Regression: "go back to version 2 of X" used to resolve an empty target.

        Two failures compounded. The "to ..." was read as a recipient, and the
        sequence verb's own words ("go back") were left in the target phrase, so the
        intent failed to resolve rather than resolving wrongly.
        """
        from origin.units.naming import _parse

        plan = _parse("go back to version 2 of the beach photo", None, lambda p, k: (None, 0.0, []))
        step = plan["steps"][0]
        self.assertEqual(step["target_phrase"], "the beach photo")
        self.assertEqual(step["seq_reference"], "2")
        self.assertIsNone(step["recipient_phrase"])

    def test_retention_unit_reclaims_history_without_being_asked(self):
        """Section 3's tiering policy must actually run, unattended.

        Until the Retention Unit existed the policy only ran when a human typed
        `/sweep apply`, so in practice it never ran. This asserts the automatic
        path: appends happen, the store ticks the Retention Unit, and payloads are
        reclaimed with no operator involvement.
        """
        from origin.core.bootstrap import boot, TALLY
        from origin.core.ids import HUMAN
        from origin.message import Message

        system = boot(retention_min_versions=5, retention_keep_recent=2)
        store_id = system.services["object_store"]
        retention_id = system.services["retention"]

        for t in range(1, 14):
            system.nucleus.send_message(
                Message(HUMAN, store_id, "object.append",
                        {"object_id": TALLY, "payload": {"count": t}}, caps=(system.guardian,))
            )
        system.nucleus.schedule()

        obj = system.store._objects[TALLY]
        self.assertTrue(obj.compacted, "retention should have reclaimed something")
        # Current state is intact and the newest version survives at full fidelity.
        self.assertEqual(system.store.read(HUMAN, TALLY, system.guardian).payload, {"count": 13})
        self.assertEqual(
            system.store.read(HUMAN, TALLY, system.guardian, seq=obj.latest_seq).payload, {"count": 13}
        )
        # Reclaimed payloads are gone but their metadata is retained (invariant 7).
        history = {v["seq"]: v for v in system.store.history(HUMAN, TALLY, system.guardian)}
        self.assertTrue(history[0]["compacted"])
        self.assertEqual(history[0]["author"], HUMAN)
        # And it happened through the Unit, which recorded what it did.
        log = system.nucleus._arenas[retention_id]["log"]
        self.assertTrue(any(e.get("versions_reclaimed") for e in log), log)

    def test_retention_is_dry_run_unless_apply_is_set(self):
        """Guards the polarity of the `apply` flag.

        `apply` maps to sweep's `dry_run` parameter, inverted. Getting that backwards
        would make an automated sweep silently reclaim nothing while reporting
        success -- a failure that looks like success, which is the worst kind.
        """
        from origin.core.bootstrap import boot, TALLY
        from origin.core.ids import HUMAN
        from origin.message import Message

        system = boot(retention_min_versions=5, retention_keep_recent=2)
        store_id = system.services["object_store"]
        for t in range(1, 14):
            system.nucleus.send_message(
                Message(HUMAN, store_id, "object.append",
                        {"object_id": TALLY, "payload": {"count": t}}, caps=(system.guardian,))
            )
        system.nucleus.schedule()

        # Enabled by default and it really did reclaim.
        self.assertTrue(system.store._objects[TALLY].compacted)

        # Disabled: appends must leave history untouched.
        quiet = boot(retention_enabled=False, retention_min_versions=5, retention_keep_recent=2)
        for t in range(1, 14):
            quiet.nucleus.send_message(
                Message(HUMAN, quiet.services["object_store"], "object.append",
                        {"object_id": TALLY, "payload": {"count": t}}, caps=(quiet.guardian,))
            )
        quiet.nucleus.schedule()
        self.assertEqual(quiet.store._objects[TALLY].compacted, set())

    def test_retention_holds_no_authority_beyond_reclaiming(self):
        """Section 5: authority exists only as explicit, narrow Capabilities.

        The Retention Unit may reclaim payloads and report on itself. It must not
        be able to inspect the system, mint authority, kill Units, or spawn them --
        a housekeeping Unit with any of those is a much larger blast radius than
        its job requires.
        """
        from origin.core.bootstrap import boot
        from origin.core.capability import Right

        system = boot()
        retention_id = system.services["retention"]
        held = set()
        for rec in system.nucleus._caps.values():
            if retention_id in rec.holders:
                held |= rec.rights

        self.assertIn(Right.PIN, held)
        self.assertIn(Right.SEND, held)
        for forbidden in (Right.AUDIT, Right.GRANT, Right.REVOKE, Right.KILL, Right.FREEZE,
                          Right.SPAWN, Right.GUARDIAN):
            self.assertNotIn(forbidden, held, f"retention must not hold {forbidden}")

    def test_retention_is_an_ordinary_unit_not_a_boot_unit(self):
        """It must be killable, replaceable, and absent from the Constitution's set.

        Section 9 fixes the boot set as the Units the system cannot run without.
        Housekeeping is not that: if this Unit dies the system still works, history
        just stops being reclaimed. Growing BOOT_ORDER would make it
        unkillable-by-accident.
        """
        from origin.core.bootstrap import boot
        from origin.core.constitution import BOOT_ORDER, is_boot_unit

        self.assertNotIn("retention", BOOT_ORDER)
        self.assertFalse(is_boot_unit("retention"))
        system = boot()
        self.assertIn("retention", system.services)

    def test_kind_scoped_token_covers_any_object_of_that_kind(self):
        """A kind scope is about the kind, not a list of ids.

        This is what makes one token correct for code Objects a Unit has never
        seen. It was the reason the Improver previously needed a token minted per
        known code Object, which meant a newly created Unit's code was unreachable.
        """
        from origin.core.capability import Right
        from origin.core.bootstrap import boot

        system = boot()
        improver = system.services["improver"]

        def holds(right, kind):
            return system.nucleus.find_capability(improver, right, "obj_anything", kind) is not None

        self.assertTrue(holds(Right.READ, "code"))
        self.assertTrue(holds(Right.APPEND, "code"))
        self.assertFalse(holds(Right.READ, "photo"))
        self.assertFalse(holds(Right.READ, "counter"))
        self.assertFalse(holds(Right.READ, None))

    def test_improver_cannot_rewrite_a_data_object(self):
        """The limitation this closes, asserted as behaviour rather than prose.

        Section 7 protected the Improver by making every change an append, so a bad
        improvement was a bad version rather than bad state. Kind scoping is the
        stronger guarantee: the token is refused outright on a data Object.
        """
        from origin.core.capability import Capability, CapabilityError, Right
        from origin.core.bootstrap import boot, BEACH_PHOTO, CODE_OBJECT

        system = boot()
        improver = system.services["improver"]
        code_oid = CODE_OBJECT["photo"]
        record = next(
            r for r in system.nucleus._caps.values()
            if improver in r.holders and r.target_kind == "code"
        )
        cap = Capability(record.cap_id)

        # Code it may touch.
        system.nucleus.validate(cap, Right.APPEND, code_oid, improver, "code")
        # Data it may not, even though it holds a valid, unrevoked APPEND token.
        with self.assertRaises(CapabilityError) as caught:
            system.nucleus.validate(cap, Right.APPEND, BEACH_PHOTO, improver, "photo")
        self.assertIn("does not grant", str(caught.exception))

        # And an unknown kind fails closed rather than widening the token.
        with self.assertRaises(CapabilityError):
            system.nucleus.validate(cap, Right.APPEND, BEACH_PHOTO, improver, None)

    def test_kind_scoped_grant_cannot_be_laundered_back_to_namespace_wide(self):
        """Attenuation stays one-directional for kind scope, as it is for target.

        Without this a delegate restricted to code Objects could mint itself an
        unrestricted token and the whole mechanism would be decorative.
        """
        from origin.core.capability import Right
        from origin.core.bootstrap import boot

        system = boot()
        console = system.services["console"]
        guardian = system.guardian

        # A delegate holding a kind-scoped GRANT.
        narrow = system.nucleus.mint(
            (Right.GRANT,), None, "human", console, authority=guardian, target_kind="code"
        )
        # Narrowing further is allowed.
        system.nucleus.mint(
            (Right.READ,), None, console, console, authority=narrow, target_kind="code"
        )
        # Widening back out is not.
        with self.assertRaises(Exception):
            system.nucleus.mint((Right.READ,), None, console, console, authority=narrow, target_kind=None)

    def test_sweep_needs_pin_authority(self):
        """Compaction is gated on PIN because retention policy is a human decision."""
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-no-sweep")

        store = ObjectStore(validator)
        obj = store.create("alice", "journal", {"t": 0}, cap, step=1)
        for t in range(1, 12):
            store.append("alice", obj.object_id, {"t": t}, cap, step=t + 1)

        with self.assertRaises(PermissionError):
            store.sweep("alice", None, keep_recent=3, min_versions=5)

    def test_object_store_falls_back_to_backup_only_when_it_loses_nothing(self):
        import json
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-fallback")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "objects.json"
            bak = Path(tmpdir) / "objects.json.bak"
            first = ObjectStore(validator, storage_path=path)
            obj = first.create("alice", "journal", {"t": 0}, cap, step=1)
            first.append("alice", obj.object_id, {"t": 1}, cap, step=2)
            first.append("alice", obj.object_id, {"t": 2}, cap, step=3)
            good = json.loads(path.read_text())

            # Backup holds the full history; main lost a version -> backup is a safe superset.
            bak.write_text(json.dumps(good))
            damaged = json.loads(path.read_text())
            damaged[obj.object_id]["versions"].pop(1)
            path.write_text(json.dumps(damaged))
            recovered = ObjectStore(validator, storage_path=path)
            self.assertEqual(recovered.history_damage, [])
            self.assertEqual(recovered.read("alice", obj.object_id, cap, seq=1).payload, {"t": 1})
            self.assertIn("snapshot.restored_from_backup", [e["kind"] for e in recovered.recovery_events])

            # Backup is older than main (lacks v2): falling back would lose data, so keep main and report.
            older = json.loads(json.dumps(good))
            older[obj.object_id]["versions"].pop(2)
            bak.write_text(json.dumps(older))
            path.write_text(json.dumps(damaged))
            kept = ObjectStore(validator, storage_path=path)
            self.assertEqual([d["kind"] for d in kept.history_damage], ["history.seq_gap"])
            self.assertEqual(kept.read("alice", obj.object_id, cap).payload, {"t": 2})

    def test_object_store_detects_history_damage_on_load(self):
        import json
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-integrity")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "objects.json"
            first = ObjectStore(validator, storage_path=path)
            obj = first.create("alice", "journal", {"t": 0}, cap, step=1)
            first.append("alice", obj.object_id, {"t": 1}, cap, step=2)
            first.append("alice", obj.object_id, {"t": 2}, cap, step=3)
            self.assertEqual(ObjectStore(validator, storage_path=path).history_damage, [])

            data = json.loads(path.read_text())
            data[obj.object_id]["versions"].pop(1)  # silently lose an acknowledged version
            data[obj.object_id]["pins"] = [9]  # pin pointing at nothing
            path.write_text(json.dumps(data))

            damaged = ObjectStore(validator, storage_path=path)
            kinds = sorted(d["kind"] for d in damaged.history_damage)
            self.assertEqual(kinds, ["history.dangling_pointer", "history.seq_gap"])
            self.assertIn("history.damaged", [e["kind"] for e in damaged.recovery_events])

    def test_object_store_reports_recovery_events(self):
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-events")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "objects.json"
            first = ObjectStore(validator, storage_path=path)
            self.assertEqual(first.recovery_events, [])
            first.create("alice", "journal", {"t": 1}, cap, step=1)
            first.create("alice", "journal", {"t": 2}, cap, step=2)
            path.write_text("garbage", encoding="utf-8")
            recovered = ObjectStore(validator, storage_path=path)
            kinds = [e["kind"] for e in recovered.recovery_events]
            self.assertEqual(kinds, ["snapshot.quarantined", "snapshot.restored_from_backup"])

    def test_object_store_recovers_from_corrupt_main_snapshot_via_backup(self):
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-corrupt-main")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "objects.json"
            first = ObjectStore(validator, storage_path=path)
            obj = first.create("alice", "journal", {"text": "one"}, cap, step=1)
            first.append("alice", obj.object_id, {"text": "two"}, cap, step=2)
            path.write_text("{ truncated", encoding="utf-8")

            recovered = ObjectStore(validator, storage_path=path)
            # Acknowledged history must not be silently dropped.
            self.assertEqual(recovered.read("alice", obj.object_id, cap, seq=0).payload, {"text": "one"})
            # The damaged file is preserved for inspection, never deleted.
            self.assertTrue(any(p.name.startswith("objects.json.corrupt") for p in Path(tmpdir).iterdir()))

    def test_object_store_persists_across_restarts(self):
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-persist")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "objects.json"
            first = ObjectStore(validator, storage_path=path)
            obj = first.create("alice", "journal", {"text": "first"}, cap, step=1)
            first.append("alice", obj.object_id, {"text": "second"}, cap, step=2)

            second = ObjectStore(validator, storage_path=path)
            loaded = second.read("alice", obj.object_id, cap, seq=0)
            self.assertEqual(loaded.payload, {"text": "first"})
            self.assertEqual(second.read("alice", obj.object_id, cap).payload, {"text": "second"})

    def test_object_store_persist_flushes_snapshot_before_replace(self):
        from unittest.mock import patch

        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-durable-write")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "objects.json"
            store = ObjectStore(validator, storage_path=path)

            with patch("origin.core.objects.os.fsync") as fsync, patch("origin.core.objects.os.replace") as replace:
                store.create("alice", "journal", {"text": "first"}, cap, step=1)

            fsync.assert_called()
            replace.assert_called_once()

    def test_object_store_recovers_from_stale_tmp_snapshot(self):
        import json

        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-recover")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "objects.json"
            tmp_path = path.with_suffix(".json.tmp")
            snapshot = {
                "obj-123": {
                    "kind": "journal",
                    "created_step": 1,
                    "versions": [{
                        "seq": 0,
                        "payload": {"text": "first"},
                        "author": "alice",
                        "step": 1,
                        "note": "genesis",
                        "acked": True,
                    }],
                    "pins": [],
                    "compacted": [],
                    "preferred": None,
                }
            }
            tmp_path.write_text(json.dumps(snapshot), encoding="utf-8")

            recovered = ObjectStore(validator, storage_path=path)
            self.assertEqual(recovered.read("alice", "obj-123", cap, seq=0).payload, {"text": "first"})
            self.assertTrue(path.exists())
            self.assertFalse(tmp_path.exists())

    def test_object_store_ignores_corrupt_tmp_snapshot_when_main_file_is_valid(self):
        import json

        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-corrupt-tmp")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "objects.json"
            tmp_path = path.with_suffix(".json.tmp")
            valid = {
                "obj-main": {
                    "kind": "journal",
                    "created_step": 1,
                    "versions": [{
                        "seq": 0,
                        "payload": {"text": "valid"},
                        "author": "alice",
                        "step": 1,
                        "note": "main",
                        "acked": True,
                    }],
                    "pins": [],
                    "compacted": [],
                    "preferred": None,
                }
            }
            path.write_text(json.dumps(valid), encoding="utf-8")
            tmp_path.write_text("{not valid json", encoding="utf-8")

            recovered = ObjectStore(validator, storage_path=path)
            self.assertEqual(recovered.read("alice", "obj-main", cap, seq=0).payload, {"text": "valid"})
            self.assertFalse(tmp_path.exists())

    def test_object_store_prefers_newer_tmp_snapshot_over_stale_main_file(self):
        import json

        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-prefer-tmp")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "objects.json"
            tmp_path = path.with_suffix(".json.tmp")

            stale = {
                "obj-old": {
                    "kind": "journal",
                    "created_step": 1,
                    "versions": [{
                        "seq": 0,
                        "payload": {"text": "stale"},
                        "author": "alice",
                        "step": 1,
                        "note": "old",
                        "acked": True,
                    }],
                    "pins": [],
                    "compacted": [],
                    "preferred": None,
                }
            }
            fresh = {
                "obj-new": {
                    "kind": "journal",
                    "created_step": 2,
                    "versions": [{
                        "seq": 0,
                        "payload": {"text": "fresh"},
                        "author": "alice",
                        "step": 2,
                        "note": "newer",
                        "acked": True,
                    }],
                    "pins": [],
                    "compacted": [],
                    "preferred": None,
                }
            }
            path.write_text(json.dumps(stale), encoding="utf-8")
            tmp_path.write_text(json.dumps(fresh), encoding="utf-8")

            recovered = ObjectStore(validator, storage_path=path)
            self.assertEqual(recovered.read("alice", "obj-new", cap, seq=0).payload, {"text": "fresh"})
            self.assertTrue(path.exists())
            self.assertFalse(tmp_path.exists())

    def test_compact_keeps_latest_version_when_keep_recent_is_zero(self):
        from origin.core.capability import Capability
        from origin.core.objects import CompactedError, ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-compact")
        store = ObjectStore(validator)

        obj = store.create("alice", "journal", {"text": "first"}, cap, step=1)
        store.append("alice", obj.object_id, {"text": "second"}, cap, step=2)
        store.append("alice", obj.object_id, {"text": "third"}, cap, step=3)

        result = store.compact("alice", obj.object_id, cap, keep_recent=0)
        self.assertEqual(result["reclaimed"], [0, 1])
        self.assertEqual(store.read("alice", obj.object_id, cap, seq=2).payload, {"text": "third"})
        # Current state is unchanged by compaction (it used to be replaced by a bookkeeping record).
        self.assertEqual(store.read("alice", obj.object_id, cap).payload, {"text": "third"})
        with self.assertRaises(CompactedError):
            store.read("alice", obj.object_id, cap, seq=0)

    def test_compact_reclaims_payloads_and_keeps_metadata_across_restart(self):
        import json

        from origin.core.capability import Capability
        from origin.core.objects import CompactedError, ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-reclaim")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "objects.json"
            store = ObjectStore(validator, storage_path=path)
            obj = store.create("alice", "journal", {"text": "first"}, cap, step=1)
            store.append("alice", obj.object_id, {"text": "second"}, cap, step=2)
            store.append("alice", obj.object_id, {"text": "third"}, cap, step=3)
            store.compact("alice", obj.object_id, cap, keep_recent=1)

            snapshot = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNone(snapshot[obj.object_id]["versions"][0]["payload"])
            self.assertIsNone(snapshot[obj.object_id]["versions"][1]["payload"])
            self.assertEqual(snapshot[obj.object_id]["versions"][2]["payload"], {"text": "third"})

            reopened = ObjectStore(validator, storage_path=path)
            with self.assertRaises(CompactedError):
                reopened.read("alice", obj.object_id, cap, seq=0)
            self.assertEqual(reopened.read("alice", obj.object_id, cap, seq=2).payload, {"text": "third"})

    def test_name_score_handles_punctuation_and_articles(self):
        from origin.units.naming import Binding, score

        binding = Binding(
            name="beach photo",
            target="obj-beach",
            kind="object",
            description="a coastal photograph",
            aliases=("beach pic",),
        )

        self.assertGreaterEqual(score(binding, "the beach photo."), 0.9)
        self.assertGreaterEqual(score(binding, "beach pic!"), 0.9)

    def test_parse_compound_intent_handles_punctuation_and_recipient(self):
        from origin.units.naming import _parse

        def resolve_phrase(phrase, kind):
            if kind == "object":
                if phrase in {"the beach photo", "beach photo", "it"}:
                    return "obj-beach", 1.0, []
                return None, 0.0, []
            if kind == "contact":
                if phrase == "david":
                    return "contact-david", 1.0, []
                return None, 0.0, []
            return None, 0.0, []

        plan = _parse("brighten the beach photo and send it to David.", {"object_id": "obj-beach"}, resolve_phrase)
        self.assertTrue(plan["resolved"])
        self.assertEqual(len(plan["steps"]), 2)
        self.assertEqual(plan["steps"][1]["recipient"], "contact-david")

    def test_parse_focus_allows_different_deictic_forms(self):
        from origin.units.naming import _parse

        def resolve_phrase(phrase, kind):
            if kind == "object" and phrase in {"beach photo", "it"}:
                return "obj-beach", 1.0, []
            if kind == "contact" and phrase == "david":
                return "contact-david", 1.0, []
            return None, 0.0, []

        plan = _parse("show the same photo", {"object_id": "obj-beach"}, resolve_phrase)
        self.assertTrue(plan["resolved"])
        self.assertEqual(plan["steps"][0]["target"], "obj-beach")

    def test_propose_choice_names_close_matches_for_ambiguity(self):
        from origin.units.console import _propose_choice

        class DummyCtx:
            def __init__(self):
                self.mem = {}
                self.last_text = ""

            def send(self, *args, **kwargs):
                payload = kwargs.get("payload") or (args[2] if len(args) > 2 else {})
                text = payload.get("text", "") if isinstance(payload, dict) else ""
                self.last_text = text
                return None

        ctx = DummyCtx()
        plan = {"steps": [{"target": None, "target_phrase": "beach photo"}]}
        amb = {
            "role": "target",
            "phrase": "beach photo",
            "candidates": [
                {"name": "beach photo", "kind": "object", "score": 0.91, "description": "coastal"},
                {"name": "beach portrait", "kind": "object", "score": 0.89, "description": "portrait"},
            ],
        }

        _propose_choice(ctx, plan, amb)
        output = ctx.last_text.lower()
        self.assertIn("closest matches", output)
        self.assertIn("choose by number", output)

    def test_agent_unit_tracks_lifecycle_state(self):
        from origin.core.bootstrap import boot
        from origin.core.unit import UnitContext
        from origin.message import Message

        system = boot()
        unit = system.nucleus.birth("agent", "demo-agent", "agent")
        ctx = UnitContext(unit, system.nucleus)

        unit.handler(ctx, Message(sender="console:live", recipient=unit.unit_id, verb="agent.start"))
        self.assertEqual(unit.arena.get("state"), "running")
        self.assertGreaterEqual(unit.arena.get("started_at", -1), unit.born_step)

        unit.handler(ctx, Message(sender="console:live", recipient=unit.unit_id, verb="agent.heartbeat"))
        self.assertEqual(unit.arena.get("state"), "running")
        self.assertGreaterEqual(unit.arena.get("last_heartbeat", -1), unit.arena.get("started_at", -1))

        unit.handler(ctx, Message(sender="console:live", recipient=unit.unit_id, verb="agent.stop"))
        self.assertEqual(unit.arena.get("state"), "stopped")

    def test_agent_registry_reports_live_monitoring_state(self):
        from origin.core.bootstrap import boot
        from origin.core.ids import HUMAN

        system = boot()
        agent = system.nucleus.birth("agent", "live-agent", "agent")

        agent.arena["state"] = "running"
        agent.arena["started_at"] = 12
        agent.arena["last_heartbeat"] = 15

        report = system.nucleus.describe(HUMAN, system.guardian, {"what": "agents"})
        self.assertEqual(report["agents"][0]["name"], "live-agent")
        self.assertEqual(report["agents"][0]["state"], "running")
        self.assertEqual(report["agents"][0]["started_at"], 12)
        self.assertEqual(report["agents"][0]["last_heartbeat"], 15)

    def test_agent_registry_reports_tools_and_scope(self):
        from origin.core.bootstrap import boot
        from origin.core.ids import HUMAN

        system = boot()
        agent = system.nucleus.birth("agent", "tool-agent", "agent")
        agent.arena["state"] = "running"
        agent.arena["started_at"] = 12
        agent.arena["last_heartbeat"] = 15
        agent.arena["tools"] = ["read", "write"]
        agent.arena["memory_scope"] = {"objects": ["obj-beach"], "max_bytes": 128}

        report = system.nucleus.describe(HUMAN, system.guardian, {"what": "agents"})
        target = next(item for item in report["agents"] if item["name"] == "tool-agent")
        self.assertEqual(target["tools"], ["read", "write"])
        self.assertEqual(target["memory_scope"]["max_bytes"], 128)

    def test_agent_failure_and_replacement_metadata_are_reported(self):
        from origin.core.bootstrap import boot
        from origin.core.ids import HUMAN
        from origin.core.unit import UnitContext
        from origin.message import Message

        system = boot()
        unit = system.nucleus.birth("agent", "flaky-agent", "agent")
        ctx = UnitContext(unit, system.nucleus)

        unit.handler(ctx, Message(sender="watcher:probe", recipient=unit.unit_id, verb="agent.fail", payload={"reason": "timeout"}))
        self.assertEqual(unit.arena.get("state"), "failed")
        self.assertEqual(unit.arena.get("failure_reason"), "timeout")
        self.assertIsNotNone(unit.arena.get("failed_at"))

        unit.arena["replaced_by"] = "replacement-agent"
        report = system.nucleus.describe(HUMAN, system.guardian, {"what": "agents"})
        target = next(item for item in report["agents"] if item["name"] == "flaky-agent")
        self.assertEqual(target["state"], "failed")
        self.assertEqual(target["failure_reason"], "timeout")
        self.assertEqual(target["replaced_by"], "replacement-agent")

    def test_agent_registry_reports_containment_over_self_report(self):
        # Section 8: a Unit the core has frozen is not "running" just because its
        # arena still says so. Containment is kernel truth and must never be
        # masked by the agent's own last self-report in /agents.
        from origin.core.bootstrap import boot
        from origin.core.ids import HUMAN, NUCLEUS

        system = boot()
        agent = system.nucleus.birth("agent", "contained-agent", "agent")
        agent.arena["state"] = "running"
        agent.arena["started_at"] = 12
        agent.arena["last_heartbeat"] = 15
        system.nucleus.freeze(agent.unit_id, by=NUCLEUS, reason="test containment")

        report = system.nucleus.describe(HUMAN, system.guardian, {"what": "agents"})
        target = next(item for item in report["agents"] if item["name"] == "contained-agent")
        self.assertEqual(target["state"], "frozen")

    def test_spawned_agent_can_reply_to_console_without_being_contained(self):
        # /spawn then /agent <name> start must leave the agent alive and answering.
        # A Unit born at runtime holds nothing, so without a reply channel its
        # first respond() presents no SEND token reaching the Console and the core
        # freezes it for capability abuse -- starting the agent would kill it.
        from origin.main import _to_console, boot

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _to_console(system, "/spawn agent worker")
            _to_console(system, "confirm")
            _to_console(system, "/agent worker start")
        output = buffer.getvalue()

        worker = system.nucleus._units[system.nucleus._names["worker"]]
        self.assertNotEqual(worker.state.value, "frozen")
        self.assertNotEqual(worker.state.value, "dead")
        self.assertGreaterEqual(len(worker.caps), 1)
        self.assertIn("worker is running", output)
        self.assertNotIn("left frozen", output)

    def test_status_mode_reports_runtime_summary(self):
        from origin.main import main

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = main(["--status"])

        self.assertEqual(result, 0)
        output = buffer.getvalue()
        self.assertIn("Origin", output)
        self.assertIn("status", output.lower())

    def test_status_reports_persistence_health(self):
        from origin.main import main

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = main(["--status"])

        self.assertEqual(result, 0)
        output = buffer.getvalue().lower()
        self.assertIn("durable", output)
        self.assertIn("compacted", output)

    def test_status_reports_temp_snapshot_health(self):
        from origin.main import _status, boot

        system = boot()
        store = getattr(system, "store", None)
        if store is not None and getattr(store, "_storage_path", None) is not None:
            tmp = store._storage_path.with_suffix(f"{store._storage_path.suffix}.tmp")
            tmp.write_text("{}", encoding="utf-8")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _status(system)

        output = buffer.getvalue().lower()
        self.assertIn("storage", output)
        self.assertIn("temp", output)
        self.assertIn("snapshot", output)

    def test_status_reports_current_focus(self):
        from origin.main import _status, boot

        system = boot()
        console = system.nucleus._units[system.console]
        console.arena["focus"] = {"name": "beach", "object_id": "obj-demo"}

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _status(system)

        output = buffer.getvalue().lower()
        self.assertIn("focus", output)
        self.assertIn("beach", output)

    def test_object_store_reports_retention_summary(self):
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-retention")
        store = ObjectStore(validator)

        obj = store.create("alice", "journal", {"text": "first"}, cap, step=1)
        store.append("alice", obj.object_id, {"text": "second"}, cap, step=2)
        store.append("alice", obj.object_id, {"text": "third"}, cap, step=3)
        store.pin("alice", obj.object_id, 2, cap)
        store.prefer("alice", obj.object_id, 0, cap)
        store.compact("alice", obj.object_id, cap, keep_recent=1)

        summary = store.retention_summary("alice", obj.object_id, cap)
        self.assertEqual(summary["total_versions"], 4)
        self.assertEqual(summary["durable"], 4)
        self.assertEqual(summary["pinned"], 1)
        # seq 0 is preferred, seq 2 is pinned, seq 3 is the newest: only seq 1 may be reclaimed.
        self.assertEqual(summary["compacted"], 1)
        self.assertEqual(summary["preferred"], 0)

    def test_objects_command_reports_compaction_state(self):
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore
        from origin.main import _handle, boot

        validator = lambda cap, right, target, holder, target_kind=None: None
        cap = Capability("cap-compact-view")
        store = ObjectStore(validator)
        obj = store.create("alice", "journal", {"text": "first"}, cap, step=1)
        store.append("alice", obj.object_id, {"text": "second"}, cap, step=2)
        store.append("alice", obj.object_id, {"text": "third"}, cap, step=3)
        store.compact("alice", obj.object_id, cap, keep_recent=1)

        system = boot()
        object_store = getattr(system, "store", None)
        object_store._objects = store._objects

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/objects")

        self.assertTrue(result)
        output = buffer.getvalue().lower()
        self.assertIn("compacted", output)

    def test_status_command_is_available_in_console(self):
        from origin.main import _handle, boot

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/status")

        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("Origin status", output)
        self.assertIn("units:", output)

    def test_console_accepts_status_command(self):
        from origin.main import _to_console, boot

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _to_console(system, "/status")

        output = buffer.getvalue().lower()
        self.assertIn("origin status", output)
        self.assertIn("units:", output)

    def test_console_status_reports_health_and_focus(self):
        from origin.main import _to_console, boot

        system = boot()
        storage = getattr(system.store, "_storage_path", None)
        if storage is not None:
            storage.with_suffix(f"{storage.suffix}.tmp").write_text("{}", encoding="utf-8")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _to_console(system, "/status")

        output = buffer.getvalue().lower()
        self.assertIn("durable versions", output)
        self.assertIn("temp snapshot", output)
        self.assertIn("focus", output)

    def test_console_log_lists_recent_transparency_events(self):
        from origin.main import _handle, boot

        system = boot()
        console = system.nucleus._units[system.console]
        console.arena["log"] = [
            {"event": "notice", "text": "boot completed", "at_step": 1},
            {"event": "notice", "text": "status checked", "at_step": 2},
        ]

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/log 2")

        self.assertTrue(result)
        output = buffer.getvalue().lower()
        self.assertIn("boot completed", output)
        self.assertIn("status checked", output)

    def test_units_command_is_available_in_console(self):
        from origin.main import _handle, boot

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/units")

        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("Origin units", output)
        self.assertIn("unit", output.lower())

    def test_agents_command_is_available_in_console(self):
        from origin.main import _to_console, boot

        system = boot()
        agent = system.nucleus.birth("agent", "live-agent", "agent")
        agent.arena["state"] = "running"
        agent.arena["started_at"] = 12
        agent.arena["last_heartbeat"] = 15

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _to_console(system, "/agents")

        output = buffer.getvalue()
        self.assertIn("Agents", output)
        self.assertIn("live-agent", output)
        self.assertNotIn("do not know", output.lower())

    def test_objects_command_is_available_in_console(self):
        from origin.main import _handle, boot

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/objects")

        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("Origin objects", output)
        self.assertIn("object", output.lower())

    def test_names_command_is_available_in_console(self):
        from origin.main import _handle, boot

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/names")

        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("Origin names", output)
        self.assertIn("binding", output.lower())

    def test_caps_command_is_available_in_console(self):
        from origin.main import _handle, boot

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/caps")

        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("Origin caps", output)
        self.assertIn("capability", output.lower())

    def test_caps_command_marks_revoked_capabilities(self):
        from origin.ids import HUMAN, NUCLEUS
        from origin.main import _handle, boot
        from origin.capability import Right

        system = boot()
        cap = system.nucleus.mint(
            (Right.READ,), "photo:beach", HUMAN, "alice", authority=system.guardian, label="beach read"
        )
        system.nucleus.revoke(cap, NUCLEUS, "security review")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _handle(system, "/caps")

        output = buffer.getvalue().lower()
        self.assertIn("revoked", output)
        self.assertIn("beach read", output.lower())
        self.assertIn("grantor", output)
        self.assertIn("reason=security review", output)

    def test_caps_can_be_filtered_by_holder_or_state(self):
        from origin.ids import HUMAN, NUCLEUS
        from origin.main import _handle, boot
        from origin.capability import Right

        system = boot()
        system.nucleus.mint(
            (Right.READ,), "photo:beach", HUMAN, "alice", authority=system.guardian, label="beach read"
        )
        cap = system.nucleus.mint(
            (Right.APPEND,), "mailbox:david", HUMAN, "bob", authority=system.guardian, label="mailbox append"
        )
        system.nucleus.revoke(cap, NUCLEUS, "revoke check")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _handle(system, "/caps alice")

        output = buffer.getvalue().lower()
        self.assertIn("beach read", output)
        self.assertNotIn("mailbox append", output)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _handle(system, "/caps revoked")

        output = buffer.getvalue().lower()
        self.assertIn("revoked", output)
        self.assertIn("revoke check", output)

    def test_caps_can_be_filtered_by_right_or_target(self):
        from origin.ids import HUMAN, NUCLEUS
        from origin.main import _handle, boot
        from origin.capability import Right

        system = boot()
        system.nucleus.mint(
            (Right.READ,), "photo:beach", HUMAN, "alice", authority=system.guardian, label="beach read"
        )
        cap = system.nucleus.mint(
            (Right.APPEND,), "mailbox:david", HUMAN, "bob", authority=system.guardian, label="mailbox append"
        )
        system.nucleus.revoke(cap, NUCLEUS, "cleanup")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _handle(system, "/caps read")

        output = buffer.getvalue().lower()
        self.assertIn("beach read", output)
        self.assertNotIn("mailbox append", output)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _handle(system, "/caps photo")

        output = buffer.getvalue().lower()
        self.assertIn("beach read", output)
        self.assertNotIn("mailbox append", output)

    def test_audit_command_is_available_in_console(self):
        from origin.main import _handle, boot

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/audit")

        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("Origin audit", output)
        self.assertIn("event", output.lower())

    def test_watcher_and_improver_reports_are_available_in_console(self):
        from origin.main import _handle, boot

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _handle(system, "/watcher")
            _handle(system, "/improver")

        output = buffer.getvalue().lower()
        self.assertIn("origin watcher", output)
        self.assertIn("events", output)
        self.assertIn("origin improver", output)
        self.assertIn("decisions", output)

    def test_messages_command_is_available_in_console(self):
        from origin.main import _handle, boot

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _handle(system, "/messages")

        output = buffer.getvalue().lower()
        self.assertIn("origin messages", output)
        self.assertIn("route", output)
        self.assertIn("sender", output)

    def test_powers_command_is_available_in_console(self):
        from origin.main import _handle, boot

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/powers")

        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("Origin powers", output)
        self.assertIn("power", output.lower())

    def test_history_command_is_available_in_console(self):
        from origin.main import _handle, boot

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/history")

        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("Origin history", output)
        self.assertIn("version", output.lower())

    def test_show_and_focus_commands_are_exposed_in_main(self):
        from origin.main import _focus, _handle, _show, boot

        system = boot()
        self.assertTrue(callable(_show))
        self.assertTrue(callable(_focus))

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/show beach photo")

        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("PROPOSED", output)
        self.assertIn("beach", output.lower())

    def test_grant_command_is_exposed_in_main(self):
        from origin.main import _grant, _handle, boot

        system = boot()
        self.assertTrue(callable(_grant))

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/grant READ beach photo")

        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("PROPOSED", output)
        self.assertIn("grant", output.lower())

    def test_freeze_command_is_exposed_in_main(self):
        from origin.main import _freeze, _handle, boot

        system = boot()
        self.assertTrue(callable(_freeze))

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/freeze flaky")

        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("PROPOSED", output)
        self.assertIn("freeze", output.lower())

    def test_kill_command_is_exposed_in_main(self):
        from origin.main import _handle, _kill, boot

        system = boot()
        self.assertTrue(callable(_kill))

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/kill flaky")

        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("PROPOSED", output)
        self.assertIn("kill", output.lower())

    def test_spawn_command_is_exposed_in_main(self):
        from origin.main import _handle, _spawn, boot

        system = boot()
        self.assertTrue(callable(_spawn))

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/spawn photo beach")

        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("PROPOSED", output)
        self.assertIn("spawn", output.lower())

    def test_spawn_confirm_sets_focus_on_new_unit(self):
        from origin.main import _handle, boot

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _handle(system, "/spawn photo beach")
            _handle(system, "confirm")

        output = buffer.getvalue().lower()
        self.assertIn("beach", output)
        self.assertIn("now refer", output)

    def test_self_heal_replacement_sets_focus_on_new_unit(self):
        from origin.main import _handle, boot

        system = boot()
        old_flaky = next(unit for unit in system.nucleus._units.values() if unit.name == "flaky")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _handle(system, "/work 3")

        output = buffer.getvalue().lower()
        console = system.nucleus._units[system.console]
        focus = console.arena.get("focus", {})

        self.assertIn("flaky", output)
        self.assertIn("now refer", output)
        self.assertEqual(focus.get("name"), "flaky")
        self.assertNotEqual(focus.get("object_id"), old_flaky.unit_id)

    def test_rollback_and_work_commands_are_exposed_in_main(self):
        from origin.main import _handle, _rollback, _work, boot

        system = boot()
        self.assertTrue(callable(_rollback))
        self.assertTrue(callable(_work))

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/rollback")
        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("usage: /rollback", output.lower())

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/work 1")
        self.assertTrue(result)
        output = buffer.getvalue()
        self.assertIn("work:", output.lower())

    def test_help_command_is_available_in_console(self):
        from origin.main import _handle, boot

        system = boot()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = _handle(system, "/help")

        self.assertTrue(result)
        output = buffer.getvalue()
        # Asserted against __phase__ rather than a literal, so the help text and
        # the phase label cannot drift apart again.
        self.assertIn(f"Origin — {__phase__}", output)
        self.assertIn("/status", output)
        self.assertIn("/log", output)
        self.assertIn("/watcher", output)
        self.assertIn("/improver", output)

    def test_cli_help(self):
        env = os.environ.copy()
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")

        result = subprocess.run(
            [sys.executable, "-m", "origin", "--help"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        # The captured pipe is decoded with the platform's default code page, so
        # the em-dash in __phase__ may arrive mangled. Assert on the ASCII-safe
        # part: the description must be built from the same label the banner uses.
        self.assertIn(__phase__.split(" ")[1], result.stdout)


class TestReclaimableReport(unittest.TestCase):
    """Phase 2.8b: compaction is a visible, deliberate choice."""

    def _store_with_history(self, versions: int = 7):
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        store = ObjectStore(lambda *a: None)
        cap = Capability("cap-reclaim")
        obj = store.create("alice", "journal", {"t": 0}, cap, step=1)
        for i in range(1, versions):
            store.append("alice", obj.object_id, {"t": i}, cap, step=i + 1)
        return store, cap, obj.object_id

    def test_report_predicts_exactly_what_compact_reclaims(self):
        store, cap, oid = self._store_with_history()
        store.pin("alice", oid, 2, cap)
        store.prefer("alice", oid, 1, cap)

        report = store.reclaimable_report("alice", cap, object_id=oid, keep_recent=2)
        result = store.compact("alice", oid, cap, keep_recent=2)

        self.assertEqual(report["reclaimable_seqs"], result["reclaimed"])
        self.assertEqual(report["reclaimable_versions"], len(result["reclaimed"]))
        # Pinned (2), preferred (1) and the two newest (5, 6) are protected.
        self.assertEqual(report["reclaimable_seqs"], [0, 3, 4])
        self.assertEqual(report["protected"]["pinned"], [2])
        self.assertEqual(report["protected"]["preferred"], 1)
        self.assertEqual(report["protected"]["recent"], [5, 6])
        self.assertEqual(report["keep_recent"], 2)
        self.assertGreater(report["reclaimable_bytes"], 0)

    def test_report_is_read_only(self):
        store, cap, oid = self._store_with_history()
        before = store.history("alice", oid, cap)

        store.reclaimable_report("alice", cap, object_id=oid)
        store.reclaimable_report("alice", cap)

        self.assertEqual(store.history("alice", oid, cap), before)
        self.assertEqual(store.retention_summary("alice", oid, cap)["compacted"], 0)

    def test_already_compacted_versions_are_not_reported_again(self):
        store, cap, oid = self._store_with_history()
        first = store.compact("alice", oid, cap, keep_recent=2)

        report = store.reclaimable_report("alice", cap, object_id=oid, keep_recent=2)

        # Compaction appends a marker version, which moves the recent window
        # forward by one, so a second pass may legitimately find one more. What
        # it must never do is offer something that was already reclaimed.
        self.assertFalse(set(report["reclaimable_seqs"]) & set(first["reclaimed"]))
        self.assertEqual(report["protected"]["already_compacted"], sorted(store._objects[oid].compacted))
        second = store.compact("alice", oid, cap, keep_recent=2)
        self.assertEqual(report["reclaimable_seqs"], second["reclaimed"])

    def test_keep_recent_is_floored_at_one_like_compact(self):
        store, cap, oid = self._store_with_history(versions=4)

        report = store.reclaimable_report("alice", cap, object_id=oid, keep_recent=0)

        self.assertEqual(report["keep_recent"], 1)
        self.assertNotIn(3, report["reclaimable_seqs"])  # the newest is never offered

    def test_store_wide_report_totals(self):
        store, cap, big = self._store_with_history(versions=8)
        small = store.create("alice", "note", {"x": 1}, cap, step=1).object_id

        report = store.reclaimable_report("alice", cap, keep_recent=3)
        totals = report["totals"]

        self.assertEqual(totals["objects"], 2)
        self.assertEqual(totals["objects_with_reclaimable"], 1)
        self.assertEqual(totals["reclaimable_versions"], 5)
        self.assertEqual(totals["total_versions"], 9)
        self.assertEqual(totals["reclaimable_bytes"], sum(r["reclaimable_bytes"] for r in report["objects"]))
        # Largest reclaimable object is listed first.
        self.assertEqual(report["objects"][0]["object_id"], big)
        self.assertEqual(next(r for r in report["objects"] if r["object_id"] == small)["reclaimable_versions"], 0)

    def test_report_requires_audit_authority(self):
        from origin.core.capability import Capability, Right
        from origin.core.objects import ObjectStore

        def validator(cap, right, target, holder, target_kind=None):
            if right is Right.AUDIT:
                raise PermissionError("no audit")

        store = ObjectStore(validator)
        cap = Capability("cap-no-audit")
        oid = store.create("alice", "journal", {"t": 0}, cap, step=1).object_id

        with self.assertRaises(PermissionError):
            store.reclaimable_report("alice", cap, object_id=oid)
        with self.assertRaises(PermissionError):
            store.reclaimable_report("alice", cap)
        with self.assertRaises(PermissionError):
            store.reclaimable_report("alice", None)

    def test_unknown_object_is_indistinguishable_miss(self):
        from origin.core.objects import ObjectNotFound

        store, cap, _ = self._store_with_history()
        with self.assertRaises(ObjectNotFound):
            store.reclaimable_report("alice", cap, object_id="obj-missing")

    def test_enumerate_now_exposes_compacted_seqs(self):
        store, cap, oid = self._store_with_history()
        store.compact("alice", oid, cap, keep_recent=2)

        row = next(r for r in store.enumerate("alice", cap) if r["object_id"] == oid)

        self.assertEqual(row["compacted"], sorted(store._objects[oid].compacted))
        self.assertTrue(row["compacted"])

    def test_object_store_unit_answers_reclaimable_verb(self):
        from types import SimpleNamespace

        from origin.units.object_store import object_store_handler

        store, cap, oid = self._store_with_history()
        replies = []
        ctx = SimpleNamespace(
            mem={"store": store},
            step=1,
            respond=lambda msg, verb, payload: replies.append((verb, payload)),
        )
        msg = SimpleNamespace(
            sender="alice", verb="object.reclaimable", payload={"object_id": oid, "keep_recent": 2}, caps=(cap,)
        )

        object_store_handler(ctx, msg)

        self.assertEqual(replies[0][0], "object.reclaimable")
        self.assertEqual(replies[0][1]["reclaimable_seqs"], [0, 1, 2, 3, 4])

        replies.clear()
        object_store_handler(ctx, SimpleNamespace(sender="alice", verb="object.reclaimable", payload={}, caps=(cap,)))
        self.assertIn("totals", replies[0][1])

    def test_reclaimable_command_and_status_line(self):
        from origin.core.ids import HUMAN
        from origin.main import _handle, boot

        system = boot()
        oid = system.store.create(
            HUMAN, "journal", {"t": 0}, system.guardian, step=1
        ).object_id
        for i in range(1, 8):
            system.store.append(HUMAN, oid, {"t": i}, system.guardian, step=i + 1)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertTrue(_handle(system, "/reclaimable 2"))
        output = buffer.getvalue()
        self.assertIn("Reclaimable history", output)
        self.assertIn("keeping the 2 most recent", output)
        self.assertIn("Nothing was compacted", output)
        self.assertEqual(system.store.retention_summary(HUMAN, oid, system.guardian)["compacted"], 0)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _handle(system, "/status")
        self.assertIn("reclaimable history:", buffer.getvalue())

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _handle(system, "/reclaimable nonsense")
        self.assertIn("Usage: /reclaimable", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
