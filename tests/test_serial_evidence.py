import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from l4ctl.core import SafetyError
from l4ctl.serial_evidence import capture_live_serial
from tests.fakes import FakeCloud, T


class SerialEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cloud = FakeCloud()
        self.vm = self.cloud.resources[T.vm_path]
        self.vm["status"] = "RUNNING"
        self.serial = T.vm_path + "/serialPort?port=1&start=0"
        self.cloud.resources[self.serial] = {"contents": 'kernel\nL4_RESTORE_BOOT_OK deadbeef {"checks":5}\n'}

    def capture(self, **kwargs):
        return capture_live_serial(self.cloud, T, "1001", "deadbeef", "restored", self.temp.name, **kwargs)

    def test_capture_is_durable_before_shutdown_and_retains_raw_sample(self):
        result = self.capture()
        stored = json.loads((Path(self.temp.name) / "restored.json").read_text())
        self.assertEqual(stored, result)
        raw = Path(self.temp.name) / result["latest_raw_file"]
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), result["latest_serial_sha256"])
        self.assertIn("L4_RESTORE_BOOT_OK deadbeef", result["markers"][0])
        self.assertEqual(stored["provider_status_at_read"], "RUNNING")
        self.assertFalse(self.cloud.calls)

    def test_stopped_port_cannot_erase_already_captured_evidence(self):
        original = self.capture()
        self.vm["status"] = "TERMINATED"
        self.cloud.resources.pop(self.serial)
        self.assertIsNone(self.capture())
        self.assertEqual(json.loads((Path(self.temp.name) / "restored.json").read_text()), original)

    def test_empty_response_cannot_erase_existing_proof(self):
        original = self.capture()
        self.cloud.resources[self.serial] = {"contents": ""}
        self.assertIsNone(self.capture())
        self.assertEqual(json.loads((Path(self.temp.name) / "restored.json").read_text()), original)

    def test_vm_name_reuse_fails_closed(self):
        self.vm["id"] = "9999"
        with self.assertRaises(SafetyError):
            self.capture()

    def test_old_boot_cannot_qualify_for_new_probe_phase(self):
        self.assertIsNone(self.capture(after_start=self.vm["lastStartTimestamp"]))
        self.assertFalse((Path(self.temp.name) / "restored.json").exists())

    def test_wrong_probe_nonce_does_not_create_success_marker(self):
        self.cloud.resources[self.serial]["contents"] = "L4_RESTORE_BOOT_OK cafebabe {}\n"
        self.assertEqual(self.capture()["markers"], [])

    def test_rollover_preserves_markers_and_their_original_raw_evidence(self):
        original = self.capture()
        self.cloud.resources[self.serial]["contents"] = "later kernel output\n"
        newer = self.capture()
        self.assertEqual(newer["markers"], original["markers"])
        self.assertTrue((Path(self.temp.name) / newer["marker_samples"][original["markers"][0]]).exists())

    def test_failed_durable_write_is_not_reported_as_success(self):
        with patch("l4ctl.serial_evidence.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.capture()
        self.assertFalse((Path(self.temp.name) / "restored.json").exists())

    def test_embedded_marker_and_nonce_prefix_cannot_qualify(self):
        self.cloud.resources[self.serial]["contents"] = ('log: L4_RESTORE_BOOT_OK deadbeef {}\n'
            'L4_RESTORE_BOOT_OK deadbeef0 {}\n')
        self.assertEqual(self.capture()["markers"], [])

    def test_new_boot_with_new_nonce_does_not_adopt_previous_proof(self):
        original = self.capture()
        self.vm["lastStartTimestamp"] = "2026-10-21T12:00:00Z"
        self.cloud.resources[self.serial]["contents"] = "L4_RESTORE_BOOT_OK deadbeef {}\n"
        fresh = capture_live_serial(self.cloud, T, "1001", "cafebabe", "restored", self.temp.name)
        self.assertEqual(fresh["markers"], [])
        self.assertTrue((Path(self.temp.name) / original["latest_raw_file"]).exists())
