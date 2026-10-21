# Separate critical review: destructive safety and recovery

Reviewed after initial implementation on 2026-10-21. This was a second source/test review by the implementing agent, not an independent external reviewer or live-cloud validation.

Findings fixed and regression-tested:

1. **Confirmation-to-request stale preconditions.** An operator could start a VM while the deletion prompt waited. Re-run every guard after confirmation: stopped state, VM/disk IDs and URIs, usage, boot protection, snapshot provenance and backup freshness. Tests restart the VM during the prompt and confirm no detach/delete is issued.
2. **Backup freshness across external activity/recovery.** Source identity alone does not detect writes after backup. Save pre-request VM last-start/disk last-attach timestamps in the durable snapshot step, retain that baseline after a lost response, and refuse removal if it changed. Tests cover restart, reattachment and uncertain-snapshot reconciliation.
3. **Explicit operation failure vs unknown outcome.** A DONE operation with an error now closes as failed and stops the plan. Uncertain responses remain blocked and are never resent. Tests distinguish both outcomes.
4. **Recovery with unsent stale steps.** Add local `recover --cancel-pending`, allowed only when all steps are done or unsent. It refuses any submitting/uncertain request. Completed local effects replay before closure. Tests ensure no cloud mutation occurs.
5. **Disk-name reuse after a lost delete response.** A recreated disk under that name cannot be adopted as a successful deletion or deleted during recovery. Test verifies zero additional mutation calls.
6. **Wrong restore source.** A disk READY status does not suffice; require exact `sourceSnapshotId` and URI before pinning/attaching. Test supplies a wrong snapshot ID and verifies no attach follows.
7. **Docker containerd store exception.** Docker `data-root` does not cover the separate containerd image store. Guarded templates now require explicit legacy image-store mode; unsupported configurations fail rather than silently retain image caches on boot. Test covers the refusal.
8. **State data minimization.** Whitelist boot attachment fields in addition to VM configuration/disk projections so encryption-key-related/private fields cannot be persisted accidentally. Tests verify metadata, token-like content and attachment private fields are absent.

Other reviewed invariants: `stop --after keep` contains only stop; deletion always follows verified snapshot and exact identity confirmation; work/test commands cannot address boot by name, ID or attachment; no snapshot deletion/pruning exists; restored boot size is at least 70 GB and original source size; VM object/configuration is retained; operation names/URIs stay within the target project/zone; state is private, atomic and fsynced; fixtures reject execution; the standard test suite needs neither network nor credentials.

Remaining practical limits are explicit: no live API/IAM, quota, GPU availability, SSH, Linux mount/systemd, filesystem or application/boot restore validation was authorized or performed. External actors can bypass this local lock. The API path uses a disk name, so numeric-ID rechecks cannot atomically exclude an external name-reuse race between check and request. Maintain exclusive cloud administration during removal. Unsupported/manual drift is stopped for inspection; uncertain operations without provable results require provider operation/audit review, not forced journal deletion or automatic retries.

## UX verification review

The terminal workflow now requires a readable plan followed by explicit approval for every cloud-changing command, even if legacy `--execute` is present. Noninteractive invocation sends no mutation. Plan mode and fixture mode cannot prompt into execution. Exact disk deletion text remains a separate approval held only in memory. Stop's default plan retains all disks.

Existing standard snapshots can be registered only through read-only provider verification against enrolled source names, URIs, numeric IDs and size. Import records restore validation false. Source-removal eligibility additionally requires the source currently attached only to the stopped selected VM, with start/attachment timestamps preceding snapshot creation; stale/unknown cases cannot authorize removal. Import never overwrites a different numeric snapshot identity under a reused name. Registration is reverified after interactive approval.

A discovered UX gap was fixed: private live configuration must not make an offline fixture address the production target. Fixture targets are now derived from the fixture unless explicit flags select otherwise. A regression test proves this separation. Default snapshot creation now requests/validates regional storage in the selected VM's region rather than inheriting an unreviewed provider default.

Fresh-shell launcher tests, true pseudo-terminal cancellation/approval tests and a two-process lost-response recovery test complement the existing engine failure tests. The launcher installer refuses an existing command/path and changes no shell profiles or existing commands. Private deployment configuration and local IDs remain outside the Git repository.

A live preflight exposed a historical-freshness gap in imported snapshots: last-start time alone does not prove the VM had stopped before the backup. Imports now require an ordered start/stop/creation chain and preserve the stop-time baseline for removal checks. Missing stop time or a stop after backup cannot qualify the copy for removal. Two regression tests cover these cases.
