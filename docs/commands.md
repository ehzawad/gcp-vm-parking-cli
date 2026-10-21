# Everyday commands

After configuration, `l4 init`, and installing the collision-safe launcher described in the README:

```sh
l4 status
l4 snapshots
l4 storage
l4 up
l4 stop
l4 park
l4 storage add large-job 200
l4 recover
```

Reads run directly. Changes show a plan and ask for `[y/N]` in a terminal; the default is cancel. Stop keeps every disk. Park requires an additional exact disk/ID deletion phrase after backup verification. Use `l4 --plan up`, `l4 --plan park`, or `l4 --plan storage add large-job 200` to preview explicitly. Without a terminal no cloud mutation executes, even with legacy `--execute`.

Existing snapshots can be verified and registered without changing cloud resources:

```sh
l4 snapshots import existing-backup
l4 --plan test-restore existing-backup --disk restore-check
```

Registration does not assert a tested restore. `up` starts the original disk while it is attached, and restores the recorded snapshot only after a completed CLI park. Restore tests allocate billable resources and must be separately approved.

From an uninstalled checkout use `python3 -m l4ctl` instead of `l4`, with global `--config /path/to/private/config.local.json` or explicit target flags. Offline commands need neither credentials nor configuration:

```sh
python3 -m l4ctl --help
python3 -m l4ctl --fixture examples/stopped.json status
python3 -m l4ctl --fixture examples/stopped.json park
```
