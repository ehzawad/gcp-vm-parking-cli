# Useful commands

Run these from the repository checkout with Python 3.10+; no package installation is necessary. Global options such as `--execute`, `--project`, and `--state-dir` go before the subcommand. No live mutating command below was run during development/publication.

Safe offline commands need no credentials or configuration:

```sh
python3 -m l4ctl --help
python3 -m l4ctl --fixture examples/stopped.json status
python3 -m l4ctl --fixture examples/stopped.json park
```

Before live reads, configure your existing resources and existing gcloud identity. Replace these illustrative values; do not create credentials just to run the CLI:

```sh
export L4CTL_PROJECT=your-project-id
export L4CTL_ZONE=asia-southeast1-c
export L4CTL_VM=your-existing-gpu-vm
python3 -m l4ctl status
python3 -m l4ctl init
```

Enrollment writes local state only, requiring the real numeric IDs printed by the init preview:

```sh
python3 -m l4ctl --execute init --vm-id ACTUAL_VM_ID --boot-id ACTUAL_BOOT_DISK_ID
```

Previews are the default and do not mutate cloud resources:

```sh
python3 -m l4ctl up
python3 -m l4ctl stop --after keep
python3 -m l4ctl park
python3 -m l4ctl storage
python3 -m l4ctl work-disk create large-job --size 200
python3 -m l4ctl recover
```

After enrollment and review, these execute cloud changes:

```sh
python3 -m l4ctl --execute up
python3 -m l4ctl --execute stop --after keep
python3 -m l4ctl --execute park
python3 -m l4ctl --execute work-disk create large-job --size 200
python3 -m l4ctl --execute work-disk attach large-job
python3 -m l4ctl --execute recover
```

`stop --after keep` retains every disk. `park` stops, snapshots, verifies the exact source and then requires a typed destructive confirmation before detach/delete. Enter/cancel keeps source data. New work disks are blank; attachment changes require the VM stopped, and formatting/mounting remains deliberate guest administration. `up` restores a parked boot disk before starting it. `recover` reconciles an uncertain operation without resending it, then may execute unsent remaining steps; inspect the journal first. Installation and Linux guest templates are separate steps described in the README.
