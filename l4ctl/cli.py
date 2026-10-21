"""Everyday L4 commands: read, explicitly plan, or interactively approve changes."""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys

from .core import (Cancelled, ComputeCloud, Engine, FixtureCloud, Planner,
                   SafetyError, Store, Target, relative, require, rid)


def parser():
    p = argparse.ArgumentParser(prog="l4", description="Read VM status or review and confirm a plan. --plan never changes cloud resources.")
    p.add_argument("--config", type=Path, default=os.environ.get("L4CTL_CONFIG"), help="Private local JSON configuration; never required for offline examples.")
    p.add_argument("--project")
    p.add_argument("--zone")
    p.add_argument("--vm")
    p.add_argument("--state-dir", type=Path)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="Apply mode; cloud changes still require confirmation in a terminal.")
    mode.add_argument("--plan", "--dry-run", dest="dry_run", action="store_true", help="Show a plan only; never prompt to execute.")
    p.add_argument("--fixture", type=Path, help="Offline inventory; cloud writes are impossible.")
    p.add_argument("--timeout", type=int, default=600, help="Seconds per operation polling phase (1–3600).")
    p.add_argument("--confirm-delete", help="Exact DELETE projects/.../disks/NAME id=ID phrase; no generic --yes.")
    sub = p.add_subparsers(dest="command")
    init = sub.add_parser("init", help="Pin the existing VM and 70 GB boot disk locally.")
    init.add_argument("--vm-id")
    init.add_argument("--boot-id")
    sub.add_parser("status")
    sub.add_parser("up", aliases=["on", "start"], help="Start/resume or restore the parked boot disk, then start.")
    stop = sub.add_parser("stop", aliases=["off"], help="Stop; keep all disks by default.")
    stop.add_argument("--after", choices=["keep", "snapshot", "park"])
    stop.add_argument("--snapshot", help="Optional new snapshot name.")
    park = sub.add_parser("park", help="Stop, snapshot, verify source, confirm, detach and delete boot disk.")
    park.add_argument("--snapshot", help="Optional new snapshot name.")
    storage = sub.add_parser("storage", help="Show provisioned space/cost formula or grow boot disk.")
    storage.add_argument("size", nargs="?", help="New boot total GB, or: add NAME SIZE_GB.")
    storage.add_argument("disk", nargs="?")
    storage.add_argument("work_size", type=int, nargs="?")
    storage.add_argument("--disk-rate", type=str, help="User-verified currency amount per provisioned GiB-month.")
    storage.add_argument("--snapshot-rate", type=str, help="User-verified currency amount per snapshot GiB-month.")
    storage.add_argument("--currency", default="USD")
    storage.add_argument("--price-source", help="Source/date for both supplied rates; no built-in prices.")
    snap = sub.add_parser("snapshot", help="Create one retained standard snapshot while stopped.")
    snap.add_argument("disk", nargs="?", help="Omit for boot; otherwise enrolled work disk name.")
    snap.add_argument("--name", dest="snapshot")
    snapshots = sub.add_parser("snapshots", help="List backups; import NAME registers an existing verified snapshot locally.")
    snapshots.add_argument("snapshot_action", nargs="?", choices=["import"])
    snapshots.add_argument("snapshot_name", nargs="?")
    snapshots.add_argument("--id", dest="snapshot_id", help="Expected numeric ID for local import.")
    work = sub.add_parser("work-disk", help="Independent work disks; never formats or mounts.")
    ops = work.add_subparsers(dest="work_action", required=True)
    ops.add_parser("list")
    create = ops.add_parser("create")
    create.add_argument("disk")
    create.add_argument("--size", type=int, required=True)
    for action in ("attach", "detach", "remove", "restore"):
        child = ops.add_parser(action)
        child.add_argument("disk")
        if action == "remove":
            child.add_argument("--snapshot", help="Back up this work disk before exact confirmation/removal.")
    test = sub.add_parser("test-restore", help="Create a separate verification disk; does not certify data/boot.")
    test.add_argument("snapshot")
    test.add_argument("--disk", required=True)
    cleanup = sub.add_parser("test-cleanup", help="Explicitly delete an unused pinned test disk.")
    cleanup.add_argument("disk")
    recovery = sub.add_parser("recover", help="Reconcile saved operations; never reissue an uncertain request.")
    recovery.add_argument("--cancel-pending", action="store_true", help="Close remaining unsent steps only; refuses any uncertain request.")
    return p


