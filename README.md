# L4 lifecycle CLI (workspace preview)

This Python CLI manages a selected existing Google Cloud GPU VM. The included target `example-project / asia-southeast1-c / gpu-vm` is illustrative. It retains an existing **70 GB or larger boot disk**, allows small jobs to use its available filesystem space, and adds independent work disks only when needed. Python 3.10+ is required; there are no runtime Python dependencies.

**Implementation/testing used mocks and offline fixtures only. No cloud resource was changed, no credentials were read by a test, and no installed command was replaced. A real restore has not been tested.** The example numeric IDs are synthetic; enrollment must discover and explicitly pin the real IDs later.

## Try it safely now

From the repository directory:

```sh
python3 -m l4ctl --help
python3 -m l4ctl --fixture examples/stopped.json status
python3 -m l4ctl --fixture examples/stopped.json up
python3 -m l4ctl --fixture examples/stopped.json stop --after keep
python3 -m l4ctl --fixture examples/stopped.json park
python3 -m unittest discover -v
```

Every command defaults to **dry run**. `--dry-run` is also explicit. Fixture mode is offline and rejects `--execute`. A preview may create a local lock directory; it does not write enrollment state or a lifecycle journal. A live preview reads the API using the existing gcloud identity. Put global flags **before** the command.

## Enrollment and future use

The repository contains generic defaults. Set `L4CTL_PROJECT`, `L4CTL_ZONE`, and `L4CTL_VM` to your actual existing resources before live reads or execution; alternatively pass global `--project`, `--zone`, and `--vm` flags. Use the same target and state directory consistently. Offline fixture commands require no configuration.

These are migration/use examples, **not actions performed during implementation**. Keep one durable state directory for the target and back it up. The default is `.l4-state` beside the source tree; use an absolute `--state-dir` if you relocate/install the package. Do not run concurrent copies with different state directories.

```sh
# Read-only discovery, using existing gcloud authentication.
python3 -m l4ctl init

# Locally pin IDs from that preview. Replace these placeholders with actual IDs.
python3 -m l4ctl --execute init --vm-id ACTUAL_VM_ID --boot-id ACTUAL_BOOT_DISK_ID

# Start the existing VM, or recreate its parked boot disk then attach/start it.
python3 -m l4ctl up
python3 -m l4ctl --execute up

# Stop compute; keep every disk and snapshot.
python3 -m l4ctl --execute stop --after keep

# Keep a backup and the source disk (VM must already be stopped).
python3 -m l4ctl --execute snapshot --name chosen-boot-backup

# Stop, snapshot, verify, then ask before removing the boot disk.
python3 -m l4ctl park
python3 -m l4ctl --execute park --snapshot chosen-park-backup
```

`on`/`start` alias `up`; `off` aliases `stop`. Interactive invocation without a command shows a menu. Interactive `stop` offers keep (Enter/default), snapshot-and-keep, snapshot-and-park, or cancel. Noninteractive `stop` defaults to keep. `stop --after keep` has no deletion path. Cancellation keeps source data; cancellation during recovery can leave a source disk detached, and `up` can reattach the pinned boot disk.

Parking asks for this entire phrase **after the snapshot has completed and been verified**:

```text
DELETE projects/example-project/zones/asia-southeast1-c/disks/gpu-vm-boot-70gb id=ACTUAL_DISK_ID
```

Enter/cancel retains the disk and the new snapshot. Automation must provide the same exact phrase through global `--confirm-delete`; there is no generic `--yes`. This design does not treat prior discussion as deletion approval. Previewing `park` never asks for or stores approval.

## What park/up preserve

`park` keeps the **stopped VM object**, including GPU/machine configuration, networking, service account and metadata. It snapshots only the selected boot disk, verifies the snapshot's READY status, numeric snapshot ID, exact `sourceDisk` URI, numeric `sourceDiskId`, and original disk size. It checks VM restart/disk reattachment timestamps before removal. After confirmation it detaches the boot disk, confirms it is unused, and deletes that exact currently pinned disk name/ID. Work disks remain independent and billed until separately managed.

