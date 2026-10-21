"""Identity-pinned plans, durable execution, and read-only crash reconciliation."""
from __future__ import annotations

import copy
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass


class SafetyError(RuntimeError):
    pass


class Uncertain(SafetyError):
    pass


class Cancelled(SafetyError):
    pass


class OperationFailed(SafetyError):
    """Known completed cloud operation with an explicit error; never retry it."""
    pass


def require(condition, message):
    if not condition:
        raise SafetyError(message)


def name(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]", value),
            "Resource names must be lowercase RFC1035 names (1–63 characters).")
    return value


def rid(resource):
    value = str((resource or {}).get("id", ""))
    require(bool(re.fullmatch(r"[0-9]+", value)), "Resource is missing a numeric ID.")
    return value


def relative(link):
    prefix = "https://www.googleapis.com/compute/v1/"
    if link.startswith(prefix):
        return link[len(prefix):]
    require(link.startswith("projects/") and ".." not in link and "?" not in link,
            "Unexpected resource URI; refusing it.")
    return link


@dataclass(frozen=True)
class Target:
    project: str = "example-project"
    zone: str = "asia-southeast1-c"
    vm: str = "gpu-vm"

    def __post_init__(self):
        for value in (self.project, self.zone, self.vm):
            name(value)

    def path(self, kind, resource):
        name(resource)
        scope = "global" if kind == "snapshots" else f"zones/{self.zone}"
        return f"projects/{self.project}/{scope}/{kind}/{resource}"

    @property
    def vm_path(self):
        return self.path("instances", self.vm)

    def as_dict(self):
        return {"project": self.project, "zone": self.zone, "vm": self.vm}


class ComputeCloud:
    """REST operations with the existing gcloud login; tokens stay only in memory.

    There is deliberately no login, configuration mutation, credential export,
    retry adapter, or SDK dependency. HTTP errors are sanitized, never dumped.
    """
    base = "https://www.googleapis.com/compute/v1/"

    def __init__(self, timeout=30):
        self.timeout = timeout
        self._token = None

    def prepare(self):
        if self._token is None:
            try:
                result = subprocess.run(["gcloud", "auth", "print-access-token"],
                                        capture_output=True, text=True, timeout=30, check=True)
            except (OSError, subprocess.SubprocessError):
                raise SafetyError("Existing gcloud credentials unavailable; no login was attempted.") from None
            token = result.stdout.strip()
            require(bool(token) and not any(c.isspace() for c in token), "Invalid gcloud token response.")
            self._token = token

    def request(self, method, path, body=None):
        self.prepare()
        require(path.startswith("projects/") and ".." not in path, "Invalid API path.")
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + self._token,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if method == "GET" and error.code == 404:
                return None
            # A mutation rejection is conservatively treated as uncertain too.
            raise SafetyError(f"Compute API {method} returned HTTP {error.code}; inspect the journal.") from None
        except (OSError, ValueError):
            raise SafetyError(f"Compute API {method} did not yield a usable response.") from None

    def get(self, path):
        return self.request("GET", path)

    def mutate(self, method, path, body, request_id):
        return self.request(method, path + ("&" if "?" in path else "?") + "requestId=" + request_id, body)


class FixtureCloud:
    """Offline inventory. Execution is prohibited even if --execute is supplied."""
    def __init__(self, fixture):
        self.resources = fixture["resources"]

    def get(self, path):
        return copy.deepcopy(self.resources.get(path))

    def prepare(self):
        raise SafetyError("Fixture mode is read-only; --execute cannot send requests.")

    def mutate(self, *args):
        raise SafetyError("Fixture mode is read-only.")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Store:
    def __init__(self, root, target):
        self.root = Path(root) / target.project / target.zone / target.vm
        self.target = target

    def load(self, filename, default=None):
        try:
            value = json.loads((self.root / filename).read_text())
        except FileNotFoundError:
            return copy.deepcopy(default)
        require(value.get("target") == self.target.as_dict(), "State belongs to a different target.")
        return value

    def save(self, filename, value):
        value["target"] = self.target.as_dict()
        atomic_json(self.root / filename, value)

    @contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Kernel flock survives stale files and is released on crash; never unlink it.
        with (self.root / "lock").open("a+") as stream:
            os.chmod(self.root / "lock", 0o600)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SafetyError("Another l4ctl process holds this target's lock.") from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


