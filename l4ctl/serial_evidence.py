"""Read-only capture of live guest evidence, durable before any shutdown.

Used by bounded restore probes. This module never creates or stops a resource.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from .core import atomic_json, relative, require, rid


def capture_live_serial(cloud, target, vm_id, run, phase, directory, after_start=None):
    require(bool(re.fullmatch(r"[0-9a-f]{8,32}", run)), "Invalid probe nonce.")
    require(phase in {"helper", "restored"}, "Invalid probe phase.")
    vm = cloud.get(target.vm_path)
    require(vm and rid(vm) == str(vm_id) and relative(vm["selfLink"]) == target.vm_path,
            "Probe VM identity changed; refusing evidence adoption.")
    if vm.get("status") != "RUNNING":
        return None  # never overwrite captured proof with an unavailable stopped port
    started = vm.get("lastStartTimestamp")
    require(bool(started), "Probe VM has no start timestamp.")
    if after_start and datetime.fromisoformat(started.replace("Z", "+00:00")) <= datetime.fromisoformat(after_start.replace("Z", "+00:00")):
        return None
    response = cloud.get(target.vm_path + "/serialPort?port=1&start=0")
    contents = (response or {}).get("contents", "")
    if not contents:
        return None
    markers = re.findall(r"(?m)^L4_[A-Z_]+ " + re.escape(run) + r"(?: [^\r\n]*)?(?=\r?$)", contents)
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(contents.encode()).hexdigest()
    raw_name = phase + "-" + digest + ".txt"
    raw = root / raw_name
    try:
        fd = os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
    path = root / (phase + ".json")
    try:
        previous = json.loads(path.read_text())
    except FileNotFoundError:
        previous = {}
    same_boot = (previous.get("vm_id") == str(vm_id) and previous.get("vm_last_start") == started
                 and previous.get("phase") == phase and previous.get("run") == run)
    samples = dict(previous.get("marker_samples", {})) if same_boot else {}
    for marker in markers:
        samples[marker] = raw_name
    result = {"target": target.as_dict(), "vm_id": str(vm_id), "vm_last_start": started,
              "phase": phase, "run": run, "provider_status_at_read": vm["status"],
              "recorded_at": datetime.now(timezone.utc).isoformat(), "markers": list(samples),
              "marker_samples": samples, "latest_serial_sha256": digest, "latest_raw_file": raw_name}
    atomic_json(path, result)  # caller must not stop the guest before this succeeds
    return result
