#!/bin/bash
# Re-sign Homebrew Python.app with a self-signed code-signing certificate.
#
# macOS Tahoe's NECP silently drops Local Network traffic from ad-hoc-signed
# binaries (Signature=adhoc, TeamIdentifier=not set) without ever prompting.
# Apple Dev Forums confirm this is intentional: LNP prompts require a stable
# Team Identifier in the code signature.
#
# This script:
#   1. Generates a self-signed code-signing certificate (RSA-2048).
#   2. Imports it to your login keychain.
#   3. Trusts it for code signing (needs `sudo` once).
#   4. Re-signs Python.app with it, giving it a real Team-Identifier-like
#      identity that Tahoe will prompt for on first connect.
#
# The cert lives in /tmp/python-cs/ and is reversible:
#     security delete-certificate -c "Local Python Code Signing" login.keychain
# Or fully restore the original Python.app:
#     brew reinstall python@3.12

set -euo pipefail

CERT_NAME="Local Python Code Signing"
CERT_DIR=/tmp/python-cs
APP=/opt/homebrew/Cellar/python@3.12/3.12.13_2/Frameworks/Python.framework/Versions/3.12/Resources/Python.app

mkdir -p "$CERT_DIR"

# --- 1. Generate the cert if we haven't already ---------------------------

if [[ ! -f "$CERT_DIR/cert.pem" || ! -f "$CERT_DIR/key.pem" ]]; then
    echo "generating self-signed code-signing cert..."
    cat > "$CERT_DIR/openssl.cnf" <<'EOF'
[req]
distinguished_name = req
prompt = no
[req_distinguished_name]
CN = Local Python Code Signing
O  = Local Development
[v3_codesign]
basicConstraints = CA:false
keyUsage = digitalSignature
extendedKeyUsage = codeSigning
EOF
    openssl req -x509 -nodes -newkey rsa:2048 \
        -keyout "$CERT_DIR/key.pem" \
        -out "$CERT_DIR/cert.pem" \
        -days 3650 \
        -config "$CERT_DIR/openssl.cnf" \
        -extensions v3_codesign
    openssl pkcs12 -export \
        -out "$CERT_DIR/cert.p12" \
        -inkey "$CERT_DIR/key.pem" \
        -in "$CERT_DIR/cert.pem" \
        -name "$CERT_NAME" \
        -passout pass:lps-local
    echo "cert generated."
else
    echo "(cert already exists at $CERT_DIR/cert.p12)"
fi

# --- 2. Import to login keychain (idempotent) -----------------------------

if security find-certificate -c "$CERT_NAME" login.keychain >/dev/null 2>&1; then
    echo "(cert already in login.keychain)"
else
    echo "importing cert into login keychain..."
    security import "$CERT_DIR/cert.p12" \
        -k "$HOME/Library/Keychains/login.keychain-db" \
        -P lps-local \
        -T /usr/bin/codesign
fi

# --- 3. Trust the cert for code signing (one-time sudo step) --------------
# This is the only step that needs admin: adding our self-signed cert to
# the System trust store so codesign accepts it as a valid identity.

echo "trusting cert for code signing (requires sudo password)..."
sudo /usr/bin/security add-trusted-cert \
    -d -r trustRoot -p codeSign \
    -k /Library/Keychains/System.keychain \
    "$CERT_DIR/cert.pem"

# --- 4. Re-sign Python.app ------------------------------------------------

# Save a backup of the previous signature state by noting current identifier
echo
echo "current Python.app signature (before our re-sign):"
/usr/bin/codesign -dv "$APP/Contents/MacOS/Python" 2>&1 | grep -E "Identifier|Signature|TeamIdentifier"

echo
echo "re-signing Python.app with self-signed identity..."
/usr/bin/codesign --force --deep --sign "$CERT_NAME" "$APP"

echo
echo "new Python.app signature:"
/usr/bin/codesign -dv "$APP/Contents/MacOS/Python" 2>&1 | grep -E "Identifier|Signature|TeamIdentifier|Authority"

echo
echo "Test the connection — macOS should NOW pop the Local Network prompt:"
echo "  $APP/Contents/MacOS/Python -c \"import socket; socket.create_connection(('192.168.18.114', 8000), timeout=5); print('CONNECT OK')\""
echo
echo "After clicking Allow, repoint the venv:"
echo "  ln -sf $APP/Contents/MacOS/Python /Users/bill/personal-code/ha-ashly/.venv/bin/python3.12"
