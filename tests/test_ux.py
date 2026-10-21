import contextlib
import io
import json
import os
import pty
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from l4ctl import cli
from l4ctl.core import Engine, FixtureCloud, Planner, SafetyError, Store
from tests.fakes import FakeCloud, T
from tools.install_launcher import install


class UXTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cloud = FakeCloud()
        self.store = Store(self.temp.name, T)
        self.state = Planner(T, self.cloud).initial_state()
        self.store.save("state.json", self.state)

    def invoke(self, args, interactive=True, answers=()):
        output = io.StringIO()
        with patch("l4ctl.cli.ComputeCloud", return_value=self.cloud), patch("sys.stdin.isatty", return_value=interactive), patch("builtins.input", side_effect=list(answers)), contextlib.redirect_stdout(output):
            code = cli.main(["--state-dir", self.temp.name, *args])
        return code, output.getvalue()

    def snapshot(self):
        plan = Planner(T, self.cloud, self.state).plan("snapshot", snapshot="existing-backup")
        Engine(T, self.cloud, self.store, self.state).apply(plan)
        # Make this an external snapshot, not an already registered one.
        self.state["snapshots"].clear()
        self.store.save("state.json", self.state)
        self.cloud.calls.clear()

    def removed_target(self):
        vm = self.cloud.resources.pop(T.vm_path)
        disk_path = T.path("disks", self.state["boot"]["name"])
        disk = self.cloud.resources.pop(disk_path)
        self.state["removed"] = {"resources": [dict(kind=kind, **{k: item[k] for k in ("id", "name", "selfLink")})
            for kind, item in (("instances", vm), ("disks", disk))]}
        self.store.save("state.json", self.state)

    def test_removed_status_verifies_absence_and_has_no_restore_claim(self):
        self.removed_target()
        _, output = self.invoke(["status"])
        self.assertIn("REMOVED", output)
        self.assertIn("No retained restore snapshot", output)
        self.assertFalse(self.cloud.calls)

    def test_removed_target_cannot_start_or_allocate(self):
        self.removed_target()
        for args in (["up"], ["storage", "add", "job", "100"], ["snapshots", "import", "backup"]):
            with self.assertRaisesRegex(SafetyError, "permanently removed"):
                self.invoke(args, answers=["y"])
        self.assertFalse(self.cloud.calls)

    def test_removed_status_refuses_name_reuse(self):
        self.removed_target()
        self.cloud.resources[T.vm_path] = {"id": "9999"}
        with self.assertRaisesRegex(SafetyError, "exists again"):
            self.invoke(["status"])

    def test_default_up_prompts_then_applies(self):
        _, output = self.invoke(["up"], answers=["y"])
        self.assertIn("Plan:", output)
        self.assertEqual(len(self.cloud.calls), 1)
        self.assertEqual(self.cloud.resources[T.vm_path]["status"], "RUNNING")

    def test_up_cancellation_has_no_request_or_journal(self):
        _, output = self.invoke(["up"], answers=[""])
        self.assertIn("Cancelled", output)
        self.assertFalse(self.cloud.calls)
        self.assertIsNone(self.store.load("journal.json"))

    def test_explicit_plan_never_prompts_or_applies(self):
        _, output = self.invoke(["--plan", "up"])
        self.assertIn("PLAN ONLY", output)
        self.assertFalse(self.cloud.calls)

    def test_noninteractive_execute_still_does_not_apply(self):
        _, output = self.invoke(["--execute", "up"], interactive=False)
        self.assertIn("no terminal confirmation", output)
        self.assertFalse(self.cloud.calls)

    def test_stop_default_retains_every_disk(self):
        self.cloud.resources[T.vm_path]["status"] = "RUNNING"
        _, output = self.invoke(["stop"], answers=["y"])
        self.assertIn("keep all disks", output)
        self.assertEqual(len(self.cloud.calls), 1)
        self.assertTrue(self.cloud.resources[T.vm_path]["disks"])

    def test_park_generic_yes_does_not_approve_deletion(self):
        with self.assertRaisesRegex(SafetyError, "cancelled"):
            self.invoke(["park"], answers=["y", "yes"])
        self.assertEqual(len(self.cloud.calls), 1)  # snapshot only
        self.assertTrue(self.cloud.resources[T.vm_path]["disks"])

    def test_storage_add_is_a_readable_work_disk_plan(self):
        _, output = self.invoke(["--plan", "storage", "add", "large-job", "200"])
        self.assertIn("Allocate a new blank work disk", output)
        self.assertIn("200 GB", output)
        self.assertFalse(self.cloud.calls)

    def test_import_registers_only_locally_and_never_asserts_restore_test(self):
        self.snapshot()
        _, output = self.invoke(["snapshots", "import", "existing-backup"], answers=["y"])
        saved = self.store.load("state.json")["snapshots"]["existing-backup"]
        self.assertFalse(saved["restore_verified"])
        self.assertTrue(saved["removal_eligible"])
        self.assertIn("NOT been validated", output)
        self.assertFalse(self.cloud.calls)

    def test_import_wrong_source_id_fails_closed(self):
        self.snapshot()
        self.cloud.resources[T.path("snapshots", "existing-backup")]["sourceDiskId"] = "9999"
        with self.assertRaisesRegex(SafetyError, "exact name/ID"):
            self.invoke(["snapshots", "import", "existing-backup"])
        self.assertFalse(self.cloud.calls)

    def test_import_wrong_expected_snapshot_id_fails_closed(self):
        self.snapshot()
        with self.assertRaisesRegex(SafetyError, "numeric ID"):
            self.invoke(["--execute", "snapshots", "import", "existing-backup", "--id", "9999"])

    def test_stale_import_cannot_authorize_source_removal(self):
        self.snapshot()
        self.cloud.resources[T.vm_path]["lastStartTimestamp"] = "2026-10-22T10:00:00Z"
        snap = Planner(T, self.cloud, self.state).import_snapshot("existing-backup")
        self.assertFalse(snap["removal_eligible"])
        self.state["snapshots"][snap["name"]] = snap
        with self.assertRaisesRegex(SafetyError, "freshness"):
            Engine(T, self.cloud, self.store, self.state).verified_snapshot(snap["name"], self.state["boot"])

    def test_import_of_source_used_elsewhere_cannot_qualify_for_removal(self):
        self.snapshot()
        self.cloud.resources[T.path("disks", self.state["boot"]["name"])]["users"] = [T.path("instances", "other-vm")]
        snap = Planner(T, self.cloud, self.state).import_snapshot("existing-backup")
        self.assertFalse(snap["removal_eligible"])

    def test_vm_stopped_after_snapshot_cannot_qualify_import_for_removal(self):
        self.snapshot()
        self.cloud.resources[T.vm_path]["lastStopTimestamp"] = "2026-10-22T10:00:00Z"
        snap = Planner(T, self.cloud, self.state).import_snapshot("existing-backup")
        self.assertFalse(snap["removal_eligible"])

    def test_missing_stop_time_cannot_qualify_import_for_removal(self):
        self.snapshot()
        self.cloud.resources[T.vm_path].pop("lastStopTimestamp")
        snap = Planner(T, self.cloud, self.state).import_snapshot("existing-backup")
        self.assertFalse(snap["removal_eligible"])

    def test_snapshots_lists_provider_metadata(self):
        self.snapshot()
        _, output = self.invoke(["snapshots"])
        self.assertIn("existing-backup", output)
        self.assertIn("1.00 GiB stored", output)
        self.assertIn("unregistered", output)
        self.assertFalse(self.cloud.calls)

    def test_config_can_be_overridden_by_explicit_target(self):
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps({"project": "wrong-project", "zone": T.zone, "vm": T.vm}))
        _, output = self.invoke(["--config", str(config), "--project", T.project, "status"])
        self.assertIn(T.project, output)

    def test_offline_fixture_ignores_private_live_target_config(self):
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps({"project": "private-project", "zone": "different-zone-a", "vm": "private-vm"}))
        fixture = Path(__file__).resolve().parents[1] / "examples/stopped.json"
        _, output = self.invoke(["--config", str(config), "--fixture", str(fixture), "status"])
        self.assertIn(T.project, output)
        self.assertNotIn("private-project", output)


