#!/usr/bin/env python3
"""Install only a new l4 launcher in an existing user-owned directory on PATH."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import sys


def install(bin_dir, config=None, python=None):
    bin_dir = Path(bin_dir).expanduser().absolute()
    if not bin_dir.is_dir() or bin_dir.stat().st_uid != os.getuid():
        raise RuntimeError("Choose an existing user-owned bin directory.")
    entries = {Path(p).expanduser().absolute() for p in os.environ.get("PATH", "").split(os.pathsep) if p}
    if bin_dir not in entries:
        raise RuntimeError("The bin directory must already be on PATH; no shell files are edited.")
    destination = bin_dir / "l4"
    if shutil.which("l4") or destination.exists() or destination.is_symlink():
        raise RuntimeError("An l4 command/path already exists; refusing to replace it.")
    entry = Path(__file__).resolve().parents[1] / "l4.py"
    interpreter = python or shutil.which("python3") or sys.executable
    command = [interpreter, str(entry)]
    if config:
        config = Path(config).expanduser().absolute()
        if not config.is_file():
            raise RuntimeError("Private config file must exist before installing the launcher.")
        command += ["--config", str(config)]
    content = "#!/bin/sh\nexec " + " ".join(shlex.quote(arg) for arg in command) + ' "$@"\n'
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    print(f"Installed {destination}; source checkout must remain at {entry.parent}.")
    return destination


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    p.add_argument("--config", type=Path)
    args = p.parse_args()
    try:
        install(args.bin_dir, args.config)
    except (RuntimeError, OSError) as error:
        sys.exit(str(error))
