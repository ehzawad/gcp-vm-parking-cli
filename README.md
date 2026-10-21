# L4 GPU VM lifecycle CLI

Simple commands for an existing Google Cloud GPU VM. Keep its 70 GB or larger boot disk for small workloads, add optional work disks for large jobs, and park storage behind a verified snapshot when explicitly approved.

## Everyday commands

After configuration and local enrollment:

```sh
l4 status                    # Read current VM/disk identities
l4 snapshots                 # Read snapshots and their stored sizes
l4 up                        # Show a plan, then ask before start/restore
l4 stop                      # Show a plan, then stop and KEEP all disks
l4 park                      # Snapshot, verify, then require exact deletion text
l4 storage                   # Read capacity and storage-cost formulas
l4 storage add large-job 200  # Plan a separate 200 GB work disk, then ask
l4 recover                   # Show an unfinished plan, then ask to reconcile it
```

**Terminal commands show a readable plan and ask `[y/N]` before cloud changes. Enter, EOF, or cancellation keeps data.** `l4 stop` retains all disks. `l4 park` additionally requires the full `DELETE projects/.../disks/NAME id=NUMERIC_ID` phrase after snapshot verification; a generic yes never approves disk removal. Stopping, retaining a snapshot, and deletion are separately visible plan steps.

Use explicit plan mode whenever you only want to inspect:

```sh
l4 --plan up
l4 --plan park
l4 --plan storage add large-job 200
```

`--dry-run` aliases `--plan`. Without an interactive terminal, mutation commands print **PLAN ONLY** and send no mutation, even with legacy `--execute`. The flag does not bypass terminal approval. Every default, plan-only, fixture and cancellation path is tested with mocked mutations.

## Setup and optional launcher

Python 3.10+ and the user's existing gcloud login are required for live reads. There are no third-party Python runtime dependencies. No login, credential creation, security changes, shell-profile editing or replacement of existing VM/storage commands is performed.

From the checkout, these commands work without installation or credentials:

```sh
python3 -m l4ctl --help
python3 -m l4ctl --fixture examples/stopped.json status
python3 -m l4ctl --fixture examples/stopped.json park
python3 -m unittest discover -v
```

Fixture mode is always offline and rejects `--execute`. It uses the fixture's target independently of private live configuration. It may create a local lock file, but never writes lifecycle state or sends requests for the plan.

Configure actual existing resources with `L4CTL_PROJECT`, `L4CTL_ZONE`, `L4CTL_VM`, global flags, or a private JSON file outside the repository. Example `config.local.json` (ignored; use your actual values):

```json
{
  "project": "your-project-id",
  "zone": "asia-southeast1-c",
  "vm": "your-existing-gpu-vm",
  "state_dir": "/absolute/private/path/to/l4-state"
}
```

Install only a new lightweight launcher in an **existing user-owned directory already on PATH**:

```sh
python3 tools/install_launcher.py --bin-dir ~/.local/bin --config /absolute/private/config.local.json
l4 --help
l4 init
```

The installer refuses any existing `l4` command or path, including broken symlinks. It does not change PATH, shell files, existing commands, authentication or system settings. The source checkout and private config must stay at their installed paths; move them only with a reviewed launcher update. `l4 init` reads the VM/disk identities and asks before pinning them locally. The selected VM must have exactly one existing boot disk of at least 70 GB. Explicit matching `--vm-id` and `--boot-id` with `--execute init` can authorize this local-only write without a terminal.

Global options go before the subcommand. Explicit flags override environment values, which override private config, then generic examples. Use one durable state directory for each target; do not operate multiple independent state directories concurrently.

## Snapshot list/import and restore

```sh
l4 snapshots
l4 snapshots import existing-backup
l4 --plan test-restore existing-backup --disk restore-check
```

Import verifies READY standard-snapshot metadata, exact snapshot/source URIs and numeric IDs, source size and the enrolled disk identity. It registers the backup locally and records **restore_verified=false**. Import sends no cloud mutation. It also checks source-attachment/VM-start timestamps before allowing an imported copy to qualify for any source-removal check; stale or missing freshness information cannot approve removal. Passing `--id EXPECTED_NUMERIC_ID` prevents name-reuse adoption. A matching ID with `--execute snapshots import` authorizes only the local registration.