class LauncherAndSubprocessTests(unittest.TestCase):
    def test_fresh_shell_resolves_installed_launcher_and_offline_status(self):
        shell = shutil.which("zsh") or shutil.which("sh")
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            bin_dir = Path(folder) / "bin"
            bin_dir.mkdir()
            env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.defpath}
            with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()):
                install(bin_dir, python=sys.executable)
            result = subprocess.run([shell, "-c", 'command -v l4; l4 --help; l4 --fixture "$1" --state-dir "$2" status', "l4-test", str(repo / "examples/stopped.json"), str(Path(folder) / "state")], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(bin_dir / "l4"), result.stdout)
            self.assertIn("snapshots", result.stdout)
            self.assertIn("TERMINATED", result.stdout)

    def test_installer_refuses_unrelated_collision(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "l4"
            dest.write_text("unrelated command")
            with patch.dict(os.environ, {"PATH": folder}):
                with self.assertRaisesRegex(RuntimeError, "refusing"):
                    install(folder)
            self.assertEqual(dest.read_text(), "unrelated command")

    def test_subprocess_cli_cancels_and_applies_mocked_requests(self):
        repo = Path(__file__).resolve().parents[1]
        driver = '''import json, os, sys
from l4ctl import cli
from l4ctl.core import Planner, Store
from tests.fakes import FakeCloud, T
cloud=FakeCloud()
store=Store(os.environ["TEST_STATE"],T)
store.save("state.json",Planner(T,cloud).initial_state())
cli.ComputeCloud=lambda:cloud
code=cli.entry()
print("MOCK_REQUEST_COUNT="+str(len(cloud.calls)))
sys.exit(code)
'''
        with tempfile.TemporaryDirectory() as folder:
            for answer, count in (("\n", 0), ("y\n", 1)):
                master, slave = pty.openpty()
                try:
                    process = subprocess.Popen([sys.executable, "-c", driver, "--state-dir", folder, "up"], stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=repo, env={**os.environ, "TEST_STATE": folder})
                    os.close(slave)
                    slave = None
                    os.write(master, answer.encode())
                    output, errors = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, errors)
                    self.assertIn("Apply this plan?", output)
                    self.assertIn("MOCK_REQUEST_COUNT=" + str(count), output)
                finally:
                    os.close(master)
                    if slave is not None:
                        os.close(slave)

    def test_subprocess_recovery_after_lost_response_never_resends(self):
        repo = Path(__file__).resolve().parents[1]
        driver = '''import json, os, sys
from pathlib import Path
from l4ctl import cli
from l4ctl.core import Planner, Store
from tests.fakes import FakeCloud, T
cloud=FakeCloud()
root=Path(os.environ["TEST_STATE"])
resources=root/"fake-provider.json"
store=Store(root,T)
if resources.exists():
 cloud.resources=json.loads(resources.read_text())
else:
 store.save("state.json",Planner(T,cloud).initial_state())
 cloud.fail_after_mutation=True
cli.ComputeCloud=lambda:cloud
sys.stdin.isatty=lambda:True
code=cli.entry()
resources.write_text(json.dumps(cloud.resources))
print("MOCK_REQUEST_COUNT="+str(len(cloud.calls)))
sys.exit(code)
'''
        with tempfile.TemporaryDirectory() as folder:
            common = dict(input="y\n", capture_output=True, text=True, cwd=repo,
                          env={**os.environ, "TEST_STATE": folder}, timeout=30)
            first = subprocess.run([sys.executable, "-c", driver, "--state-dir", folder, "snapshot", "--name", "saved"], **common)
            self.assertEqual(first.returncode, 1)
            self.assertIn("MOCK_REQUEST_COUNT=1", first.stdout)
            second = subprocess.run([sys.executable, "-c", driver, "--state-dir", folder, "recover"], **common)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("MOCK_REQUEST_COUNT=0", second.stdout)
            self.assertEqual(Store(folder, T).load("journal.json")["status"], "completed")
