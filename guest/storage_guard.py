#!/usr/bin/env python3
"""Linux foreground workload/cache guard and Docker ExecStartPre check.

No formatting, mounting, credential relocation, or implicit boot fallback.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


class GuardError(RuntimeError):
    pass


def need(condition, message):
    if not condition:
        raise GuardError(message)


def mount_info(path):
    try:
        result = subprocess.run(["findmnt", "--json", "--mountpoint", path,
                                 "--output", "TARGET,UUID,FSTYPE,OPTIONS,FSROOT"],
                                capture_output=True, text=True, check=True, timeout=10)
        entries = json.loads(result.stdout)["filesystems"]
        need(len(entries) == 1 and entries[0]["target"] == path, "Expected an exact mount point.")
        return entries[0]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        raise GuardError(f"Cannot verify exact mount {path}; refusing storage fallback.") from None


def checked_root(mode, expected_uuid=None, allow_boot=False):
    if mode == "boot":
        need(allow_boot, "Boot storage requires deliberate --allow-boot-storage.")
        root = Path("/var/lib/l4-small")
        need(not root.is_symlink(), "Boot storage root must not be a symlink.")
        need(root.is_dir(), "Provision /var/lib/l4-small with appropriate ownership first.")
        return root
    need(mode == "work", "Unknown storage mode.")
    need(bool(expected_uuid) and re.fullmatch(r"[A-Fa-f0-9-]{8,64}", expected_uuid), "Supply the expected filesystem UUID.")
    work = Path("/work")
    need(not work.is_symlink(), "/work must not be a symlink.")
    info, boot = mount_info("/work"), mount_info("/")
    need(info.get("uuid") == expected_uuid and info.get("uuid") != boot.get("uuid"), "Wrong UUID or boot filesystem at /work.")
    need(info.get("fstype") in {"ext4", "xfs"} and info.get("fsroot") == "/", "Unsupported filesystem or bind/subdirectory mount.")
    need("rw" in info.get("options", "").split(","), "/work must be writable.")
    return work


def cache_environment(anchor):
    # Keep HOME, HF_HOME, HF_TOKEN_PATH, CLOUDSDK_CONFIG, DOCKER_CONFIG and
    # XDG_CONFIG_HOME exactly where the caller put them. Only named caches move.
    return {"HF_HUB_CACHE": str(anchor / "hf-hub"),
            "HF_ASSETS_CACHE": str(anchor / "hf-assets"),
            "HF_DATASETS_CACHE": str(anchor / "hf-datasets"),
            "PIP_CACHE_DIR": str(anchor / "pip"), "UV_CACHE_DIR": str(anchor / "uv"),
            "TORCH_HOME": str(anchor / "torch"), "CUDA_CACHE_PATH": str(anchor / "cuda"),
            "TRITON_CACHE_DIR": str(anchor / "triton")}


def docker_check(policy_path, daemon_path):
    policy = json.loads(Path(policy_path).read_text())
    root = checked_root(policy["mode"], policy.get("uuid"), policy.get("allow_boot_storage") is True)
    daemon = json.loads(Path(daemon_path).read_text())
    expected = str(root / "docker")
    need(daemon.get("data-root") == expected, "Docker data-root must match the selected explicit storage mode.")
    need(daemon.get("features", {}).get("containerd-snapshotter") is False,
         "Explicitly disable containerd-snapshotter for this policy; data-root alone does not relocate containerd images.")
    need(not Path(expected).is_symlink(), "Docker data-root must not be a symlink.")
    print(f"Verified Docker storage: {expected}")


def run_workload(mode, expected_uuid, allow_boot, command):
    need(bool(command), "Supply a foreground command after --.")
    root = checked_root(mode, expected_uuid, allow_boot)
    # Pin the filesystem with a directory FD. Cache paths keep referring to that
    # filesystem even if /work is lazily unmounted; they cannot become boot paths.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        checked_root(mode, expected_uuid, allow_boot)  # close the open/check race
        need(os.fstat(fd).st_dev == root.stat().st_dev, "Mount changed while opening storage.")
        try:
            os.mkdir("l4-cache", mode=0o700, dir_fd=fd)
        except FileExistsError:
            pass
        cache_fd = os.open("l4-cache", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            anchor = Path(f"/proc/{os.getpid()}/fd/{cache_fd}")
            env = {**os.environ, **cache_environment(anchor)}
            for key in cache_environment(anchor):
                Path(env[key]).mkdir(mode=0o700, exist_ok=True)
            # Parent holds the FD until the foreground process exits.
            return subprocess.run(command, env=env, cwd=str(root), check=False).returncode
        finally:
            os.close(cache_fd)
    finally:
        os.close(fd)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run")
    run.add_argument("--mode", choices=["work", "boot"], required=True)
    run.add_argument("--uuid")
    run.add_argument("--allow-boot-storage", action="store_true")
    run.add_argument("command", nargs=argparse.REMAINDER)
    docker = sub.add_parser("docker-check")
    docker.add_argument("--policy", default="/etc/l4-storage.json")
    docker.add_argument("--daemon", default="/etc/docker/daemon.json")
    args = p.parse_args(argv)
    if args.action == "docker-check":
        docker_check(args.policy, args.daemon)
        return 0
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    return run_workload(args.mode, args.uuid, args.allow_boot_storage, command)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (GuardError, OSError, ValueError, KeyError) as error:
        print(f"Storage guard refused: {error}", file=sys.stderr)
        sys.exit(1)