`up` recreates a zonal disk from the recorded snapshot before attaching it. A snapshot cannot itself be a boot disk. Restore size is at least the source snapshot size, the recorded original disk size, and 70 GB for boot. Disk type, device name, interface and boot auto-delete policy are retained; the current boot disk's auto-delete policy is **true**. VM deletion remains outside this CLI. A restored disk receives a new numeric disk ID, which is pinned after source-snapshot ID verification.

The VM configuration is compared to the enrollment record before execution; drift fails closed. Startup scripts/metadata contents, tokens and encryption keys are excluded from local state. Configuration preservation comes from retaining the VM rather than recreating it. This CLI does not support managed-instance-group automation, Local SSD workflows, customer-supplied encryption keys, or replacing a missing VM.

RUNNING means Compute Engine reports that state. SSH, Docker, GPU drivers and application readiness are not implied. Continue using existing `ssh l4-vm` or the installed `l4-vm ssh`; nothing here changes SSH configuration or keys. Suspend/resume is supported for `up`; parking requires stable RUNNING/TERMINATED status.

## Independent work disks and storage

```sh
python3 -m l4ctl storage
python3 -m l4ctl work-disk list
python3 -m l4ctl --execute work-disk create large-job --size 200
# Attachment changes require the VM stopped. New disks have auto-delete=false.
python3 -m l4ctl --execute work-disk attach large-job
python3 -m l4ctl --execute up

# Later: stop/keep, independently snapshot/remove the work disk after confirmation.
python3 -m l4ctl --execute stop --after keep
python3 -m l4ctl --execute snapshot large-job --name chosen-work-backup
python3 -m l4ctl --execute work-disk detach large-job
python3 -m l4ctl --execute work-disk remove large-job --snapshot another-work-backup
python3 -m l4ctl --execute work-disk restore large-job
python3 -m l4ctl --execute work-disk attach large-job
```

`work-disk remove` always creates its own verified backup before removal and requires the exact disk phrase. The boot name, boot numeric ID, and any boot attachment are protected from work/test removal. Disks created here are blank: formatting and mount setup are deliberate guest administration steps, never automatic. See [guest configuration](docs/guest-storage.md) before using `/work`.

`storage TOTAL_GB` previews permanent boot-disk growth; `--execute` grows only cloud capacity. It never shrinks the disk. It does **not** expand the guest partition/filesystem or claim the space is usable. The existing `l4-storage TOTAL_GB` handles checked guest expansion; after new CLI capacity growth, run the installed helper at that same total size while the VM is running. Do not use both tools concurrently. No resize is necessary for the retained 70 GB setup.

`storage` reports provisioned capacity, not guest free space. Inspect `df -h /` in an existing SSH session to decide whether a small job fits. Boot space holds the OS, installed software, and small workloads together.

## Billing estimates

No prices are hard-coded or fetched as if live. A stopped VM's disks and snapshots remain billable. Parking avoids the boot disk's allocation cost **only after deletion**, and restoration allocates at least 70 GB again. Work and test disks, reserved/external IPs, transfer and other resources can still cost money.

