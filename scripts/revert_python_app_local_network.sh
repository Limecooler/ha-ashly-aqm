#!/bin/bash
# Revert grant_python_app_local_network.sh: restore Python.app's original
# Info.plist (from the /tmp backup) and re-sign the bundle.

set -euo pipefail

PYTHON_DIR=/opt/homebrew/Cellar/python@3.12/3.12.13_2/Frameworks/Python.framework/Versions/3.12
APP="$PYTHON_DIR/Resources/Python.app"
BACKUP=/tmp/python312-backup/Python.app-Info.plist.pre-lnp

if [[ ! -f "$BACKUP" ]]; then
    echo "ERROR: backup at $BACKUP missing. Use 'brew reinstall python@3.12' to restore." >&2
    exit 1
fi

cp "$BACKUP" "$APP/Contents/Info.plist"
echo "restored Info.plist from $BACKUP"

# Clean up any stale in-bundle backup that might still exist
rm -f "$APP/Contents/Info.plist.pre-lnp"

/usr/bin/codesign --force --deep --sign - "$APP"
echo "re-signed Python.app"
echo
echo "If you also repointed the venv earlier, restore it with:"
echo "  ln -sf /opt/homebrew/opt/python@3.12/bin/python3.12 /Users/bill/personal-code/ha-ashly/.venv/bin/python3.12"
