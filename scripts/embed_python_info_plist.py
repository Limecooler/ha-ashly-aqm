"""Embed NSLocalNetworkUsageDescription into Homebrew Python's binary.

This is a one-time, reversible workaround for macOS Tahoe (26.x) and
Sequoia (15.x), which silently deny Local Network privacy access for any
binary that lacks an embedded `__TEXT,__info_plist` Mach-O section
containing `NSLocalNetworkUsageDescription`. Homebrew Python's
`bin/python3.12` is built without such a section, so it can never even
trigger the system permission prompt — it's just silently blocked from
LAN connections (manifests as `[Errno 65] No route to host` even when
the destination is reachable from `curl`).

Symptom of the problem (run before applying this fix):

    $ .venv/bin/python -c "import socket; \
        socket.create_connection(('192.168.x.y', 80), timeout=3)"
    OSError: [Errno 65] No route to host

After running this script, the next connection attempt will pop the
standard macOS prompt asking to allow Python to access Local Network;
click *Allow* once and pytest's live-integration suite works directly
against LAN devices.

Run with Apple's system Python so you're not modifying the binary that
you're currently using to run the script. Requires `lief`:

    /usr/bin/python3 -m pip install --user lief
    /usr/bin/python3 scripts/embed_python_info_plist.py

A backup copy of the original Mach-O is saved alongside the binary as
`python3.12.pre-embed`. To revert at any time:

    cp <python_dir>/bin/python3.12.pre-embed <python_dir>/bin/python3.12

Or simply re-install via `brew reinstall python@3.12`, which restores
the original ad-hoc-signed bottle.
"""

from __future__ import annotations

import glob
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import lief

INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>org.python.python</string>
    <key>CFBundleName</key>
    <string>Python</string>
    <key>CFBundleExecutable</key>
    <string>python3.12</string>
    <key>CFBundleVersion</key>
    <string>3.12</string>
    <key>CFBundleShortVersionString</key>
    <string>3.12</string>
    <key>NSLocalNetworkUsageDescription</key>
    <string>Python needs Local Network access to talk to development \
devices on the LAN (e.g. pytest live-integration suites against \
locally-hosted services).</string>
</dict>
</plist>
"""


def find_python_binary() -> Path:
    """Locate the Homebrew Python 3.12 binary we want to patch."""
    candidates = sorted(
        glob.glob(
            "/opt/homebrew/Cellar/python@3.12/*/Frameworks/Python.framework/"
            "Versions/3.12/bin/python3.12"
        )
    )
    if not candidates:
        raise SystemExit(
            "Could not find Homebrew python3.12 binary under /opt/homebrew/Cellar/python@3.12. "
            "Pass the path explicitly: pass the binary path as argv[1]."
        )
    if len(sys.argv) >= 2:
        return Path(sys.argv[1])
    return Path(candidates[-1])  # newest version if multiple


def main() -> None:
    binary_path = find_python_binary()
    backup_path = binary_path.with_suffix(binary_path.suffix + ".pre-embed")

    print(f"target binary: {binary_path}")
    print(f"backup:        {backup_path}")

    if not backup_path.exists():
        shutil.copy2(binary_path, backup_path)
        print(f"backup written: {backup_path}")
    else:
        print("(backup already present; not overwriting)")

    plist_bytes = INFO_PLIST.encode("utf-8")
    print(f"embedding Info.plist ({len(plist_bytes)} bytes)...")

    fat = lief.MachO.parse(str(binary_path))
    binary = fat.at(0)
    text_segment = next(
        (s for s in binary.segments if s.name == "__TEXT"), None
    )
    if text_segment is None:
        raise SystemExit("no __TEXT segment in binary")

    existing = next(
        (s for s in text_segment.sections if s.name == "__info_plist"), None
    )
    if existing is not None:
        print("  replacing existing __info_plist section")
        existing.content = list(plist_bytes)
    else:
        print("  adding new __info_plist section")
        new = lief.MachO.Section("__info_plist", list(plist_bytes))
        binary.add_section(text_segment, new)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tmp")
    tmp.close()
    binary.write(tmp.name)
    shutil.move(tmp.name, str(binary_path))
    print("modified binary written")

    print("re-signing (ad-hoc)...")
    subprocess.run(
        ["/usr/bin/codesign", "--force", "--sign", "-", str(binary_path)],
        check=True,
    )
    print("\nfinal signature:")
    subprocess.run(["/usr/bin/codesign", "-dv", str(binary_path)], check=False)
    print(
        "\nDone. On your next Python LAN connection, macOS should pop a "
        "Local Network permission prompt — click Allow.\n"
        "Test with:\n"
        "  .venv/bin/python -c \"import socket; "
        "socket.create_connection(('<lan-ip>', 80), timeout=3); print('ok')\""
    )


if __name__ == "__main__":
    main()