`storage --disk-rate VERIFIED_RATE --snapshot-rate VERIFIED_RATE --price-source 'URL checked YYYY-MM-DD'` calculates tracked storage estimates using user-supplied per-GiB-month rates. `--currency` labels the currency (default USD). Disk estimates use allocation; snapshot estimates use currently reported `storageBytes / 2^30`. This is a full-month approximation for enrolled storage only, excluding compute/GPU usage, IPs, licenses, transfer, tax and discounts. Incremental snapshot allocation can change; snapshot cost is not simply 70 × the disk rate. Review [current regional pricing](https://cloud.google.com/compute/disks-image-pricing) and your actual billing export. No automatic snapshot pruning is implemented.

## Crash recovery

The per-target kernel lock serializes this CLI. State and journal writes use atomic replacement, file fsync and directory fsync, with mode 0600. Each step has a UUID `requestId`; the submitting record is durable **before** any mutation request. The operation URI is saved when returned, polled with a bounded timeout, and followed by resource verification.

```sh
python3 -m l4ctl recover                    # read saved journal
python3 -m l4ctl --execute recover          # poll/reconcile, then continue unsent steps
python3 -m l4ctl --execute recover --cancel-pending
```

`recover` never resends an uncertain request. With a known operation it polls that same URI. With a lost response it inspects the postcondition; created disks/snapshots must carry the original operation label and exact source IDs. If a postcondition cannot prove completion, recovery stops and the journal blocks new mutation plans. **Do not delete/edit the journal to force a retry.** Inspect the saved operation and provider audit logs first. An explicit failed operation closes as failed without issuing subsequent steps. Pending-only cancellation closes unsent steps locally and refuses submitting/uncertain steps. New execution archives the previous closed journal under `history/`.

Destructive approval is held only in memory, so a resumed pending delete asks again. Before sending it, the CLI rechecks stopped state, usage, IDs, boot protection, backup source and freshness, including after the user types confirmation. Completed local effects replay idempotently if a crash occurred between journal and state writes.

The lock cannot prevent changes made in the console, by the old scripts, another machine or another state directory. Compute disk deletion uses a resource name and does not provide an ID precondition in this implementation; a small check/request race remains against an independent actor deleting/recreating that name. Coordinate exclusive maintenance before destructive operations. A manual change, missing state, reused name, failed API read, expired login/token or ambiguous outcome fails closed for inspection. HTTP/API mutation calls are never automatically retried.

## Separate test-restore workflow

Snapshot READY proves completion, **not** filesystem integrity, bootability, application consistency or recoverability. A stopped VM gives a quiescent copy but does not validate an application's prior shutdown. For important data, test recovery before parking away the source.

1. Retain the source disk, stop the VM, create an enrolled snapshot with `snapshot --name chosen-backup`.
2. Preview `python3 -m l4ctl test-restore chosen-backup --disk l4-restore-check`.
3. After separately approving test allocation/cost, use `--execute test-restore ...`. This creates an unattached disk at least the snapshot's original size and verifies its `sourceSnapshotId`; it does not mark the backup as tested.
4. In an isolated inspection environment, attach that **test copy**, check filesystem health without repair/write, mount read-only using filesystem-appropriate options, compare known file checksums, and perform application recovery checks on disposable copies. Avoid exposing duplicate filesystem UUIDs together on a production guest. If bootability matters, separately authorize a temporary isolated VM boot using the copy and appropriate original architecture/settings. Never replace the production boot disk for this test.
5. Record actual observations, IDs and date. No such live filesystem or boot test was run during implementation.
6. Detach the copy from any inspector. `--execute test-cleanup l4-restore-check` removes only the pinned unused test disk with exact confirmation, retaining its snapshot. Cleanup refuses attached disks. Test disks accrue charges until explicitly removed.

## Compatibility and delivery

Compatibility inspection of pre-existing local lifecycle, storage and SSH helpers was read-only. None were replaced. Existing `on` starts/resumes; existing `off` keeps disks; existing `storage` grows boot storage. The published package uses `L4CTL_PROJECT`, `L4CTL_ZONE`, and `L4CTL_VM` environment variables or the corresponding global flags. Enrollment pins the selected VM and boot disk by numeric ID.

The live transport captures `gcloud auth print-access-token` from the already configured gcloud identity in memory and uses Compute REST async operations. It never logs tokens, invokes login, creates credentials, changes gcloud configuration, or saves credential files. Missing authentication stops execution. Tests and fixture previews do not invoke gcloud or access the network.

Keep using `python3 -m l4ctl` from this directory until an installation/migration is explicitly approved. Packaging metadata is supplied for future installation as `l4ctl`; no installation was performed. Do not replace existing symlinks until enrollment, preview review and separately authorized live validation succeed. The guest helper/drop-ins are templates; they were not copied to the VM.

See [engineering references](docs/engineering-guidance.md), [critical review](docs/safety-review.md), and [verification evidence](docs/verification.md).

For a compact reference, see [useful commands](docs/commands.md).
