# Security Policy

## Reporting a Vulnerability

If you believe you've found a security issue in this integration, please
**do not file a public GitHub issue**. Instead, report it privately to the
maintainer:

- Open a [GitHub Security Advisory](https://github.com/limecooler/ha-ashly-aqm/security/advisories/new) (preferred), or
- Contact the codeowner listed in `custom_components/ashly/manifest.json`.

Include enough detail to reproduce the issue:

- Home Assistant version
- Integration version (from `manifest.json`)
- Steps to trigger
- Impact (e.g. credential exposure, remote code execution, denial of service)

The maintainer will acknowledge receipt within 7 days and provide a
remediation plan within 30 days for confirmed issues.

## Scope

This integration talks to Ashly Audio AQM-series devices over the local
network using cookie-authenticated HTTP. In-scope issues include:

- Credential leakage in logs, diagnostics, or the entity registry
- Path-traversal or injection via crafted device responses
- Cross-config-entry data bleed (one device's state leaking into another)
- DoS amplification via the coordinator or meter websocket

Out of scope:

- Issues in the AquaControl firmware on the device itself — report those
  to Ashly Audio directly.
- Issues in Home Assistant core — report those to
  [home-assistant/core](https://github.com/home-assistant/core/security).

## Known security-relevant behaviors

- **Factory-default credentials trigger a repair issue.** The integration
  surfaces a Warning-severity issue in Settings → Repairs when the device
  is still using `admin` / `secret`. Users are directed to change the
  password on the device and reconfigure the integration.
- **Diagnostics redact sensitive fields.** `password`, `host`, and
  `mac_address` are removed from `async_get_config_entry_diagnostics`
  output via HA's standard redaction helper.
- **Cookie jar per entry.** Each config entry gets its own cookie jar so
  the device's session cookie cannot leak to other integrations sharing
  HA's default session.
