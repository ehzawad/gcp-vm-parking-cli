import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from guest.storage_guard import (GuardError, cache_environment, checked_root,
                                 docker_check, run_workload)


UUID = "11111111-2222-3333-4444-555555555555"


def mounts(path):
    return {"target": path, "uuid": UUID if path == "/work" else "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "fstype": "ext4", "options": "rw,relatime", "fsroot": "/"}


class StorageGuardTests(unittest.TestCase):
    def test_missing_mount_never_creates_directories_or_runs_job(self):
        with patch("guest.storage_guard.mount_info", side_effect=GuardError("absent")), \
             patch("guest.storage_guard.os.open") as opened, patch("guest.storage_guard.subprocess.run") as run:
            with self.assertRaises(GuardError):
                run_workload("work", UUID, False, ["workload"])
            opened.assert_not_called()
            run.assert_not_called()

    def test_wrong_mount_uuid_refused(self):
        with patch("guest.storage_guard.mount_info", side_effect=mounts):
            with self.assertRaisesRegex(GuardError, "Wrong UUID"):
                checked_root("work", "ffffffff-2222-3333-4444-555555555555")

    def test_boot_filesystem_bind_mounted_as_work_refused(self):
        def same(path):
            return {**mounts(path), "uuid": UUID}
        with patch("guest.storage_guard.mount_info", side_effect=same):
            with self.assertRaisesRegex(GuardError, "boot filesystem"):
                checked_root("work", UUID)

    def test_subdirectory_bind_mount_refused(self):
        with patch("guest.storage_guard.mount_info", side_effect=lambda path: {**mounts(path), "fsroot": "/subdir"}):
            with self.assertRaisesRegex(GuardError, "bind/subdirectory"):
                checked_root("work", UUID)

    def test_read_only_work_refused(self):
        with patch("guest.storage_guard.mount_info", side_effect=lambda path: {**mounts(path), "options": "ro"}):
            with self.assertRaisesRegex(GuardError, "writable"):
                checked_root("work", UUID)

    def test_deliberate_boot_mode_requires_flag(self):
        with self.assertRaisesRegex(GuardError, "deliberate"):
            checked_root("boot")
        with patch("guest.storage_guard.Path.is_dir", return_value=True), \
             patch("guest.storage_guard.Path.is_symlink", return_value=False):
            self.assertEqual(checked_root("boot", allow_boot=True), Path("/var/lib/l4-small"))

    def test_valid_work_mount_passes(self):
        with patch("guest.storage_guard.mount_info", side_effect=mounts):
            self.assertEqual(checked_root("work", UUID), Path("/work"))

    def test_cache_environment_does_not_relocate_authentication(self):
        result = cache_environment(Path("/proc/123/fd/4"))
        for key in ("HOME", "HF_HOME", "HF_TOKEN_PATH", "DOCKER_CONFIG", "CLOUDSDK_CONFIG", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            self.assertNotIn(key, result)
        self.assertTrue(all(value.startswith("/proc/123/fd/4/") for value in result.values()))

    def test_docker_wrong_data_root_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            policy, daemon = Path(folder) / "policy.json", Path(folder) / "daemon.json"
            policy.write_text(json.dumps({"mode": "work", "uuid": UUID}))
            daemon.write_text(json.dumps({"data-root": "/var/lib/docker"}))
            with patch("guest.storage_guard.mount_info", side_effect=mounts):
                with self.assertRaisesRegex(GuardError, "data-root"):
                    docker_check(policy, daemon)

    def test_docker_missing_work_mount_refuses_start(self):
        with tempfile.TemporaryDirectory() as folder:
            policy, daemon = Path(folder) / "policy.json", Path(folder) / "daemon.json"
            policy.write_text(json.dumps({"mode": "work", "uuid": UUID}))
            daemon.write_text(json.dumps({"data-root": "/work/docker"}))
            with patch("guest.storage_guard.mount_info", side_effect=GuardError("missing /work")):
                with self.assertRaises(GuardError):
                    docker_check(policy, daemon)

    def test_docker_separate_containerd_store_is_not_silently_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            policy, daemon = Path(folder) / "policy.json", Path(folder) / "daemon.json"
            policy.write_text(json.dumps({"mode": "work", "uuid": UUID}))
            daemon.write_text(json.dumps({"data-root": "/work/docker"}))
            with patch("guest.storage_guard.mount_info", side_effect=mounts):
                with self.assertRaisesRegex(GuardError, "containerd"):
                    docker_check(policy, daemon)


if __name__ == "__main__":
    unittest.main()
