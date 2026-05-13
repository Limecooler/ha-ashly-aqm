# Changelog

All notable changes to this integration are documented here. Versioning loosely
follows [Semantic Versioning](https://semver.org/) with the caveat that the
`<major>.<minor>.<patch>` field also drives HA's HACS update notifications.

## 0.6.1 — 2026-05-13

UX-focused release driven by an onboarding review. No breaking changes for
existing users, but every first-run path is now clearer.

### Security / setup UX

- **Factory password is no longer pre-filled** on the setup form. Users
  now have to type the device's password rather than one-clicking through
  with `secret`, which the `default_credentials` repair issue then scolded
  them for. The username default (`admin`) stays.
- **`cannot_connect` error names the host and port** and warns against
  pasting URLs into the host field or hitting the device's web-UI port
  (80) instead of the AquaControl API port (8000).
- **`data_description` strings** rewritten with real "where do I find
  this on the device?" guidance, not echoes of the field default.

### Discovery

- **Zeroconf / mDNS discovery added.** Static-IP installs (the norm on
  AV racks) often miss DHCP-based discovery. The manifest now matches
  `_http._tcp.local.` advertisements with `aqm*` or `ashly*` hostnames;
  the new `async_step_zeroconf` extracts the MAC from the hostname
  suffix or properties dict and behaves identically to the DHCP path.
- **Port override in the discovery confirm dialog** for deployments that
  remapped AquaControl off port 8000.

### Reauth / reconfigure

- Reauth dialog now shows the device name and `host:port` so users with
  multiple AQMs know which one is asking.
- Reconfigure description shows the current host/port and frames the
  common "IP changed / password changed" cases.

### Repair fix flow

- **`default_credentials` repair issue is now fixable in one click.**
  The Fix button opens a small form, validates new credentials against
  the device, writes them to the entry, and reloads. The issue
  auto-clears on the next successful poll.

### Options

- Poll-interval floor raised from 5 s to 10 s (a 9-endpoint gather every
  5 s is harsh on a small embedded CPU). Description recommends 30 s and
  notes that live meters update at 1 Hz regardless of this setting.

### Misc

- `no_mac` is now an abort, not a re-submittable error — the user can't
  fix this without firmware updates.
- `already_configured` copy explains how to add a second device.
- 100% coverage maintained; 21 new tests covering the zeroconf paths,
  port override, no_mac abort, and 5 repair-flow paths.

## 0.6.0 — 2026-05-13

Resilience, performance, and HA-platform polish. No breaking changes for end
users; entity names, IDs, and behavior are preserved.

### Resilience

- **Per-endpoint timeout softening.** A single critical endpoint timing out
  during the gather poll (e.g. dvca takes too long once while the others
  return instantly) now reuses the prior poll's value for that one endpoint
  instead of failing the whole poll. A new `AshlyTimeoutError` subclasses
  `AshlyConnectionError` so existing handlers still catch it.
- **Meter reconnect jitter.** Backoff between websocket reconnect attempts
  now applies ±30% jitter. N devices on the same LAN no longer pile their
  reconnects on the same tick after a network outage.
- **`device_unreachable` repair issue.** After ~10 minutes of consecutive
  poll failures, a Warning-severity issue appears in Settings → Repairs
  pointing the user at the reconfigure flow (in case the device's IP has
  drifted via DHCP). Auto-clears on first successful poll.
- **Graceful HA shutdown.** Registers `EVENT_HOMEASSISTANT_STOP` listener
  to stop the meter websocket before the event loop tears down, eliminating
  "Task was destroyed but it is pending" warnings during HA stop.
- **Repair issues re-evaluated each poll.** Changing default credentials
  on the device now clears the default-credentials repair on the next
  successful poll, not just at setup.

### Performance

- **Skip crosspoint poll when no entities enabled.** The 96-entry crosspoint
  matrix (~30 KB JSON) is the largest endpoint. When no crosspoint entity
  is enabled in the registry (default state), the poll skips that HTTP
  request entirely. Re-evaluates each poll so enabling an entity
  automatically resumes fetching.
- **Coalesce crosspoint optimistic updates.** Crosspoint mute/level changes
  within a 50 ms window collapse into one `async_set_updated_data` fan-out
  instead of N. Scenes that flip many crosspoints at once go from
  ~280 entities × N to ~280 × 1.

### HA platform polish

- **`RestoreEntity` on slow-changing sensors.** Firmware version, preset
  count, and last-recalled-preset sensors restore their last known value
  on HA restart instead of showing `unavailable` for ~30 s.
- **Dynamic preset buttons.** One `button.ashly_recall_<preset>` entity per
  preset on the device. Disabled by default; enable in the entity registry
  for the presets you wire to dashboards. New presets appear automatically;
  removed presets mark their button permanently unavailable.
- **Device triggers.** `preset_recalled` device trigger so automations can
  say "when the Ashly preset changes" directly in the UI instead of
  templating against the sensor entity.
- **Device actions.** `recall_preset` device action so users can pick it
  from the automation UI's "Then do…" picker.
- **Service response.** `ashly.recall_preset` now returns
  `{"recalled": [{"host": "...", "preset": "..."}]}` when called with
  `return_response=True`. Existing callers continue to work unchanged
  (response is `SupportsResponse.OPTIONAL`).
- **Multi-device verified.** New integration test boots two AQM entries
  concurrently and verifies the service registers once, deregisters only
  on the last entry removal, and each entry's repair issues are isolated.

### Diagnostics & docs

- **Expanded `diagnostics.py`.** Now includes coordinator health
  (last_update_success, consecutive_failures, unreachable_issue_raised,
  update_interval, pending crosspoint patches, last_exception), client auth
  epoch + authenticated flag, and meter connection state. All sensitive
  fields (password, host, MAC) still redacted.
- **README architecture diagram is now Mermaid** (renders inline in GitHub).
- **`SECURITY.md`** and **`CONTRIBUTING.md`** added at the repo root.
- **`strings.json`** added as the HA-core convention source-of-truth;
  CONTRIBUTING.md documents the dual-source maintenance.
- **`pyproject.toml`** has proper description, license, authors, keywords,
  and URLs.

### Internal

- 100% test coverage maintained. CI `--cov-fail-under=100` so any
  regression in coverage fails the build.
- New `tests/test_snapshots.py` locks in the per-platform entity-key set
  and disabled-by-default invariants — regressions in user-visible
  entity_ids surface as explicit test diffs.

## 0.5.1 — 2026-05-12

- 100% test coverage across all 14 source files (up from 95.13%).
- CI `--cov-fail-under` raised to 100.

## 0.5.0 — 2026-05-12

- Hit Platinum quality scale (every rule across Bronze/Silver/Gold/Platinum).
- Test coverage raised to 95.13% with ~70 new tests.
- Default-credentials repair issue.
- README sections: use cases, data refresh model, known limitations.

## 0.4.0 — 2026-05-12

- Mypy strict mode passing across all 14 source files.
- Entity translations + icon translations + exception translations.
- New `quality_scale.yaml` declaring per-rule status for all 47 rules.

## 0.3.1 — 2026-05-12

- In-repo brand icons under `custom_components/ashly/brand/` (HA 2026.3+
  Brands Proxy API).

## 0.3.0 — 2026-05-12

- Initial public release. Full feature set: zones, gain/mix, mutes,
  preset recall, live metering, DHCP discovery, reauth/reconfigure flows.
