import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from l4ctl.cli import main, quote_rate
from l4ctl.core import (Cancelled, ComputeCloud, Engine, FixtureCloud, Planner,
                        SafetyError, Store, Target, Uncertain, relative)
from tests.fakes import FakeCloud, T, inventory


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cloud = FakeCloud()
        self.store = Store(self.temp.name, T)
        self.state = Planner(T, self.cloud).initial_state()
        self.store.save("state.json", self.state)

    def plan(self, action, **options):
        return Planner(T, self.cloud, self.state).plan(action, **options)

    def run_plan(self, action, confirm=True, **options):
        plan = self.plan(action, **options)
        Engine(T, self.cloud, self.store, self.state, interval=0,
               confirm=lambda phrase: confirm).apply(plan)
        return plan

    def test_stop_only_retains_every_disk_and_snapshot(self):
        self.cloud.resources[T.vm_path]["status"] = "RUNNING"
        plan = self.run_plan("stop")
        self.assertEqual([s["kind"] for s in plan["steps"]], ["stop"])
        self.assertIn(T.path("disks", self.state["boot"]["name"]), self.cloud.resources)
        self.assertEqual(self.state["snapshots"], {})

    def test_stop_already_stopped_is_idempotent(self):
        self.run_plan("stop")
        self.run_plan("stop")
        self.assertFalse(self.cloud.calls)

    def test_full_park_restore_preserves_vm_and_original_attachment(self):
        original = copy.deepcopy(self.cloud.resources[T.vm_path])
        old_id = self.state["boot"]["id"]
        self.run_plan("park", snapshot="boot-saved")
        self.assertTrue(self.state["boot"]["parked"])
        self.assertEqual(self.cloud.resources[T.vm_path]["disks"], [])
        self.assertNotIn(T.path("disks", self.state["boot"]["name"]), self.cloud.resources)
        self.assertEqual(self.cloud.resources[T.vm_path]["machineType"], original["machineType"])
        self.run_plan("up")
        self.assertNotEqual(self.state["boot"]["id"], old_id)
        self.assertEqual(self.state["boot"]["sizeGb"], "70")
        self.assertEqual(self.cloud.resources[T.vm_path]["id"], "1001")
        attachment = self.cloud.resources[T.vm_path]["disks"][0]
        self.assertEqual(attachment["deviceName"], "persistent-disk-0")
        self.assertTrue(attachment["boot"])
        self.assertTrue(attachment["autoDelete"])
        self.assertIn("boot-saved", self.state["snapshots"])

    def test_cancel_after_snapshot_keeps_source_attached(self):
        with self.assertRaises(Cancelled):
            self.run_plan("park", confirm=False)
        self.assertEqual([c[0] for c in self.cloud.calls], ["POST"])
        self.assertTrue(self.cloud.resources[T.vm_path]["disks"])
        self.assertEqual(self.store.load("journal.json")["status"], "cancelled")

    def test_bad_snapshot_source_prevents_detach_and_delete(self):
        self.cloud.bad_snapshot_source = True
        with self.assertRaises(SafetyError):
            self.run_plan("park")
        self.assertEqual(len(self.cloud.calls), 1)
        self.assertTrue(self.cloud.resources[T.vm_path]["disks"])

    def test_snapshot_ready_but_wrong_id_blocks_deletion(self):
        self.run_plan("snapshot", snapshot="saved")
        self.cloud.resources[T.path("snapshots", "saved")]["id"] = "9999"
        engine = Engine(T, self.cloud, self.store, self.state)
        with self.assertRaises(SafetyError):
            engine.verified_snapshot("saved", self.state["boot"])

    def test_vm_name_reuse_blocks_planning(self):
        self.cloud.resources[T.vm_path]["id"] = "9999"
        with self.assertRaisesRegex(SafetyError, "name was reused"):
            self.plan("up")

    def test_disk_name_reuse_before_request_blocks_it(self):
        plan = self.plan("park")
        self.cloud.resources[T.path("disks", self.state["boot"]["name"])]["id"] = "9999"
        with self.assertRaises(SafetyError):
            Engine(T, self.cloud, self.store, self.state).apply(plan)
        self.assertFalse(self.cloud.calls)

    def test_work_path_cannot_delete_boot_even_if_registry_tampered(self):
        self.state["work"][self.state["boot"]["name"]] = self.state["boot"]
        with self.assertRaisesRegex(SafetyError, "Boot disk"):
            self.plan("work-remove", disk=self.state["boot"]["name"])

    def test_work_disk_independent_park_restore(self):
        self.run_plan("work-create", disk="large-job", size=200)
        self.run_plan("work-attach", disk="large-job")
        self.run_plan("work-remove", disk="large-job", snapshot="work-saved")
        self.assertTrue(self.state["work"]["large-job"]["parked"])
        self.assertFalse(self.state["boot"]["parked"])
        self.run_plan("work-restore", disk="large-job")
        self.run_plan("work-attach", disk="large-job")
        self.assertTrue(self.cloud.resources[T.vm_path]["disks"][0]["boot"])
        self.assertFalse(self.cloud.resources[T.vm_path]["disks"][1]["autoDelete"])

    def test_independent_test_restore_and_cleanup(self):
        self.run_plan("snapshot", snapshot="saved")
        self.run_plan("test-restore", snapshot="saved", disk="restore-check")
        self.assertEqual(self.state["tests"]["restore-check"]["sizeGb"], "70")
        self.assertEqual(len(self.cloud.resources[T.vm_path]["disks"]), 1)
        self.run_plan("test-cleanup", disk="restore-check")
        self.assertNotIn(T.path("disks", "restore-check"), self.cloud.resources)
        self.assertIn(T.path("snapshots", "saved"), self.cloud.resources)

    def test_no_smaller_restore_or_shrink(self):
        with self.assertRaisesRegex(SafetyError, "shrink"):
            self.plan("resize", size=60)
        self.run_plan("resize", size=90)
        self.run_plan("park")
        plan = self.plan("up")
        self.assertEqual(plan["steps"][0]["size"], 90)

    def test_config_drift_fails_closed(self):
        self.cloud.resources[T.vm_path]["machineType"] = "changed"
        with self.assertRaisesRegex(SafetyError, "drifted"):
            self.run_plan("up")
        self.assertFalse(self.cloud.calls)

    def test_uncertain_creation_is_reconciled_once_without_resend(self):
        plan = self.plan("snapshot", snapshot="saved")
        self.cloud.fail_after_mutation = True
        with self.assertRaises(TimeoutError):
            Engine(T, self.cloud, self.store, self.state).apply(plan)
        self.assertEqual(plan["steps"][0]["phase"], "uncertain")
        self.cloud.fail_after_mutation = False
        Engine(T, self.cloud, self.store, self.state).apply(plan, recover=True)
        self.assertEqual(len(self.cloud.calls), 1)
        self.assertEqual(plan["status"], "completed")
        with self.assertRaisesRegex(SafetyError, "closed"):
            Engine(T, self.cloud, self.store, self.state).apply(plan, recover=True)
        self.assertEqual(len(self.cloud.calls), 1)

    def test_absent_uncertain_result_never_blindly_retried(self):
        plan = self.plan("snapshot")
        self.cloud.fail_before_mutation = True
        with self.assertRaises(TimeoutError):
            Engine(T, self.cloud, self.store, self.state).apply(plan)
        self.cloud.fail_before_mutation = False
        with self.assertRaises(SafetyError):
            Engine(T, self.cloud, self.store, self.state).apply(plan, recover=True)
        self.assertEqual(len(self.cloud.calls), 1)

    def test_error_operation_stops_without_later_steps(self):
        self.cloud.operation_error = True
        with self.assertRaisesRegex(SafetyError, "reported failure"):
            self.run_plan("park")
        self.assertEqual(len(self.cloud.calls), 1)
        self.assertEqual(self.store.load("journal.json")["status"], "failed")

    def test_confirmation_rechecks_stopped_state_before_detach(self):
        plan = self.plan("park")
        def confirm(phrase):
            self.cloud.resources[T.vm_path]["status"] = "RUNNING"
            return True
        with self.assertRaisesRegex(SafetyError, "stopped"):
            Engine(T, self.cloud, self.store, self.state, confirm=confirm).apply(plan)
        self.assertEqual(len(self.cloud.calls), 1)
        self.assertTrue(self.cloud.resources[T.vm_path]["disks"])

    def test_restart_since_backup_blocks_destructive_steps(self):
        plan = self.plan("park")
        def confirm(phrase):
            self.cloud.resources[T.vm_path]["lastStartTimestamp"] = "2026-10-21T01:00:00Z"
            return True
        with self.assertRaisesRegex(SafetyError, "restarted"):
            Engine(T, self.cloud, self.store, self.state, confirm=confirm).apply(plan)
        self.assertEqual(len(self.cloud.calls), 1)

    def test_source_reattached_since_backup_blocks_removal(self):
        plan = self.plan("park")
        def confirm(phrase):
            self.cloud.resources[T.path("disks", self.state["boot"]["name"])]["lastAttachTimestamp"] = "2026-10-21T01:00:00Z"
            return True
        with self.assertRaisesRegex(SafetyError, "reattached"):
            Engine(T, self.cloud, self.store, self.state, confirm=confirm).apply(plan)
        self.assertEqual(len(self.cloud.calls), 1)

    def test_park_repeated_after_completion_sends_no_requests(self):
        self.run_plan("park")
        count = len(self.cloud.calls)
        self.run_plan("park")
        self.assertEqual(len(self.cloud.calls), count)

    def test_uncertain_snapshot_recovery_keeps_original_freshness_baseline(self):
        plan = self.plan("snapshot", snapshot="saved")
        self.cloud.fail_after_mutation = True
        with self.assertRaises(TimeoutError):
            Engine(T, self.cloud, self.store, self.state).apply(plan)
        self.cloud.resources[T.vm_path]["lastStartTimestamp"] = "2026-10-21T01:00:00Z"
        self.cloud.fail_after_mutation = False
        engine = Engine(T, self.cloud, self.store, self.state)
        engine.apply(plan, recover=True)
        with self.assertRaisesRegex(SafetyError, "restarted"):
            engine.verified_snapshot("saved", self.state["boot"])

    def test_lost_delete_reply_does_not_delete_reused_disk_name(self):
        plan = self.plan("park", snapshot="saved")
        engine = Engine(T, self.cloud, self.store, self.state, confirm=lambda phrase: True)
        original_mutate = self.cloud.mutate
        def lost_delete(method, path, body, request_id):
            result = original_mutate(method, path, body, request_id)
            if method == "DELETE":
                raise TimeoutError("Lost deletion reply")
            return result
        with patch.object(self.cloud, "mutate", side_effect=lost_delete):
            with self.assertRaises(TimeoutError):
                engine.apply(plan)
        disk_path = T.path("disks", self.state["boot"]["name"])
        self.cloud.resources[disk_path] = {"id": "9999", "name": self.state["boot"]["name"], "selfLink": disk_path}
        before = len(self.cloud.calls)
        with self.assertRaisesRegex(SafetyError, "name was reused"):
            Engine(T, self.cloud, self.store, self.state).apply(plan, recover=True)
        self.assertEqual(len(self.cloud.calls), before)
        self.assertEqual(self.cloud.resources[disk_path]["id"], "9999")

    def test_wrong_source_snapshot_on_restored_disk_prevents_attach(self):
        self.run_plan("park", snapshot="saved")
        plan = self.plan("up")
        original_mutate = self.cloud.mutate
        def tamper(method, path, body, request_id):
            operation = original_mutate(method, path, body, request_id)
            if body and "sourceSnapshot" in body:
                self.cloud.resources[T.path("disks", body["name"])]["sourceSnapshotId"] = "9999"
            return operation
        before = len(self.cloud.calls)
        with patch.object(self.cloud, "mutate", side_effect=tamper):
            with self.assertRaisesRegex(SafetyError, "snapshot identity"):
                Engine(T, self.cloud, self.store, self.state).apply(plan)
        self.assertEqual(len(self.cloud.calls), before + 1)
        self.assertEqual(self.cloud.resources[T.vm_path]["disks"], [])

    def test_operation_timeout_keeps_known_operation_for_read_only_recovery(self):
        self.cloud.operation_running = True
        plan = self.plan("snapshot")
        engine = Engine(T, self.cloud, self.store, self.state, timeout=0, interval=0)
        with self.assertRaises(Uncertain):
            engine.apply(plan)
        self.assertIn("operation", plan["steps"][0])
        for obj in self.cloud.resources.values():
            if "/operations/" in obj.get("selfLink", ""):
                obj["status"] = "DONE"
        engine.apply(plan, recover=True)
        self.assertEqual(len(self.cloud.calls), 1)

    def test_crash_between_journal_and_local_effect_replays_idempotently(self):
        plan = self.plan("snapshot", snapshot="saved")
        engine = Engine(T, self.cloud, self.store, self.state)
        real_effect = engine.effect
        with patch.object(engine, "effect", side_effect=OSError("local write failed")):
            with self.assertRaises(OSError):
                engine.apply(plan)
        self.assertEqual(plan["steps"][0]["phase"], "done")
        engine.effect = real_effect
        engine.apply(plan, recover=True)
        self.assertEqual(len(self.cloud.calls), 1)
        self.assertIn("saved", self.state["snapshots"])

    def test_wrong_project_operation_is_uncertain_and_blocks(self):
        engine = Engine(T, self.cloud, self.store, self.state)
        with self.assertRaises(SafetyError):
            engine.operation_path({"selfLink": "projects/other/global/operations/op-1"})

    def test_snapshot_requires_stopped_vm(self):
        self.cloud.resources[T.vm_path]["status"] = "RUNNING"
        with self.assertRaisesRegex(SafetyError, "Stop first"):
            self.plan("snapshot")

    def test_second_confirmation_on_recovery_before_delete(self):
        plan = self.plan("park", snapshot="saved")
        engine = Engine(T, self.cloud, self.store, self.state, confirm=lambda phrase: True)
        original_guard = engine.guard
        def fail_at_delete(step):
            if step["kind"] == "delete":
                raise OSError("simulated crash before next request")
            original_guard(step)
        with patch.object(engine, "guard", side_effect=fail_at_delete):
            with self.assertRaises(OSError):
                engine.apply(plan)
        with self.assertRaises(Cancelled):
            Engine(T, self.cloud, self.store, self.state, confirm=lambda phrase: False).apply(plan, recover=True)
        self.assertIn(T.path("disks", self.state["boot"]["name"]), self.cloud.resources)

    def test_lock_excludes_second_process(self):
        with self.store.lock():
            with self.assertRaisesRegex(SafetyError, "lock"):
                with Store(self.temp.name, T).lock():
                    self.fail("Second lock acquired")

    def test_state_contains_no_metadata_or_ip_credentials(self):
        saved = (self.store.root / "state.json").read_text()
        self.assertNotIn("DO-NOT-PERSIST", saved)
        self.assertNotIn("192.0.2.1", saved)
        self.assertEqual((self.store.root / "state.json").stat().st_mode & 0o777, 0o600)

    def test_up_running_is_idempotent(self):
        self.cloud.resources[T.vm_path]["status"] = "RUNNING"
        self.run_plan("up")
        self.assertFalse(self.cloud.calls)

    def test_up_resumes_suspended_vm(self):
        self.cloud.resources[T.vm_path]["status"] = "SUSPENDED"
        plan = self.run_plan("up")
        self.assertEqual([s["kind"] for s in plan["steps"]], ["resume"])

    def test_attachment_private_fields_are_not_persisted(self):
        self.cloud.resources[T.vm_path]["disks"][0]["diskEncryptionKey"] = {"rawKey": "secret"}
        state = Planner(T, self.cloud).initial_state()
        self.assertNotIn("diskEncryptionKey", state["boot"]["attachment"])

    def test_enrollment_uses_selected_boot_identity_instead_of_fixed_example_name(self):
        vm = self.cloud.resources[T.vm_path]
        original_path = vm["disks"][0]["source"]
        disk = self.cloud.resources.pop(original_path)
        disk.update(name="existing-boot", selfLink=T.path("disks", "existing-boot"))
        self.cloud.resources[disk["selfLink"]] = disk
        vm["disks"][0]["source"] = disk["selfLink"]
        state = Planner(T, self.cloud).initial_state()
        self.assertEqual(state["boot"]["name"], "existing-boot")
        self.assertEqual(state["boot"]["id"], "2001")