CONFIG_FIELDS = ("machineType", "guestAccelerators", "scheduling", "serviceAccounts",
                 "shieldedInstanceConfig", "confidentialInstanceConfig", "deletionProtection",
                 "canIpForward", "tags", "networkInterfaces")


def config_view(vm):
    # Deliberately omit metadata/startup scripts and encryption key material.
    result = {k: copy.deepcopy(vm[k]) for k in CONFIG_FIELDS if k in vm}
    for interface in result.get("networkInterfaces", []):
        for key in ("networkIP", "ipv6Address", "fingerprint"):
            interface.pop(key, None)
        for access in interface.get("accessConfigs", []):
            for key in ("natIP", "externalIpv6"):
                access.pop(key, None)
    return result


def disk_view(disk):
    return {k: copy.deepcopy(disk[k]) for k in
            ("id", "name", "sizeGb", "type", "selfLink", "status", "users",
             "sourceSnapshot", "sourceSnapshotId", "labels") if k in disk}


class Planner:
    def __init__(self, target, cloud, state=None):
        self.t, self.cloud, self.state = target, cloud, copy.deepcopy(state)
        self.vm = cloud.get(target.vm_path)
        require(self.vm is not None, "VM is missing. This CLI never recreates or deletes a VM.")
        require(relative(self.vm["selfLink"]) == target.vm_path, "Wrong VM URI.")
        rid(self.vm)
        if state:
            require(rid(self.vm) == state["vm_id"], "VM name was reused: numeric ID changed.")

    def initial_state(self):
        boots = [d for d in self.vm.get("disks", []) if d.get("boot")]
        require(len(boots) == 1, "Enrollment requires exactly one existing boot disk.")
        attachment = boots[0]
        path = relative(attachment["source"])
        require(path.startswith(self.t.path("disks", "x")[:-1]), "Boot disk is outside target project/zone.")
        disk = self.cloud.get(path)
        require(disk and relative(disk["selfLink"]) == path, "Boot disk not found or URI mismatch.")
        require(int(disk["sizeGb"]) >= 70,
                "Enrollment requires an existing boot disk of at least 70 GB.")
        require(attachment.get("mode", "READ_WRITE") == "READ_WRITE", "Unsupported boot mode.")
        return {"version": 1, "target": self.t.as_dict(), "vm_id": rid(self.vm),
                "vm_config": config_view(self.vm),
                "boot": {**disk_view(disk), "attachment": {k: copy.deepcopy(attachment[k]) for k in
                         ("boot", "source", "deviceName", "autoDelete", "mode", "interface", "type") if k in attachment}, "parked": False},
                "work": {}, "tests": {}, "snapshots": {}}

    def tracked(self, disk_name, role="work"):
        require(self.state is not None, "Run init to pin numeric resource IDs before execution.")
        record = self.state["boot"] if role == "boot" else self.state[role].get(disk_name)
        require(record is not None, f"Disk {disk_name} is not enrolled as {role}.")
        require(record["name"] == disk_name, "Wrong enrolled disk name.")
        return copy.deepcopy(record)

    def plan(self, action, **options):
        require(self.state is not None, "Enrollment state required.")
        steps = []

        def add(kind, **data):
            step = {"kind": kind, **data, "request_id": str(uuid.uuid4()), "phase": "pending"}
            steps.append(step)
            return step

        def stop():
            require(self.vm["status"] in {"RUNNING", "TERMINATED"},
                    "Wait for a stable RUNNING/TERMINATED state before stopping or parking.")
            if self.vm["status"] == "RUNNING":
                add("stop")

        def snapshot(record, role, snap_name=None):
            snap_name = name(snap_name or f"l4-{role}-{uuid.uuid4().hex[:16]}")
            add("snapshot", disk=record, role=role, snapshot=snap_name)
            return snap_name

        boot = self.state["boot"]
        if action == "stop":
            stop()
            after = options.get("after", "keep")
            if after in {"snapshot", "park"}:
                snap = snapshot(boot, "boot", options.get("snapshot"))
                if after == "park":
                    add("detach", disk=boot, role="boot", snapshot=snap, destructive=True)
                    add("delete", disk=boot, role="boot", snapshot=snap, destructive=True)
        elif action == "park":
            if boot.get("parked"):
                require(self.cloud.get(self.t.path("disks", boot["name"])) is None,
                        "Parked disk name now exists; inspect possible name reuse.")
                require(not any(a.get("boot") for a in self.vm.get("disks", [])), "Unexpected boot attachment.")
                return {"version": 1, "id": str(uuid.uuid4()), "action": action, "target": self.t.as_dict(),
                        "vm_id": self.state["vm_id"], "steps": [], "status": "active"}
            stop()
            snap = snapshot(boot, "boot", options.get("snapshot"))
            add("detach", disk=boot, role="boot", snapshot=snap, destructive=True)
            add("delete", disk=boot, role="boot", snapshot=snap, destructive=True)
        elif action == "snapshot":
            require(self.vm["status"] == "TERMINATED", "Stop first for a quiescent snapshot.")
            role = options.get("role", "boot")
            record = boot if role == "boot" else self.tracked(options["disk"], role)
            snapshot(record, role, options.get("snapshot"))
        elif action == "up":
            boots = [d for d in self.vm.get("disks", []) if d.get("boot")]
            if boots:
                require(len(boots) == 1 and relative(boots[0]["source"]) == self.t.path("disks", boot["name"]),
                        "Unexpected boot attachment.")
                actual = self.cloud.get(self.t.path("disks", boot["name"]))
                require(actual and rid(actual) == str(boot["id"]), "Boot disk ID changed.")
            else:
                require(self.vm["status"] == "TERMINATED", "Boot restoration requires a stopped VM.")
                existing = self.cloud.get(self.t.path("disks", boot["name"]))
                if existing:
                    require(rid(existing) == str(boot["id"]), "Boot name was reused; refusing adoption.")
                    add("attach", disk=boot, role="boot")
                else:
                    require(boot.get("parked"), "Boot disk vanished without a completed park; inspect recovery.")
                    snap = self.state["snapshots"].get(boot.get("snapshot"))
                    require(snap is not None, "No pinned park snapshot recorded.")
                    size = max(70, int(boot["sizeGb"]), int(snap["diskSizeGb"]))
                    add("restore", disk=boot, role="boot", snapshot=snap["name"], size=size)
                    add("attach", disk={"name": boot["name"], "id": "$created", "attachment": boot["attachment"]}, role="boot")
            if self.vm["status"] != "RUNNING":
                require(self.vm["status"] in {"TERMINATED", "SUSPENDED"}, "VM is transitioning; wait and inspect status.")
                add("resume" if self.vm["status"] == "SUSPENDED" else "start")
        elif action == "resize":
            size = int(options["size"])
            require(size >= int(boot["sizeGb"]), "Disk cannot shrink.")
            require(size <= 65536, "Disk size exceeds this CLI's limit.")
            if size > int(boot["sizeGb"]):
                add("resize", disk=boot, role="boot", size=size)
        elif action == "work-create":
            disk_name = name(options["disk"])
            require(disk_name != boot["name"], "Work disk cannot use the boot disk name.")
            require(1 <= int(options["size"]) <= 65536, "Work size must be 1–65536 GB.")
            require(disk_name not in self.state["work"], "Work disk already enrolled; use attach or restore.")
            add("create", disk={"name": disk_name}, role="work", size=int(options["size"]))
        elif action in {"work-attach", "work-detach", "work-remove", "work-restore"}:
            record = self.tracked(options["disk"])
            require(record["name"] != boot["name"] and str(record["id"]) != str(boot["id"]), "Boot disk is protected.")
            require(self.vm["status"] == "TERMINATED", "Stop first before changing work attachments.")
            if action == "work-restore":
                require(record.get("parked"), "Work disk has not been independently parked.")
                snap = self.state["snapshots"].get(record.get("snapshot"))
                require(snap is not None, "Pinned work snapshot missing.")
                add("restore", disk=record, role="work", snapshot=snap["name"],
                    size=max(int(record["sizeGb"]), int(snap["diskSizeGb"])))
            elif action == "work-remove":
                snap = snapshot(record, "work", options.get("snapshot"))
                add("detach", disk=record, role="work", snapshot=snap, destructive=True)
                add("delete", disk=record, role="work", snapshot=snap, destructive=True)
            else:
                add("attach" if action == "work-attach" else "detach", disk=record, role="work")
        elif action == "test-restore":
            snap_name = options["snapshot"]
            require(snap_name in self.state["snapshots"], "Test restore requires an enrolled snapshot.")
            snap = self.state["snapshots"][snap_name]
            disk_name = name(options["disk"])
            require(disk_name != boot["name"] and disk_name not in self.state["work"], "Use a separate test disk name.")
            add("restore", disk={"name": disk_name}, role="tests", snapshot=snap_name, size=int(snap["diskSizeGb"]))
        elif action == "test-cleanup":
            record = self.tracked(options["disk"], "tests")
            require(record.get("snapshot"), "Test disk provenance missing.")
            add("delete", disk=record, role="tests", snapshot=record["snapshot"], destructive=True)
        else:
            raise SafetyError("Unknown plan action.")
        return {"version": 1, "id": str(uuid.uuid4()), "action": action, "target": self.t.as_dict(),
                "vm_id": self.state["vm_id"], "steps": steps, "status": "active"}


