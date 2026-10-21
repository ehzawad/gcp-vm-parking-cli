# Engineering guidance reviewed

Reviewed 2026-10-21. No AGENTS.md or repository-specific engineering instructions were present in this workspace or its ancestor directories. Existing installed scripts were inspected read-only for compatibility; credentials and SSH key contents were not inspected.

Primary references informed the implementation:

- [Google: detach/reattach boot disks](https://docs.cloud.google.com/compute/docs/disks/detach-reattach-boot-disk): do so on stopped VMs; preserve the VM object.
- [Google: restore a snapshot](https://docs.cloud.google.com/compute/docs/disks/restore-snapshot): allocate a disk at least the source size, then attach it. Bigger disks need guest filesystem growth.
- [Google: snapshot resource](https://docs.cloud.google.com/compute/docs/reference/rest/v1/snapshots): pin numeric `id`, verify `sourceDiskId` and exact source URI; reported snapshot allocation can change.
- [Google: disk resource](https://docs.cloud.google.com/compute/docs/reference/rest/v1/disks): verify numeric IDs, `sourceSnapshotId`, disk status/users and reattachment timestamps.
- [Google: snapshot best practices](https://docs.cloud.google.com/compute/docs/disks/snapshot-best-practices): quiesce writes; operation DONE and snapshot READY/FAILED are distinct checks; snapshots have rate limits. The CLI takes stopped snapshots and does not retry rate-limit errors.
- [Google: set boot auto-delete](https://docs.cloud.google.com/sdk/gcloud/reference/compute/instances/set-disk-auto-delete): deleting the current VM would also delete its auto-delete boot disk. VM deletion is deliberately absent.
- [Docker daemon storage](https://docs.docker.com/engine/daemon/), [systemd unit dependencies](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html), and [Hugging Face environment variables](https://huggingface.co/docs/huggingface_hub/en/package_reference/environment_variables): guard mount identity and service lifecycle; separate model caches from token/configuration paths.

Engineering choices: pure plan construction, explicit execution, dependency-free runtime, one target lock, atomic durable checkpoints, asynchronous operation polling, postcondition verification, UUID request IDs, no automatic mutation retries, read-only uncertain-outcome reconciliation, retained backups, and tests that prohibit real transport access. Snapshot existence alone is not treated as a tested recovery procedure.