class CLITests(unittest.TestCase):
    def test_fixture_previews_never_call_token_or_network(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = Path(root) / "inventory.json"
            fixture.write_text(json.dumps(inventory()))
            commands = [["status"], ["init"], ["up"], ["stop", "--after", "keep"], ["park"],
                        ["storage"], ["snapshot"], ["work-disk", "create", "job", "--size", "200"]]
            with patch("subprocess.run", side_effect=AssertionError("No credentials or subprocess")), \
                 patch("urllib.request.urlopen", side_effect=AssertionError("No network")):
                for command in commands:
                    with self.subTest(command=command), redirect_stdout(io.StringIO()):
                        self.assertEqual(main(["--fixture", str(fixture), "--state-dir", root, *command]), 0)
            self.assertFalse(list(Path(root).rglob("journal.json")))
            self.assertFalse(list(Path(root).rglob("state.json")))

    def test_fixture_cannot_execute(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = Path(root) / "inventory.json"
            fixture.write_text(json.dumps(inventory()))
            with self.assertRaises(SafetyError), redirect_stdout(io.StringIO()):
                main(["--execute", "--fixture", str(fixture), "init"])

    def test_init_requires_matching_explicit_ids(self):
        with tempfile.TemporaryDirectory() as root, patch("l4ctl.cli.ComputeCloud", return_value=FakeCloud()), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SafetyError, "matching"):
                main(["--state-dir", root, "--execute", "init", "--vm-id", "1001", "--boot-id", "wrong"])
            main(["--state-dir", root, "--execute", "init", "--vm-id", "1001", "--boot-id", "2001"])

    def test_unfinished_journal_blocks_new_plan(self):
        cloud = FakeCloud()
        with tempfile.TemporaryDirectory() as root, patch("l4ctl.cli.ComputeCloud", return_value=cloud), redirect_stdout(io.StringIO()):
            store = Store(root, T)
            state = Planner(T, cloud).initial_state()
            store.save("state.json", state)
            store.save("journal.json", Planner(T, cloud, state).plan("park"))
            with self.assertRaisesRegex(SafetyError, "Unfinished"):
                main(["--state-dir", root, "--execute", "up"])
            self.assertFalse(cloud.calls)

    def test_cancel_unsent_pending_steps_is_local_only(self):
        cloud = FakeCloud()
        with tempfile.TemporaryDirectory() as root, patch("l4ctl.cli.ComputeCloud", return_value=cloud), redirect_stdout(io.StringIO()):
            store = Store(root, T)
            state = Planner(T, cloud).initial_state()
            store.save("state.json", state)
            store.save("journal.json", Planner(T, cloud, state).plan("park"))
            main(["--state-dir", root, "--execute", "recover", "--cancel-pending"])
            self.assertEqual(store.load("journal.json")["status"], "cancelled")
            self.assertFalse(cloud.calls)

    def test_cancel_pending_refuses_uncertain_request(self):
        cloud = FakeCloud()
        with tempfile.TemporaryDirectory() as root, patch("l4ctl.cli.ComputeCloud", return_value=cloud), redirect_stdout(io.StringIO()):
            store = Store(root, T)
            state = Planner(T, cloud).initial_state()
            store.save("state.json", state)
            plan = Planner(T, cloud, state).plan("park")
            plan["steps"][0]["phase"] = "uncertain"
            store.save("journal.json", plan)
            with self.assertRaisesRegex(SafetyError, "Cannot cancel"):
                main(["--state-dir", root, "--execute", "recover", "--cancel-pending"])
            self.assertEqual(store.load("journal.json")["status"], "active")

    def test_rate_validation(self):
        for value in ("NaN", "Infinity", "-1", "nonsense"):
            with self.subTest(value=value), self.assertRaises(SafetyError):
                quote_rate(value)

    def test_reject_injection_and_foreign_uris(self):
        for value in ("--other-project", "x;rm", "$(danger)", "a/b"):
            with self.assertRaises(SafetyError):
                Target(vm=value)
        with self.assertRaises(SafetyError):
            relative("https://example.com/projects/p")

    def test_detach_query_preserves_request_id_delimiter(self):
        client = ComputeCloud()
        with patch.object(client, "request", return_value={}) as call:
            client.mutate("POST", T.vm_path + "/detachDisk?deviceName=work", {}, "request-id")
            self.assertIn("&requestId=request-id", call.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