class Engine:
    def __init__(self, target, cloud, store, state, timeout=600, interval=2, confirm=None):
        self.t, self.cloud, self.store, self.state = target, cloud, store, state
        self.timeout, self.interval = timeout, interval
        self.confirm = confirm or (lambda phrase: False)
        self.confirmed = set()

    def save(self, journal):
        self.store.save("journal.json", journal)

    def vm(self):
        vm = self.cloud.get(self.t.vm_path)
        require(vm and rid(vm) == self.state["vm_id"], "VM numeric ID changed or is missing.")
        require(relative(vm["selfLink"]) == self.t.vm_path, "VM URI differs from the selected target.")
        require(config_view(vm) == self.state["vm_config"], "VM configuration drifted; review before continuing.")
        return vm

    def disk(self, step, absent=False):
        record = step["disk"]
        actual = self.cloud.get(self.t.path("disks", record["name"]))
        if absent:
            require(actual is None, "Destination disk name already exists; refusing overwrite/adoption.")
        else:
            expected = record.get("id")
            if expected == "$created":
                expected = self.state["boot"]["id"]
            require(actual and rid(actual) == str(expected), "Disk ID changed or disk missing; name reuse protection.")
            require(relative(actual["selfLink"]) == self.t.path("disks", record["name"]), "Wrong disk URI.")
        return actual

    def verified_snapshot(self, snap_name, record=None):
        saved = self.state["snapshots"].get(snap_name)
        require(saved is not None, "Snapshot is not identity-pinned in the ledger.")
        snap = self.cloud.get(self.t.path("snapshots", snap_name))
        require(snap and rid(snap) == str(saved["id"]) and snap.get("status") == "READY",
                "Snapshot is not READY or its numeric ID changed.")
        require(relative(snap["selfLink"]) == self.t.path("snapshots", snap_name), "Wrong snapshot URI.")
        require(str(snap.get("sourceDiskId")) == str(saved["sourceDiskId"]) and
                relative(snap["sourceDisk"]) == relative(saved["sourceDisk"]), "Snapshot source identity changed.")
        if record:
            require(str(snap["sourceDiskId"]) == str(record["id"]) and
                    relative(snap["sourceDisk"]) == self.t.path("disks", record["name"]) and
                    int(snap["diskSizeGb"]) == int(record["sizeGb"]), "Snapshot does not cover this exact source disk.")
            actual_disk = self.cloud.get(self.t.path("disks", record["name"]))
            require(actual_disk and actual_disk.get("lastAttachTimestamp") == saved.get("sourceLastAttachTimestamp"),
                    "Source disk was reattached after snapshot; take a fresh backup before removal.")
            require(self.vm().get("lastStartTimestamp") == saved.get("vmLastStartTimestamp"),
                    "VM restarted after snapshot; take a fresh backup before removal.")
        return snap

    def deletion_phrase(self, record):
        return f"DELETE {self.t.path('disks', record['name'])} id={record['id']}"

    def guard(self, step):
        vm = self.vm()
        kind, role = step["kind"], step.get("role")
        if kind in {"snapshot", "detach", "delete", "attach"}:
            require(vm["status"] == "TERMINATED", "VM must be stopped for disk changes/snapshots.")
        if role in {"work", "tests"} and "disk" in step:
            record = step["disk"]
            require(record["name"] != self.state["boot"]["name"] and
                    str(record.get("id")) != str(self.state["boot"]["id"]), "Boot disk is protected from work/test operations.")
        if kind in {"create", "restore"}:
            self.disk(step, absent=True)
            if kind == "restore":
                snap = self.verified_snapshot(step["snapshot"])
                require(step["size"] >= int(snap["diskSizeGb"]), "Restore disk would be too small.")
        elif "disk" in step:
            disk = self.disk(step)
            if kind in {"detach", "snapshot"}:
                require(all(relative(u) == self.t.vm_path for u in disk.get("users", [])), "Disk is used by another VM.")
            if kind in {"attach", "delete"}:
                require(not disk.get("users"), "Disk is still attached; refusing attach/delete.")
            attachments = [a for a in vm.get("disks", []) if relative(a.get("source", "")) == self.t.path("disks", disk["name"])]
            if role == "work":
                require(not any(a.get("boot") for a in attachments), "Work operation discovered a boot attachment.")
            if kind == "attach" and role == "boot":
                require(not any(a.get("boot") for a in vm.get("disks", [])), "VM already has a boot disk.")
            if kind == "resize":
                require(int(disk["sizeGb"]) <= step["size"], "Cannot shrink current cloud disk.")
            if kind == "snapshot":
                require(int(disk["sizeGb"]) == int(step["disk"]["sizeGb"]), "Disk size changed; replan snapshot.")
                require(self.cloud.get(self.t.path("snapshots", step["snapshot"])) is None, "Snapshot name already exists.")
                step["source_last_attach"] = disk.get("lastAttachTimestamp")
                step["vm_last_start"] = vm.get("lastStartTimestamp")
        if kind == "start":
            require(vm["status"] == "TERMINATED", "Start requires a stopped VM.")
        if kind == "resume":
            require(vm["status"] == "SUSPENDED", "Resume requires a suspended VM.")
        if kind in {"start", "resume"}:
            boot = self.state["boot"]
            disk = self.disk({"disk": boot})
            require(any(a.get("boot") and relative(a["source"]) == self.t.path("disks", disk["name"])
                        for a in vm.get("disks", [])), "Pinned boot disk is not attached.")
        if kind == "stop":
            require(vm["status"] == "RUNNING", "Stop plan is stale; inspect/replan.")
        if step.get("destructive"):
            self.verified_snapshot(step["snapshot"], None if role == "tests" else step["disk"])
            if role == "tests":
                require(str(disk.get("sourceSnapshotId")) == str(self.state["snapshots"][step["snapshot"]]["id"]), "Wrong test disk provenance.")
            phrase = self.deletion_phrase(step["disk"])
            if phrase not in self.confirmed:
                if not self.confirm(phrase):
                    raise Cancelled("Deletion cancelled; all source data is retained (possibly detached).")
                self.confirmed.add(phrase)
                # Re-run every precondition after user interaction, including
                # stopped state, users, boot protection and backup freshness.
                self.guard(step)

    def request(self, step):
        kind, role = step["kind"], step.get("role")
        disk = step.get("disk", {})
        path, method, body = self.t.vm_path, "POST", {}
        tag = {"l4ctl-operation": step["request_id"]}
        if kind in {"start", "stop", "resume"}:
            path += "/" + kind
        elif kind == "snapshot":
            path = f"projects/{self.t.project}/global/snapshots"
            body = {"name": step["snapshot"], "sourceDisk": self.t.path("disks", disk["name"]),
                    "snapshotType": "STANDARD", "labels": tag}
        elif kind in {"create", "restore"}:
            path = f"projects/{self.t.project}/zones/{self.t.zone}/disks"
            body = {"name": disk["name"], "sizeGb": str(step["size"]), "labels": tag,
                    "type": f"projects/{self.t.project}/zones/{self.t.zone}/diskTypes/pd-balanced"}
            if kind == "restore":
                body["sourceSnapshot"] = self.t.path("snapshots", step["snapshot"])
                if role != "tests":
                    body["type"] = disk["type"]
        elif kind == "attach":
            path += "/attachDisk"
            attachment = disk.get("attachment", {})
            body = {"source": self.t.path("disks", disk["name"]), "boot": role == "boot",
                    "autoDelete": attachment.get("autoDelete", False) if role == "boot" else False,
                    "mode": "READ_WRITE", "type": "PERSISTENT",
                    "deviceName": attachment.get("deviceName", disk["name"]),
                    "interface": attachment.get("interface", "SCSI")}
        elif kind == "detach":
            path += "/detachDisk?deviceName=" + name(disk.get("attachment", {}).get("deviceName", disk["name"]))
        elif kind == "delete":
            path, method, body = self.t.path("disks", disk["name"]), "DELETE", None
        elif kind == "resize":
            path, body = self.t.path("disks", disk["name"]) + "/resize", {"sizeGb": str(step["size"])}
        else:
            raise SafetyError("Unknown request kind.")
        return method, path, body

    def operation_path(self, operation):
        require(isinstance(operation, dict), "Malformed operation response.")
        path = relative(operation.get("selfLink", ""))
        require(re.fullmatch(rf"projects/{re.escape(self.t.project)}/(?:global|zones/{re.escape(self.t.zone)})/operations/[a-zA-Z0-9_-]+", path),
                "Operation is outside the selected project/zone.")
        return path

    def poll(self, operation):
        path = self.operation_path(operation)
        deadline = time.monotonic() + self.timeout
        while True:
            current = self.cloud.get(path)
            require(current is not None, "Operation is missing; outcome needs inspection.")
            require(relative(current["selfLink"]) == path, "Wrong operation returned.")
            if current.get("status") == "DONE":
                if current.get("error") or current.get("httpErrorStatusCode"):
                    raise OperationFailed("Cloud operation reported failure; no retries. Source disks were retained unless an earlier step completed.")
                return
            if time.monotonic() >= deadline:
                raise Uncertain("Operation polling timed out; recover polls this same operation without resubmission.")
            time.sleep(min(self.interval, max(0, deadline - time.monotonic())))

    def verify(self, step):
        kind, role = step["kind"], step.get("role")
        vm = self.vm()
        record = step.get("disk", {})
        if kind == "snapshot":
            snap = self.cloud.get(self.t.path("snapshots", step["snapshot"]))
            require(snap and snap.get("status") == "READY" and
                    snap.get("labels", {}).get("l4ctl-operation") == step["request_id"] and
                    str(snap.get("sourceDiskId")) == str(record["id"]) and
                    relative(snap["sourceDisk"]) == self.t.path("disks", record["name"]) and
                    int(snap["diskSizeGb"]) == int(record["sizeGb"]), "Snapshot completion/source verification failed.")
            require(relative(snap["selfLink"]) == self.t.path("snapshots", step["snapshot"]), "Wrong snapshot URI.")
            rid(snap)
            result = {k: snap[k] for k in ("name", "id", "selfLink", "sourceDisk", "sourceDiskId", "diskSizeGb", "status", "storageBytes") if k in snap}
            result["sourceLastAttachTimestamp"] = step.get("source_last_attach")
            result["vmLastStartTimestamp"] = step.get("vm_last_start")
            return result
        if kind in {"create", "restore"}:
            disk = self.cloud.get(self.t.path("disks", record["name"]))
            require(disk and disk.get("status") == "READY" and
                    disk.get("labels", {}).get("l4ctl-operation") == step["request_id"] and
                    int(disk["sizeGb"]) >= step["size"] and not disk.get("users"), "Created disk verification failed.")
            require(relative(disk["selfLink"]) == self.t.path("disks", record["name"]), "Wrong created disk URI.")
            rid(disk)
            if kind == "restore":
                snap = self.verified_snapshot(step["snapshot"])
                require(str(disk.get("sourceSnapshotId")) == rid(snap) and
                        relative(disk["sourceSnapshot"]) == self.t.path("snapshots", snap["name"]), "Restored disk snapshot identity mismatch.")
            return disk_view(disk)
        if kind == "delete":
            require(self.cloud.get(self.t.path("disks", record["name"])) is None, "Deleted disk still exists or name was reused.")
        elif kind in {"detach", "attach"}:
            disk = self.disk(step)
            matches = [a for a in vm.get("disks", []) if relative(a["source"]) == self.t.path("disks", record["name"])]
            if kind == "detach":
                require(not matches and not disk.get("users"), "Detach verification failed.")
            else:
                require(len(matches) == 1 and bool(matches[0].get("boot")) == (role == "boot"), "Attach verification failed.")
                require(matches[0].get("autoDelete", False) == (record.get("attachment", {}).get("autoDelete", False) if role == "boot" else False), "Auto-delete policy mismatch.")
        elif kind in {"start", "resume", "stop"}:
            require(vm["status"] == ("TERMINATED" if kind == "stop" else "RUNNING"), "VM state verification failed.")
        elif kind == "resize":
            disk = self.disk(step)
            require(int(disk["sizeGb"]) == step["size"], "Disk resize verification failed.")
        return {}

    def effect(self, step, result):
        kind, role = step["kind"], step.get("role")
        record = step.get("disk", {})
        if kind == "snapshot":
            self.state["snapshots"][result["name"]] = result
            dest = self.state["boot"] if role == "boot" else self.state["work"][record["name"]]
            dest["snapshot"] = result["name"]
        elif kind in {"create", "restore"}:
            dest = {**copy.deepcopy(record), **result, "parked": False}
            if kind == "restore":
                dest["snapshot"] = step["snapshot"]
            if role == "boot":
                self.state["boot"] = dest
            else:
                self.state[role][record["name"]] = dest
        elif kind == "delete":
            dest = self.state["boot"] if role == "boot" else self.state[role][record["name"]]
            dest["parked"] = True
        elif kind == "resize":
            self.state["boot"]["sizeGb"] = str(step["size"])
        self.store.save("state.json", self.state)

    def apply(self, journal, recover=False):
        require(journal["target"] == self.t.as_dict() and journal["vm_id"] == self.state["vm_id"], "Wrong journal target/VM ID.")
        require(journal["status"] == "active", "Journal is closed; nothing to resume.")
        self.cloud.prepare()  # auth failure occurs before any submitting marker
        self.save(journal)
        for step in journal["steps"]:
            if step["phase"] == "done":
                # Replay local effects after a crash between durable journal and state writes.
                self.effect(step, step["result"])
                continue
            if step["phase"] in {"submitting", "uncertain"}:
                require(recover, "Uncertain request: use recover; do not repeat the command.")
                if step.get("operation"):
                    try:
                        self.poll(step["operation"])
                    except OperationFailed:
                        step["phase"], journal["status"] = "failed", "failed"
                        self.save(journal)
                        raise
                # No request is ever reissued here, even if postcondition is absent.
                result = self.verify(step)
                step.update(phase="done", result=result)
                self.save(journal)
                self.effect(step, result)
                continue
            require(step["phase"] == "pending", "Unknown journal phase.")
            try:
                self.guard(step)
            except Cancelled:
                journal["status"] = "cancelled"
                self.save(journal)
                raise
            method, path, body = self.request(step)
            step["phase"] = "submitting"
            self.save(journal)  # fsync before sending: a crash cannot hide an issued request
            try:
                operation = self.cloud.mutate(method, path, body, step["request_id"])
                self.operation_path(operation)
                step["operation"] = {k: operation[k] for k in ("selfLink", "name", "id") if k in operation}
                self.save(journal)
                self.poll(operation)
                result = self.verify(step)
            except OperationFailed:
                step["phase"], journal["status"] = "failed", "failed"
                self.save(journal)
                raise
            except BaseException:
                step["phase"] = "uncertain"
                self.save(journal)
                raise
            step.update(phase="done", result=result)
            self.save(journal)
            self.effect(step, result)
        journal["status"] = "completed"
        self.save(journal)
