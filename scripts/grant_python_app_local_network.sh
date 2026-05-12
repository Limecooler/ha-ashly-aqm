#!/bin/bash
# Add NSLocalNetworkUsageDescription to Homebrew Python.app's Info.plist,
# then re-sign the bundle. macOS Tahoe needs an .app bundle (not a loose
# CLI binary) for the Local Network privacy prompt to fire — kernel logs
# show our bin/python3.12 ran with `bundle_id: (null)`, so the embedded
# Info.plist trick on the CLI binary alone is insufficient.
#
# After running, point your venv at Python.app's binary instead of
# bin/python3.12 (the script prints the symlink command at the end).
#
# Reversible:
#     /Users/bill/personal-code/ha-ashly/scripts/revert_python_app_local_network.sh
# Or: brew reinstall python@3.12

set -euo pipefail

PYTHON_DIR=/opt/homebrew/Cellar/python@3.12/3.12.13_2/Frameworks/Python.framework/Versions/3.12
APP="$PYTHON_DIR/Resources/Python.app"
INFO_PLIST="$APP/Contents/Info.plist"

# Keep the backup OUTSIDE the .app bundle — codesign would otherwise try
# to seal it as a bundle resource and complain that it's unsigned.
BACKUP_DIR=/tmp/python312-backup
mkdir -p "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/Python.app-Info.plist.pre-lnp"

if [[ ! -d "$APP" ]]; then
    echo "ERROR: $APP not found. Check the Homebrew Python version path." >&2
    exit 1
fi

# Clean up any old in-bundle backup that codesign would refuse.
rm -f "$INFO_PLIST.pre-lnp"

if [[ -f "$BACKUP" ]]; then
    echo "(backup at $BACKUP already exists — leaving alone)"
else
    cp "$INFO_PLIST" "$BACKUP"
    echo "backup: $BACKUP"
fi

DESC="Python needs Local Network access to talk to development devices on the LAN (e.g. pytest live-integration suites against locally-hosted services)."

if /usr/bin/plutil -replace NSLocalNetworkUsageDescription -string "$DESC" "$INFO_PLIST" 2>/dev/null; then
    echo "replaced existing NSLocalNetworkUsageDescription"
else
    /usr/bin/plutil -insert NSLocalNetworkUsageDescription -string "$DESC" "$INFO_PLIST"
    echo "inserted NSLocalNetworkUsageDescription"
fi

echo
echo "re-signing Python.app (ad-hoc, deep, with timestamp):"
/usr/bin/codesign --force --deep --sign - "$APP"

echo
echo "final signature:"
/usr/bin/codesign -dv "$APP/Contents/MacOS/Python" 2>&1 | grep -E "Identifier|Signature|flags"

echo
echo "Test connection (should NOW pop a macOS prompt — click Allow):"
echo "  $APP/Contents/MacOS/Python -c \"import socket; socket.create_connection(('192.168.18.114', 8000), timeout=3); print('CONNECT OK')\""
echo
echo "Once you've clicked Allow, repoint the venv:"
echo "  ln -sf $APP/Contents/MacOS/Python /Users/bill/personal-code/ha-ashly/.venv/bin/python3.12"
