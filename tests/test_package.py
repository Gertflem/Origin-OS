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

        validator = lambda cap, right, target, holder: None
        store = ObjectStore(validator)
        cap = Capability("cap-durability")

        obj = store.create("alice", "journal", {"text": "first"}, cap, step=1)
        self.assertTrue(obj.versions[0].acked)

        v2 = store.append("alice", obj.object_id, {"text": "second"}, cap, step=2)
        self.assertTrue(v2.acked)

        store.acknowledge("alice", obj.object_id, v2.seq, cap, acked=False, note="not yet durable")
        self.assertFalse(store.read("alice", obj.object_id, cap, seq=v2.seq).acked)
        self.assertEqual(len(store.durable_versions("alice", obj.object_id, cap)), 1)

    def test_object_store_persists_across_restarts(self):
        from origin.core.capability import Capability
        from origin.core.objects import ObjectStore

        validator = lambda cap, right, target, holder: None
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

    def test_compact_keeps_latest_version_when_keep_recent_is_zero(self):
        from origin.core.capability import Capability
        from origin.core.objects import CompactedError, ObjectStore

        validator = lambda cap, right, target, holder: None
        cap = Capability("cap-compact")
        store = ObjectStore(validator)

        obj = store.create("alice", "journal", {"text": "first"}, cap, step=1)
        store.append("alice", obj.object_id, {"text": "second"}, cap, step=2)
        store.append("alice", obj.object_id, {"text": "third"}, cap, step=3)

        result = store.compact("alice", obj.object_id, cap, keep_recent=0)
        self.assertEqual(result["reclaimed"], [0, 1])
        self.assertEqual(store.read("alice", obj.object_id, cap, seq=2).payload, {"text": "third"})
        self.assertEqual(store.read("alice", obj.object_id, cap).payload["kind"], "compaction")
        with self.assertRaises(CompactedError):
            store.read("alice", obj.object_id, cap, seq=0)

    def test_compact_reclaims_payloads_and_keeps_metadata_across_restart(self):
        import json

        from origin.core.capability import Capability
        from origin.core.objects import CompactedError, ObjectStore

        validator = lambda cap, right, target, holder: None
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
        self.assertIn("Origin — Phase 1", output)
        self.assertIn("/status", output)

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
        self.assertIn("Origin Phase 1", result.stdout)


if __name__ == "__main__":
    unittest.main()
