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

    def test_status_mode_reports_runtime_summary(self):
        from origin.main import main

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = main(["--status"])

        self.assertEqual(result, 0)
        output = buffer.getvalue()
        self.assertIn("Origin", output)
        self.assertIn("status", output.lower())

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
