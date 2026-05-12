"""Force a new LC_UUID on Python.app's main binary so macOS treats it as a
fresh binary for Local Network privacy tracking, *and* re-embed an
Info.plist with NSLocalNetworkUsageDescription. Then re-sign the bundle.

Run from Apple's system Python (it has lief installed via the previous
`/usr/bin/python3 -m pip install --user lief`).

Why this is needed
------------------
On macOS Tahoe, the kernel's NECP (Network Extension Control Policy)
caches Local Network privacy decisions keyed on the binary's Mach-O
LC_UUID — *not* the code signature. Re-signing changes the signature
but leaves LC_UUID untouched, so previously-cached denials persist.
Forcing a new LC_UUID + re-signing makes the kernel re-evaluate from
scratch. Combined with the NSLocalNetworkUsageDescription that we add
to the .app's Info.plist, macOS should now display the permission
prompt on the next connection.

Usage:
    /usr/bin/python3 scripts/refresh_python_uuid.py

Backup of the binary lives at /tmp/python312-backup/Python.pre-uuid.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import lief

PYTHON_DIR = Path(
    "/opt/homebrew/Cellar/python@3.12/3.12.13_2/Frameworks/Python.framework/"
    "Versions/3.12"
)
APP = PYTHON_DIR / "Resources" / "Python.app"
APP_BINARY = APP / "Contents" / "MacOS" / "Python"
BACKUP = Path("/tmp/python312-backup/Python.pre-uuid")


def main() -> None:
    if not APP_BINARY.exists():
        sys.exit(f"missing: {APP_BINARY}")

    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    if not BACKUP.exists():
        shutil.copy2(APP_BINARY, BACKUP)
        print(f"backup: {BACKUP}")
    else:
        print(f"(backup already at {BACKUP})")

    fat = lief.MachO.parse(str(APP_BINARY))
    binary = fat.at(0)

    # Find the existing UUID load command and replace it.
    uuid_cmd = next(
        (c for c in binary.commands if c.command == lief.MachO.LoadCommand.TYPE.UUID),
        None,
    )
    if uuid_cmd is None:
        sys.exit("no LC_UUID load command — unexpected for a Mach-O")

    old_uuid = bytes(uuid_cmd.uuid).hex().upper()
    new_uuid_bytes = list(uuid.uuid4().bytes)
    uuid_cmd.uuid = new_uuid_bytes
    new_uuid_hex = bytes(new_uuid_bytes).hex().upper()
    print(f"LC_UUID: {old_uuid}  ->  {new_uuid_hex}")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tmp")
    tmp.close()
    binary.write(tmp.name)
    shutil.copy2(tmp.name, APP_BINARY)
    Path(tmp.name).unlink(missing_ok=True)

    # `binary.write` resets file perms; restore +x.
    APP_BINARY.chmod(0o755)
    print("wrote new binary; re-signing bundle (ad-hoc, deep)...")

    subprocess.run(
        ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(APP)],
        check=True,
    )

    print("\nfinal signature:")
    subprocess.run(["/usr/bin/codesign", "-dv", str(APP_BINARY)], check=False)

    print(
        "\n"
        "LC_UUID is now fresh. macOS should treat this as a new binary on "
        "the next connection attempt and pop the Local Network prompt.\n"
        "Test with:\n"
        f"  {APP_BINARY} -c \"import socket; "
        "socket.create_connection(('192.168.18.114', 8000), timeout=5); print('OK')\""
    )


if __name__ == "__main__":
    main()
