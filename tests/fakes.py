"""In-memory Compute implementation. No subprocess, credentials or network."""
import copy
import uuid
from l4ctl.core import Target


T = Target()


def inventory():
    disk_path = T.path("disks", "gpu-vm-boot-70gb")
    return {"resources": {
        T.vm_path: {"id": "1001", "name": T.vm, "selfLink": T.vm_path, "status": "TERMINATED",
                    "lastStartTimestamp": "2026-10-16T01:00:00Z",
                    "machineType": f"zones/{T.zone}/machineTypes/g2-standard-8",
                    "guestAccelerators": [{"acceleratorType": "nvidia-l4", "acceleratorCount": 1}],
                    "metadata": {"items": [{"key": "secret-test", "value": "DO-NOT-PERSIST"}]},
                    "networkInterfaces": [{"name": "nic0", "network": "default", "accessConfigs": [{"natIP": "192.0.2.1"}]}],
                    "disks": [{"boot": True, "source": disk_path, "deviceName": "persistent-disk-0",
                               "autoDelete": True, "mode": "READ_WRITE", "interface": "SCSI"}]},
        disk_path: {"id": "2001", "name": "gpu-vm-boot-70gb", "selfLink": disk_path, "status": "READY",
                    "lastAttachTimestamp": "2026-10-16T00:00:00Z",
                    "sizeGb": "70", "type": f"projects/{T.project}/zones/{T.zone}/diskTypes/pd-balanced", "users": [T.vm_path]}
    }}


class FakeCloud:
    def __init__(self):
        self.resources = inventory()["resources"]
        self.calls = []
        self.next_id = 3000
        self.fail_after_mutation = False
        self.fail_before_mutation = False
        self.bad_snapshot_source = False
        self.operation_error = False
        self.operation_running = False

    def prepare(self):
        pass

    def get(self, path):
        return copy.deepcopy(self.resources.get(path))

    def list_snapshots(self, project, disk_ids):
        return [copy.deepcopy(r) for p, r in self.resources.items() if f"projects/{project}/global/snapshots/" in p and str(r.get("sourceDiskId")) in {str(i) for i in disk_ids}]

    def mutate(self, method, path, body, request_id):
        self.calls.append((method, path, copy.deepcopy(body), request_id))
        if self.fail_before_mutation:
            raise TimeoutError("No response; may have reached the server")
        vm = self.resources[T.vm_path]
        base_path = path.split("?")[0]
        op_path = f"projects/{T.project}/{'global' if '/global/' in path else 'zones/' + T.zone}/operations/op-{uuid.uuid4()}"
        operation = {"selfLink": op_path, "name": op_path.rsplit('/', 1)[-1],
                     "status": "RUNNING" if self.operation_running else "DONE"}
        if self.operation_error:
            operation["error"] = {"errors": [{"code": "TEST_FAILURE"}]}
        if base_path.endswith(("/start", "/resume")):
            vm["status"] = "RUNNING"
        elif base_path.endswith("/stop"):
            vm["status"] = "TERMINATED"
        elif base_path.endswith("/global/snapshots"):
            disk = self.resources[body["sourceDisk"]]
            snap_path = T.path("snapshots", body["name"])
            self.next_id += 1
            self.resources[snap_path] = {"id": str(self.next_id), "name": body["name"], "selfLink": snap_path,
                "storageLocations": body.get("storageLocations", []), "snapshotType": "STANDARD", "creationTimestamp": "2026-10-21T10:00:00Z",
                "status": "READY", "sourceDisk": body["sourceDisk"], "sourceDiskId": "9999" if self.bad_snapshot_source else disk["id"],
                "diskSizeGb": disk["sizeGb"], "storageBytes": "1073741824", "labels": body["labels"]}
        elif base_path.endswith("/disks") and method == "POST":
            self.next_id += 1
            disk_path = T.path("disks", body["name"])
            self.resources[disk_path] = {"id": str(self.next_id), "name": body["name"], "selfLink": disk_path,
                "status": "READY", "sizeGb": body["sizeGb"], "type": body["type"], "labels": body["labels"], "users": []}
            if "sourceSnapshot" in body:
                self.resources[disk_path].update(sourceSnapshot=body["sourceSnapshot"],
                                                sourceSnapshotId=self.resources[body["sourceSnapshot"]]["id"])
        elif base_path.endswith("/detachDisk"):
            device = path.split("deviceName=")[1]
            found = [a for a in vm["disks"] if a["deviceName"] == device]
            if found:
                self.resources[found[0]["source"]]["users"] = []
            vm["disks"] = [a for a in vm["disks"] if a["deviceName"] != device]
        elif base_path.endswith("/attachDisk"):
            vm["disks"].append(copy.deepcopy(body))
            self.resources[body["source"]]["users"] = [T.vm_path]
        elif base_path.endswith("/resize"):
            self.resources[base_path[:-7]]["sizeGb"] = body["sizeGb"]
        elif method == "DELETE":
            del self.resources[base_path]
        else:
            raise AssertionError((method, path, body))
        self.resources[op_path] = operation
        if self.fail_after_mutation:
            raise TimeoutError("Reply lost after server changed resource")
        return copy.deepcopy(operation)
