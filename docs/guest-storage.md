# Optional /work storage: deliberate modes, no fallback

These are Linux guest setup instructions and templates. No guest setup or VM connection was performed. The current boot disk remains 70 GB and small workloads can deliberately use its free space.

`guest/storage_guard.py` supports two explicit modes:

```sh
# On Linux, after deliberate filesystem provisioning/mount and UUID discovery:
python3 storage_guard.py run --mode work --uuid ACTUAL_FILESYSTEM_UUID -- python3 job.py

# Explicit small workload mode; provision /var/lib/l4-small with suitable ownership first.
python3 storage_guard.py run --mode boot --allow-boot-storage -- python3 small_job.py
```

Work mode requires an exact writable `/work` mount, the expected filesystem UUID, ext4/xfs, a whole-filesystem mount, and a UUID different from `/`. It refuses a plain directory, wrong disk, symlink, bind/subdirectory mount, read-only filesystem and missing mount. Validation happens before any cache directory is created. The foreground launcher holds a directory descriptor and gives caches `/proc/PARENT_PID/fd/N/...` paths anchored to the verified filesystem. A lazy unmount cannot redirect those cache paths into the boot directory. Use foreground jobs; do not daemonize a child after the wrapper exits. This helper is Linux-only; its mount/proc behavior was mocked on the Mac.

Only named model/package/compilation caches move: `HF_HUB_CACHE`, `HF_ASSETS_CACHE`, `HF_DATASETS_CACHE`, `PIP_CACHE_DIR`, `UV_CACHE_DIR`, `TORCH_HOME`, `CUDA_CACHE_PATH`, `TRITON_CACHE_DIR`. The helper leaves `HOME`, `HF_HOME`, `HF_TOKEN_PATH`, `CLOUDSDK_CONFIG`, `DOCKER_CONFIG`, and XDG configuration paths alone. Do not move authentication tokens into disposable caches; inspect application-specific paths before adding more cache variables. A program can ignore these variables, so configure its own storage explicitly too. User files and the working directory are not silently migrated.

For Docker work mode, configure a **dedicated optional `work.mount` systemd unit** for the expected UUID, rather than a mandatory `/work` fstab entry that can block a restored boot. Example to review:

```ini
# /etc/systemd/system/work.mount (do not enable globally)
[Unit]
Description=Optional L4 work filesystem

[Mount]
What=/dev/disk/by-uuid/ACTUAL_FILESYSTEM_UUID
Where=/work
Type=ext4
Options=defaults
TimeoutSec=30
```

Set `/etc/l4-storage.json` to `{"mode":"work","uuid":"ACTUAL_FILESYSTEM_UUID"}`. Carefully merge `{"data-root":"/work/docker","features":{"containerd-snapshotter":false}}` into the existing `/etc/docker/daemon.json`, preserving unrelated configuration. Install the reviewed guard at `/usr/local/lib/l4ctl/storage_guard.py` and the [work-mode Docker drop-in](../guest/docker-work.conf.example). `Requires`, `After` and `BindsTo` tie Docker to the exact mount unit; `ExecStartPre` verifies UUID and data-root. Missing/mismatched storage prevents Docker startup instead of creating `/work/docker` on the boot filesystem. Do not launch a second dockerd outside this guarded service.

Docker's containerd image store can keep images in a separate containerd root even when `data-root` changes. The supplied guard therefore requires `containerd-snapshotter=false` explicitly. If the installed Docker cannot support that deliberate legacy storage mode, it must refuse to start until a separate guarded containerd layout is reviewed; do not bypass the check. Other independent containerd consumers require their own storage policy.

For deliberate small Docker workloads, stop Docker first, replace the work drop-in with [the boot-mode drop-in](../guest/docker-boot.conf.example), choose `{"mode":"boot","allow_boot_storage":true}` and explicit Docker `data-root=/var/lib/l4-small/docker` with `containerd-snapshotter=false`. Reload systemd after reviewing unit changes, validate daemon config, and only then start it. Switching modes changes which image/container store Docker sees; it does not copy or remove data. No automatic Docker/cache migration, formatting, unmount or cleanup is provided. Avoid mandatory optional-work dependencies during boot snapshotting, and remove stale work-disk entries from fstab before detaching as Google directs.

Keep `/work` and policy/configuration root-owned so an unprivileged process cannot replace the mountpoint or daemon settings. Give the intended user ownership of only its cache/job directories. Review actual daemon startup flags and configuration paths before installing the templates. These templates assume the default Linux system Docker service, not rootless Docker or Snap packaging.

References: [Docker data directories](https://docs.docker.com/engine/daemon/), [systemd BindsTo/After](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html#BindsTo=), [Hugging Face cache/token variables](https://huggingface.co/docs/huggingface_hub/en/package_reference/environment_variables), [Google Linux disk mount guidance](https://docs.cloud.google.com/compute/docs/disks/format-mount-disk-linux).