def choose():
    print("L4 VM: 1) up  2) stop  3) status  4) storage  5) park  6) snapshots  7) cancel")
    try:
        answer = input("Choose [3]: ").strip() or "3"
    except (EOFError, KeyboardInterrupt):
        return None
    return {"1": "up", "2": "stop", "3": "status", "4": "storage", "5": "park", "6": "snapshots"}.get(answer)


def approve(args, message, local=False, exact_id=False):
    if args.dry_run or args.fixture:
        print("PLAN ONLY: nothing applied.")
        return False
    if local and args.execute and exact_id:
        return True  # explicit matching IDs authorize only a local ledger write
    if not sys.stdin.isatty():
        print("PLAN ONLY: no terminal confirmation available. Run this command in a terminal to review and apply it.")
        return False
    try:
        accepted = input(message + " [y/N]: ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        accepted = False
    if not accepted:
        print("Cancelled; no further changes applied.")
    return accepted


def show_plan(plan):
    print("Plan:")
    if not plan["steps"]:
        print("  Already in the requested state; no changes needed.")
    descriptions = {"start": "Start the existing VM (compute/GPU charges begin)",
                    "resume": "Resume the existing VM (compute/GPU charges begin)",
                    "stop": "Stop the VM; retain all disks", "snapshot": "Create and verify a retained snapshot",
                    "detach": "Detach this disk from the stopped VM", "delete": "DELETE this source disk after exact typed confirmation",
                    "restore": "Create a new disk from the pinned snapshot", "create": "Allocate a new blank work disk",
                    "attach": "Attach the pinned disk", "resize": "Permanently grow cloud disk capacity"}
    for index, step in enumerate(plan["steps"], 1):
        detail = descriptions.get(step["kind"], step["kind"])
        if step.get("disk"):
            disk = step["disk"]
            detail += f" — {disk['name']}" + (f" id={disk['id']}" if disk.get("id") else "")
        if step.get("snapshot"):
            detail += f"; snapshot={step['snapshot']}"
        if step.get("size"):
            detail += f"; {step['size']} GB"
        print(f"  {index}. {detail} [{step.get('phase', 'pending')}]")


def quote_rate(value):
    if value is None:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise SafetyError("Rate must be a finite, nonnegative decimal.") from None
    require(amount.is_finite() and amount >= 0, "Rate must be finite and nonnegative.")
    return amount


def storage_report(state, cloud, target, args):
    disk_gb = 0
    for record in [state["boot"], *state["work"].values(), *state["tests"].values()]:
        disk = cloud.get(target.path("disks", record["name"]))
        if disk:
            require(rid(disk) == str(record["id"]), "Disk numeric ID changed; cannot estimate wrong resource.")
            size = int(disk["sizeGb"])
            disk_gb += size
            print(f"{record['name']}: id={rid(disk)} {size} provisioned GiB ({disk['type'].rsplit('/', 1)[-1]})")
    snap_bytes = 0
    for snap in state["snapshots"].values():
        actual = cloud.get(target.path("snapshots", snap["name"]))
        require(actual and rid(actual) == str(snap["id"]), "Snapshot missing/ID changed; cost inventory incomplete.")
        require("storageBytes" in actual, "Snapshot stored bytes unavailable; cannot estimate.")
        snap_bytes += int(actual["storageBytes"])
    print(f"Tracked disk allocation: {disk_gb} GiB; snapshot-reported storage: {snap_bytes} bytes.")
    print("Disk allocation is billed while stopped; small workloads can use existing boot free space.")
    print("Free filesystem space requires guest df; provisioned GiB is not free space. No shrink to 50/60 GB.")
    dr, sr = quote_rate(args.disk_rate), quote_rate(args.snapshot_rate)
    if dr is not None or sr is not None:
        require(bool(args.price_source), "Supply --price-source with rate provenance/date.")
        require(all(r.get("type", "").endswith("/pd-balanced") for r in
                    [state["boot"], *state["work"].values(), *state["tests"].values()]), "A single disk rate cannot cover mixed types.")
        print(f"Rates supplied by user ({args.currency}); source: {args.price_source}")
        if dr is not None:
            print(f"Tracked disks, full-month approximation: {disk_gb} × {dr} = {disk_gb * dr:.2f} {args.currency}/month")
        if sr is not None:
            used = Decimal(snap_bytes) / Decimal(2 ** 30)
            print(f"Snapshot-reported bytes / 2^30 × {sr} = {used * sr:.2f} {args.currency}/month (approximate, may change)")
    else:
        print("No live prices loaded. Estimate = provisioned GiB × verified regional disk rate + snapshot billed storage × snapshot rate.")
    print("This covers enrolled storage only. Compute/GPU hours, IP addresses, licenses, transfer, taxes and discounts are excluded.")
    print("Snapshots are incremental; storageBytes can change. Parking saves disk allocation only after source deletion.")


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    explicit_target = {key: getattr(args, key) for key in ("project", "zone", "vm")}
    config = json.loads(args.config.read_text()) if args.config else {}
    require(isinstance(config, dict), "Local config must be a JSON object.")
    args.project = args.project or os.environ.get("L4CTL_PROJECT") or config.get("project", "example-project")
    args.zone = args.zone or os.environ.get("L4CTL_ZONE") or config.get("zone", "asia-southeast1-c")
    args.vm = args.vm or os.environ.get("L4CTL_VM") or config.get("vm", "gpu-vm")
    args.state_dir = args.state_dir or config.get("state_dir") or Path(__file__).resolve().parent.parent / ".l4-state"
    fixture_data = json.loads(args.fixture.read_text()) if args.fixture else None
    if fixture_data is not None:
        vm_paths = [relative(path).split("/") for path in fixture_data["resources"] if "/instances/" in path]
        require(len(vm_paths) == 1, "An offline fixture must contain exactly one VM.")
        parts = vm_paths[0]
        require(len(parts) == 6 and parts[0] == "projects" and parts[2] == "zones" and parts[4] == "instances", "Invalid fixture VM URI.")
        for key, value in zip(("project", "zone", "vm"), (parts[1], parts[3], parts[5])):
            setattr(args, key, explicit_target[key] or value)
    if args.command is None:
        if not sys.stdin.isatty():
            p.print_help()
            return 0
        command = choose()
        if command is None:
            print("Cancelled; no cloud changes.")
            return 0
        # Parse command defaults while retaining global settings.
        args = p.parse_args([command], namespace=args)
    require(1 <= args.timeout <= 3600, "--timeout must be 1–3600 seconds.")
    target = Target(args.project, args.zone, args.vm)
    cloud = FixtureCloud(fixture_data) if args.fixture else ComputeCloud()
    require(not (args.fixture and args.execute), "--fixture and --execute cannot be combined.")
    store = Store(args.state_dir, target)
    mode = "OFFLINE" if args.fixture else "PLAN ONLY" if args.dry_run else "TARGET"
    print(f"{mode}: {target.project} / {target.zone} / {target.vm}")
    with store.lock():
        state = store.load("state.json")
        journal = store.load("journal.json")
        command = {"on": "up", "start": "up", "off": "stop"}.get(args.command, args.command)

        def confirm(phrase):
            print("Source deletion leaves the snapshot as the recovery copy. READY does not prove a tested restore.")
            print("Exact destructive confirmation: " + phrase)
            if args.confirm_delete is not None:
                return args.confirm_delete == phrase
            if not sys.stdin.isatty():
                return False
            try:
                return input("Type the full phrase, or Enter to keep data: ").strip() == phrase
            except (EOFError, KeyboardInterrupt):
                return False

        if command == "recover":
            require(journal is not None, "No journal exists.")
            print(f"Recovery journal: {journal['status']}")
            show_plan(journal)
            if journal["status"] == "active" and approve(args, "Continue this recovery plan?", local=args.cancel_pending, exact_id=args.cancel_pending):
                require(state is not None, "Enrollment state missing; restore its local backup.")
                engine = Engine(target, cloud, store, state, args.timeout, confirm=confirm)
                if args.cancel_pending:
                    require(all(s["phase"] in {"pending", "done"} for s in journal["steps"]),
                            "Cannot cancel an uncertain/submitting request; reconcile it first.")
                    for step in journal["steps"]:
                        if step["phase"] == "done":
                            engine.effect(step, step["result"])
                    journal["status"] = "cancelled"
                    store.save("journal.json", journal)
                    print("Unsent steps cancelled locally; already completed changes/data retained.")
                else:
                    engine.apply(journal, recover=True)
                    print("Saved request reconciled; remaining pending steps completed. No uncertain request was resubmitted.")
            return 0
        planner = Planner(target, cloud, state)
        print(f"VM numeric ID: {rid(planner.vm)}; state: {planner.vm['status']}")
        if command == "init":
            require(state is None, "Already enrolled; init never replaces pinned identities.")
            new_state = planner.initial_state()
            print(f"Boot disk: {new_state['boot']['name']} id={new_state['boot']['id']}; {new_state['boot']['sizeGb']} GB")
            if args.vm_id is not None or args.boot_id is not None:
                require(args.vm_id == new_state["vm_id"] and args.boot_id == str(new_state["boot"]["id"]), "init requires matching --vm-id and --boot-id.")
            if approve(args, "Pin these exact VM and boot-disk identities locally?", local=True,
                       exact_id=args.vm_id == new_state["vm_id"] and args.boot_id == str(new_state["boot"]["id"])):
                store.save("state.json", new_state)
                print("Pinned locally. No cloud changes.")
            return 0
        if command == "snapshots":
            if args.snapshot_action == "import":
                require(state is not None, "Run l4 init before registering snapshots.")
                require(args.snapshot_name is not None, "Use: l4 snapshots import NAME [--id NUMERIC_ID].")
                require(not journal or journal["status"] != "active", "Recover the unfinished journal before changing the ledger.")
                snap = planner.import_snapshot(args.snapshot_name, args.snapshot_id)
                print(f"Register snapshot {snap['name']} id={snap['id']}; sourceDiskId={snap['sourceDiskId']}; {snap['diskSizeGb']} GB; READY")
                print("Local registration only. A live restore has NOT been validated.")
                if approve(args, "Register this verified snapshot locally?", local=True, exact_id=args.snapshot_id == str(snap["id"])):
                    # Re-read provider metadata after confirmation, before changing the ledger.
                    snap = Planner(target, cloud, state).import_snapshot(args.snapshot_name, str(snap["id"]))
                    state["snapshots"][snap["name"]] = snap
                    store.save("state.json", state)
                    print("Snapshot registered locally; no cloud changes.")
                return 0
            require(args.snapshot_name is None, "Use: l4 snapshots, or l4 snapshots import NAME.")
            inventory = state or planner.initial_state()
            ids = {str(d["id"]) for d in [inventory["boot"], *inventory["work"].values()]}
            snapshots = {s["name"]: s for s in cloud.list_snapshots(target.project, ids)}
            for saved in inventory["snapshots"].values():
                actual = cloud.get(target.path("snapshots", saved["name"]))
                require(actual and rid(actual) == str(saved["id"]), "Registered snapshot missing or name reused.")
                snapshots[saved["name"]] = actual
            if not snapshots:
                print("No snapshots found for the enrolled/current disk IDs.")
            for snap in sorted(snapshots.values(), key=lambda s: s.get("creationTimestamp", s["name"]), reverse=True):
                stored = f"{int(snap['storageBytes']) / 2**30:.2f} GiB stored" if "storageBytes" in snap else "stored size unavailable"
                print(f"{snap['name']}  {snap.get('status', 'UNKNOWN')}  id={rid(snap)}  {stored}  {','.join(snap.get('storageLocations', []))}  {'registered' if snap['name'] in inventory['snapshots'] else 'unregistered'}")
            print("Snapshot completion is shown; live restore validation is not implied.")
            return 0
        if command == "status":
            for attachment in planner.vm.get("disks", []):
                disk = cloud.get(relative(attachment["source"]))
                require(disk is not None, "Attached disk cannot be read; inventory incomplete.")
                print(f"{'boot' if attachment.get('boot') else 'work'}: {attachment['source']} id={rid(disk)} {disk['sizeGb']} GB autoDelete={attachment.get('autoDelete', False)}")
                if state:
                    pinned = state["boot"] if attachment.get("boot") else state["work"].get(disk["name"])
                    require(pinned is None or (pinned["name"] == disk["name"] and str(pinned["id"]) == rid(disk)),
                            "Attached disk identity differs from enrollment; inspect name reuse.")
            if state:
                print(f"Boot ledger: {state['boot']['name']} id={state['boot']['id']} parked={state['boot'].get('parked', False)}")
                print(f"Retained snapshots: {len(state['snapshots'])}; tracked work disks: {sum(not d.get('parked', False) for d in state['work'].values())}; tracked test disks: {sum(not d.get('parked', False) for d in state['tests'].values())}")
            else:
                print("Not enrolled. Preview init, then pin the displayed numeric IDs before execution.")
            if journal and journal["status"] == "active":
                print("Unfinished journal: recover before any new mutation.")
            return 0
        # Offline previews may derive enrollment, never write it.
        enrolled = state is not None
        if state is None:
            state = planner.initial_state()
            planner.state = state
            print("Plan uses current inventory; run l4 init to pin IDs before applying cloud changes.")
        if command == "storage":
            storage_report(state, cloud, target, args)
            if args.size is None:
                require(args.disk is None and args.work_size is None, "Use l4 storage, l4 storage TOTAL_GB, or l4 storage add NAME SIZE_GB.")
                return 0
            if args.size in {"add", "create"}:
                require(args.disk is not None and args.work_size is not None, "Use: l4 storage add NAME SIZE_GB.")
                action, options = "work-create", {"disk": args.disk, "size": args.work_size}
            else:
                require(args.disk is None and args.work_size is None, "Boot resize accepts one total size.")
                action, options = "resize", {"size": int(args.size)}
        elif command == "work-disk":
            if args.work_action == "list":
                print(json.dumps(state["work"], indent=2))
                return 0
            action, options = "work-" + args.work_action, vars(args)
        elif command == "snapshot":
            action, options = command, {"role": "work" if args.disk else "boot", "disk": args.disk, "snapshot": args.snapshot}
        elif command == "stop":
            after = args.after or "keep"
            if after == "keep":
                print("Storage choice: keep all disks (default). Optional: stop --after snapshot; park is a separate explicit choice.")
            action, options = command, {"after": after or "keep", "snapshot": args.snapshot}
        else:
            action, options = command, vars(args)
        require(not journal or journal["status"] != "active", "Unfinished journal: run recover; no new plan can overwrite it.")
        plan = planner.plan(action, **options)
        show_plan(plan)
        if not plan["steps"]:
            return 0
        if not approve(args, "Apply this plan?"):
            return 0
        require(enrolled, "Run l4 init before applying cloud changes.")
        if journal:
            store.save(f"history/{journal['id']}.json", journal)
        Engine(target, cloud, store, state, args.timeout, confirm=confirm).apply(plan)
        print("Plan completed; retained snapshots were not pruned.")
        if action == "resize":
            print("Cloud capacity grew only. Inspect/grow the guest partition and filesystem separately; see README.")
        if action == "test-restore":
            print("Restored disk/source IDs verified only. Data integrity, application recovery and boot are NOT tested; follow README test-restore checks.")
        if action == "up":
            print("Compute reports RUNNING. SSH, Docker and application readiness have not been checked.")
        return 0


def entry():
    try:
        return main()
    except Cancelled as error:
        print(str(error), file=sys.stderr)
        return 0
    except (SafetyError, OSError, ValueError, KeyError) as error:
        print(f"Stopped safely: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Keep source data; inspect status/recover before retrying.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(entry())
