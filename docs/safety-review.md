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
