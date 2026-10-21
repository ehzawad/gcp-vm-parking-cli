"""Mac CLI entry point. All cloud writes require --execute."""
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
    p = argparse.ArgumentParser(prog="l4ctl", description="Preview or manage the existing L4 VM. Default: dry run.")
    p.add_argument("--project", default=os.environ.get("L4CTL_PROJECT", "example-project"))
    p.add_argument("--zone", default=os.environ.get("L4CTL_ZONE", "asia-southeast1-c"))
    p.add_argument("--vm", default=os.environ.get("L4CTL_VM", "gpu-vm"))
    p.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parent.parent / ".l4-state")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="Allow future real cloud changes; init also requires numeric IDs.")
    mode.add_argument("--dry-run", action="store_true", help="Preview only (the default).")
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
    storage.add_argument("size", type=int, nargs="?")
    storage.add_argument("--disk-rate", type=str, help="User-verified currency amount per provisioned GiB-month.")
    storage.add_argument("--snapshot-rate", type=str, help="User-verified currency amount per snapshot GiB-month.")
    storage.add_argument("--currency", default="USD")
    storage.add_argument("--price-source", help="Source/date for both supplied rates; no built-in prices.")
    snap = sub.add_parser("snapshot", help="Create one retained standard snapshot while stopped.")
    snap.add_argument("disk", nargs="?", help="Omit for boot; otherwise enrolled work disk name.")
    snap.add_argument("--name", dest="snapshot")
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
    print("L4 VM: 1) up  2) stop  3) status  4) storage  5) park  6) cancel")
    try:
        answer = input("Choose [3]: ").strip() or "3"
    except (EOFError, KeyboardInterrupt):
        return None
    return {"1": "up", "2": "stop", "3": "status", "4": "storage", "5": "park"}.get(answer)


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
    cloud = FixtureCloud(json.loads(args.fixture.read_text())) if args.fixture else ComputeCloud()
    require(not (args.fixture and args.execute), "--fixture and --execute cannot be combined.")
    store = Store(args.state_dir, target)
    mode = "EXECUTE" if args.execute else "DRY RUN"
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
            print(json.dumps(journal, indent=2))
            if args.execute and journal["status"] == "active":
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
            if args.execute:
                require(args.vm_id == new_state["vm_id"] and args.boot_id == str(new_state["boot"]["id"]),
                        "init --execute requires matching --vm-id and --boot-id from the preview.")
                store.save("state.json", new_state)
                print("Pinned locally. No cloud changes.")
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
                print(f"Retained snapshots: {len(state['snapshots'])}; work disks: {len(state['work'])}; test disks: {len(state['tests'])}")
            else:
                print("Not enrolled. Preview init, then pin the displayed numeric IDs before execution.")
            if journal and journal["status"] == "active":
                print("Unfinished journal: recover before any new mutation.")
            return 0
        # Offline previews may derive enrollment, never write it.
        if state is None and not args.execute:
            state = planner.initial_state()
            planner.state = state
            print("Preview uses current inventory; init must pin IDs before execution.")
        require(state is not None, "Run init before --execute.")
        if command == "storage":
            storage_report(state, cloud, target, args)
            if args.size is None:
                return 0
            action, options = "resize", {"size": args.size}
        elif command == "work-disk":
            if args.work_action == "list":
                print(json.dumps(state["work"], indent=2))
                return 0
            action, options = "work-" + args.work_action, vars(args)
        elif command == "snapshot":
            action, options = command, {"role": "work" if args.disk else "boot", "disk": args.disk, "snapshot": args.snapshot}
        elif command == "stop":
            after = args.after
            if after is None and sys.stdin.isatty():
                try:
                    answer = input("After stopping: [Enter] keep disks; s snapshot/keep; p snapshot/park; c cancel: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = "c"
                if answer in {"c", "cancel"}:
                    print("Cancelled; all data kept.")
                    return 0
                require(answer in {"", "s", "p"}, "Unknown choice; no cloud changes.")
                after = {"s": "snapshot", "p": "park"}.get(answer, "keep")
            action, options = command, {"after": after or "keep", "snapshot": args.snapshot}
        else:
            action, options = command, vars(args)
        require(not journal or journal["status"] != "active", "Unfinished journal: run recover; no new plan can overwrite it.")
        plan = planner.plan(action, **options)
        print(json.dumps(plan, indent=2))
        if not args.execute:
            print("Preview only: no cloud requests sent for this plan; no lifecycle journal written.")
            return 0
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
