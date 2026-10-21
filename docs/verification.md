# Verification

The updated CLI was tested on macOS with Python 3.15; runtime requires Python 3.10+. The final suite passed **75 tests** with `python3 -m unittest discover -q`. Source compilation and `git diff --check` also pass.

Tests cover mocked up/stop/park/restore, exact resource identity, boot protection, regional snapshot verification, operation polling, lost replies, idempotent recovery, locking and missing mounts. UX tests cover explicit plan mode, real pseudo-terminal approval/cancellation in a subprocess, noninteractive refusal even with `--execute`, readable storage allocation, snapshot import/source/ID protection, unvalidated/stale backup handling, and failure recovery across separate subprocesses without resending the uncertain mutation.

The collision-safe installer was tested in temporary user-owned PATH directories and refuses existing commands. A fresh login shell on the actual Mac resolves the new launcher and successfully runs its help. The new launcher uses private target/configuration/state files outside the repository. Existing lifecycle/storage/SSH commands were not replaced.

Real read-only checks exercised the installed launcher's provider transport: VM/disk status, snapshot listing, storage inventory, snapshot registration using verified provider IDs, up/park previews, and a restore-test preview. The production VM remained stopped with its original boot disk attached, and the retained snapshot remained READY. Registration wrote only local state and marked restore validation false.

The earlier UX verification used read-only provider calls. A later explicitly authorized bounded live test created a restored disk and an isolated CPU helper VM. Disk/source-snapshot identity verification succeeded. The helper stopped before probe output was captured; serial output was unavailable after shutdown, so no restored-OS boot success is claimed. Temporary test resources were cleaned up, and production resources were retained. No new credentials, persistent grants, firewall changes or broad network access were introduced. GPU/application recovery remains untested. Mocked restoration and snapshot READY status do not establish live recoverability.

Machine paths, actual project/resource identities, runtime journals, private records, logs, credentials and state are excluded from the repository. Example resources and numeric IDs are synthetic.
