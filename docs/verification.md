# Verification

The sanitized repository was tested locally with Python 3.15 on macOS. The runtime requires Python 3.10 or later and has no third-party Python dependencies.

```sh
python3 -m unittest discover -v
python3 -m compileall -q l4ctl guest tests
python3 -m l4ctl --fixture examples/stopped.json status
python3 -m l4ctl --fixture examples/stopped.json park
```

The final local run passed **54 tests** (`python3 -m unittest discover -q`, exit 0). Compilation and offline park preview also exited 0.

The unit suite covers mocked lifecycle success/failure, exact resource identity, boot protection, deletion confirmation, uncertain-outcome reconciliation, crash recovery, locking, explicit storage modes and absent/wrong mounts. Fixture tests prohibit subprocess and HTTP access, proving offline previews do not touch credentials or cloud resources. See [safety review](safety-review.md).

No live Compute Engine operation, IAM/quota validation, Linux mount/systemd integration, application recovery or boot restore was performed. Snapshot READY and mocked restoration are not evidence of a tested live restore. The README describes a separate explicitly authorized test-restore procedure.

Machine-specific script hashes, local paths, runtime journals/state, private records, caches, credential files and original machine logs are excluded from this repository. The project, VM and disk values in fixtures are illustrative, and numeric IDs are synthetic.