`up` has no snapshot argument. With the original boot disk attached, it starts that original disk after confirmation. After a completed CLI park, it creates a new disk from the recorded snapshot, verifies `sourceSnapshotId`, attaches it, and starts the preserved VM after plan approval. Restore size is at least 70 GB and the original source size. A snapshot itself cannot boot. Boot disk type, interface, device name and original auto-delete policy are retained. The VM object, metadata, GPU/machine settings and networking are preserved; configuration drift fails closed. RUNNING does not imply SSH, GPU-driver, Docker or application readiness.

**READY is not a tested restore.** The separate `test-restore` plan allocates an unattached disk, preserving source and snapshot. It verifies creation/source identity only. Filesystem checks, known-file checksums, application recovery and isolated boot testing require separately approved temporary resources. Avoid mounting duplicate filesystem UUIDs together on production. Test disks remain billed until exact-confirmation `test-cleanup NAME`; attached test disks cannot be cleaned up. No snapshot pruning exists. Disk creation from a real snapshot has been exercised, but no successful live restored-OS boot validation is recorded. A CPU-helper attempt ended without retained probe evidence; full recovery remains unverified.

## Work disks and boot space

`l4 storage` reports provisioned allocation, not guest filesystem free space. Inspect `df -h /` in an existing SSH session to decide whether a small workload fits. No shrink to 50/60 GB is attempted.

`l4 storage add NAME SIZE_GB` creates a blank independent balanced work disk after approval. `l4 work-disk attach NAME` attaches it while stopped, with auto-delete false. Formatting and mounting are explicit guest-administration steps. `work-disk detach`, `work-disk remove` and `work-disk restore` are separately scoped. Removal snapshots/verifies the selected work disk and then requires exact deletion text; boot name, numeric ID and boot attachments are protected.

`l4 storage TOTAL_GB` permanently grows cloud boot capacity after confirmation; it does not expand the guest partition/filesystem or claim the space is usable. Use an existing checked guest-growth procedure separately. Do not operate an old helper concurrently with this CLI.

See [guest storage](docs/guest-storage.md) for fail-closed `/work` cache/Docker guards, explicit small-workload boot mode and token-path separation. Those Linux templates are not automatically installed or claimed to be live-tested. Docker's separate containerd image store must be explicitly accounted for; the supplied guard refuses unreviewed storage fallback.

## Recovery and safety limits

Per-target kernel locking, private atomic/fsynced state, UUID request IDs and a durable submitting record precede every mutation. Returned operation URIs are pinned, polled with timeouts and followed by identity/postcondition checks. Created resources carry an operation label. Uncertain requests are never automatically resent; recovery polls the same operation or proves the postcondition. A failure or unprovable result stops for inspection. Never delete a journal to force a retry.

`l4 recover` displays the saved steps and asks before reconciling/continuing unsent steps. `l4 recover --cancel-pending` can cancel only unsent steps after completed local effects replay; any submitting/uncertain request blocks cancellation. Destructive approval is not saved across processes. Stopped state, users, exact IDs/URIs, boot protection, snapshot provenance and backup freshness are rechecked after confirmation and immediately before requests.

The lock cannot coordinate console changes, another machine or another state directory. Disk deletion uses a resource name; numeric-ID rechecks cannot atomically exclude an external name-reuse race between check and request. Maintain exclusive administration during destructive maintenance. Manual drift, missing state, failed reads and expired authentication fail closed. No VM deletion, missing-VM recreation, Local SSD parking, managed-instance-group management or customer-supplied encryption-key workflow is implemented.

## Cost and verification

Disks remain billed while stopped. Parking avoids the source allocation charge only after its deletion; snapshots, retained work/test disks and other resources remain billable. No prices are fabricated or hard-coded as live quotes. `storage --disk-rate RATE --snapshot-rate RATE --price-source 'URL checked DATE'` estimates enrolled storage from user-verified regional per-GiB-month rates. Snapshot allocation uses reported `storageBytes/2^30`, which may change with incremental allocation. Compute/GPU hours, IPs, transfer, licenses, tax and discounts are excluded. See [Google pricing](https://cloud.google.com/compute/disks-image-pricing).

The live transport captures a token from existing gcloud authentication in memory, never logs/saves it, and uses Compute REST async operations. Metadata contents, encryption-key fields, tokens and local configuration are excluded from committed data. The repository has generic examples only.

See [commands](docs/commands.md), [engineering references](docs/engineering-guidance.md), [safety review](docs/safety-review.md), and [verification](docs/verification.md). Local launcher resolution and real read-only status/snapshot reads are checked separately from mocked lifecycle execution. No live restore, start, attachment or deletion was run as part of the UX verification.
