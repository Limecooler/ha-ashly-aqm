# Ashly Audio Integration for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)

Home Assistant custom integration for **Ashly AquaControl Zone Series**
mixers — primarily the **AQM1208** (12 in × 8 out) but the API surface is
identical across the AQM family (AQM408, etc.) and the integration is
designed to scale to multiple devices on the same LAN.

Communicates with the device's **AquaControl REST API** (cookie-authenticated,
port 8000) for control and configuration, plus the **socket.io 4.x stream**
on port 8001 for live signal-level meters.

---

## Features

### Control & state
- **Power** (front-panel power switch)
- **Front-panel LEDs** (config toggle)
- **Per-channel chain mutes** — 12 inputs + 8 outputs
- **Output → mixer assignment** — pick which mixer feeds each of the 8 outputs
- **DCA group level + mute** — 12 virtual DCAs
- **Mixer crosspoint level + mute** — full 8 × 12 matrix (disabled by default)
- **Phantom power** per mic input
- **Mic preamp gain** per mic input (0–66 dB, 6 dB steps)
- **GPO outputs** — drive the 2 rear-panel logic-output pins

### Live signal metering (via websocket)
- **24 signal-level sensors** per device — 12 rear-panel input meters
  (post-preamp) + 12 mixer-input meters (post-DSP)
- Streamed over the AquaControl socket.io endpoint on port 8001 at ~6 Hz,
  throttled to 1 Hz updates
- Disabled by default; enable per-channel from the entity settings

### Diagnostics
- **Firmware version** sensor
- **Preset count** sensor (with full list as attribute)
- **Last recalled preset** sensor
- **Identify** button (blinks the device's COM LED)
- HA **diagnostics download** with credentials / MAC / host redacted

### Lifecycle
- **DHCP auto-discovery** via the Ashly MAC OUI prefix (00:14:AA)
- **Multi-device support** — each AQM gets its own config entry
- **Reauth** flow when credentials fail
- **Reconfigure** flow for IP / port / credential changes
- **Options flow** — configurable polling interval (10–300 s, default 30 s)

---

## Supported Devices

| Model    | Inputs       | Outputs     | Verified |
|----------|--------------|-------------|----------|
| AQM1208  | 12 mic/line  | 8 balanced  | ✅ firmware 1.1.8 |
| AQM408   | 4 mic/line   | 8 balanced  | ⚠️ same API surface; not verified live |

If you have an AQM408 or another AquaControl Portal 2.0 device, please open
an issue with a diagnostic dump — the integration should work as-is but the
entity counts will need to be made model-aware.

---

## Installation

### HACS (recommended)

1. Open HACS in your Home Assistant instance.
2. Menu → **Custom repositories** → add this repo's URL with category
   **Integration**.
3. Search for "Ashly Audio" and install.
4. **Restart Home Assistant.**
5. **Settings → Devices & Services → Add Integration** → "Ashly Audio".

### Manual

1. Copy `custom_components/ashly/` into your HA `config/custom_components/`
   directory.
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration** → "Ashly Audio".

### Removing the integration

This integration follows the standard Home Assistant integration removal
process. No additional steps are required.

1. **Settings → Devices & Services → Ashly Audio** → ⋮ → **Delete**.
   Home Assistant unloads the integration, closes the device session and
   the live-meter socket, and removes the device and all its entities
   from the registry. No state remains on disk.
2. (Optional, manual install only) Delete `custom_components/ashly/` from
   your HA config directory and restart.
3. (Optional, HACS install) HACS → **Ashly Audio** → ⋮ → **Remove**.

The Ashly device itself is unaffected — its on-device presets, mixer
state, and authentication credentials remain untouched.

---

## Use cases

Concrete things people actually do with this integration:

- **Zone paging from automations** — Trigger an Apple Music / Sonos /
  TTS announcement, then use a `switch.turn_off` on the relevant chain
  mute switches to unmute the corresponding zones for the duration of
  the announcement, then re-mute.
- **Time-of-day preset recall** — Use the `ashly.recall_preset` service
  on an HA scheduler (e.g. `time_pattern` trigger or Schedule helper) to
  switch the venue from "Lunch", "Happy Hour", "Evening", "Late Night"
  presets without anyone touching the rack.
- **Meeting-room "default state"** — A single tap on a Lutron / Hue /
  Z-Wave keypad recalls a preset that mutes mics, drops zone levels,
  and reassigns mixer routing — all via one service call.
- **Phantom-power safety interlock** — Wire a `switch.turn_off` on the
  `phantom_power_*` switches into an automation that fires when any
  channel mute changes to `off` outside of business hours, to avoid the
  classic "pop" when a mic is hot-plugged.
- **Live-meter dashboard** — Enable a handful of the disabled-by-default
  meter sensors and put them on a Lovelace gauge / history graph for an
  at-a-glance health view of the room's audio chain.
- **Preset-recall notification** — Use the **Last recalled preset**
  sensor as a trigger to push a notification ("the system was switched
  to 'Sound Check' at 14:32") to the AV operator's phone.

---

## How data is refreshed

The integration uses two complementary update paths:

1. **REST polling (most state).** A `DataUpdateCoordinator` polls the
   device's REST API on the configured interval (default 30 s; 10–300 s
   via the integration options). One poll fetches the front panel,
   chain state, DCA state, crosspoints, presets, phantom-power state,
   mic-preamp gains, GPO state, and last-recalled preset — concurrently.
   Entities go `unavailable` when the poll fails and recover on the next
   successful poll, with a single log line per transition.

2. **Live socket.io stream (meters only).** The 24 channel-meter
   sensors are pushed from the device over a long-lived socket.io 4.x
   connection on a separate port. Updates fire at roughly 1 Hz when a
   channel has signal; the sensors are disabled by default to keep the
   recorder DB sane.

Optimistic updates: when you toggle a mute or change a level via HA,
the integration applies the change locally first and then writes to the
device, so the UI feels instant. The next poll reconciles state in case
the write didn't actually land.

---

## Known limitations

- **Supported models.** Only the AQM1208 (12-in / 8-out) and AQM408
  (8-in / 8-out) are tested. The integration may work on other AQM-
  family devices that expose the same AquaControl REST API but the
  default entity counts assume 12 inputs / 8 outputs / 8 mixers — open
  an issue with a diagnostic dump if you have a different model.
- **Reachable port.** The device must be reachable on the configured
  port (default 8000) for both the REST poll and config flow, and on
  port 8001 for the meter socket.io stream. There's no UDP discovery
  beyond DHCP MAC-prefix sniffing (`00:14:AA:*`).
- **No firmware update from HA.** This integration does not push
  firmware to the device. Updates are done from Ashly's standalone
  AquaControl Portal.
- **Crosspoint mutes/levels are disabled by default.** 96 of each per
  AQM1208 would flood the default UI; enable per-mixer or per-input as
  needed from the entity-settings page.
- **Channel meter sensors are disabled by default and not recorded in
  long-term statistics.** They're meant for live dashboards, not
  historical analysis (the value range is signal level, not acoustic
  SPL — `device_class` is intentionally unset).
- **macOS Tahoe (15) Local Network Privacy can silently drop traffic
  from Homebrew Python.** This affects developers running the test
  suite, not production HA installs. See the Troubleshooting section.
- **Cookie-auth sessions can expire** if the device reboots or its
  clock skews dramatically. The client transparently re-authenticates;
  if the configured password has actually changed on the device,
  HA's reauth flow kicks in and prompts for new credentials.

---

## Configuration

### Setup form

| Field    | Default  | Description                              |
|----------|----------|------------------------------------------|
| Host     | —        | IP or hostname of the AQM device         |
| Port     | 8000     | AquaControl REST API port                |
| Username | `admin`  | Device login username                    |
| Password | `secret` | Device login password (factory default)  |

Once you submit, the integration logs in, fetches the device's MAC for the
unique ID, and creates the config entry. Subsequent calls reuse the session
cookie returned by `/v1.0-beta/session/login`; expired cookies trigger
transparent re-authentication on the next request.

### Options

- **Polling interval** (10–300 seconds, default 30) — how often the
  coordinator polls REST state. Changing it triggers an automatic reload.

### Discovery

DHCP discovery fires for any device whose MAC starts with `00:14:AA`. HA
will surface a "discovered device" notification; click through to enter
credentials.

---

## Entities

Each AQM1208 creates **~277 entities** (≈80 enabled by default; the rest are
the noisy / install-time controls that are off by default but can be turned on
per-entity from the entity-settings UI):

### Switches (145 total — 49 enabled by default)

| Category                  | Count | Default      | Notes                                                  |
|---------------------------|-------|--------------|--------------------------------------------------------|
| Power                     | 1     | Enabled      | Front-panel power state                                |
| Chain mute (inputs)       | 12    | Enabled      | `is_on` = muted                                        |
| Chain mute (outputs)      | 8     | Enabled      | `is_on` = muted                                        |
| DCA mute                  | 12    | Enabled      | Per virtual DCA group                                  |
| Front-panel LEDs          | 1     | Enabled      | Config category                                        |
| Phantom power             | 12    | Enabled      | +48 V per mic input; config category                   |
| GPO output                | 2     | Enabled      | Drives rear-panel logic-output pins high/low           |
| Crosspoint mute           | 96    | **Disabled** | 8 mixers × 12 inputs; enable per-mixer as needed       |

### Numbers (120 total — 24 enabled by default)

| Category              | Count | Default      | Range                  |
|-----------------------|-------|--------------|------------------------|
| DCA level             | 12    | Enabled      | −50.1 to +12 dB        |
| Mic preamp gain       | 12    | Enabled      | 0 to +66 dB (6 dB steps; config category) |
| Crosspoint level      | 96    | **Disabled** | −50.1 to +12 dB        |

### Selects (8)
- **Output mixer assignment** — for each of the 8 outputs, pick a mixer
  (`Mixer.1`…`Mixer.8`) or `None`.

### Buttons (1)
- **Identify** — blinks the device's COM LED for 10 s (diagnostic).

### Services (1)
- **`ashly.recall_preset`** — load a stored preset by name or by 1-based
  numeric index. Targets a device by `device_id`. The preset list is
  exposed as an attribute on the **Preset count** sensor for templating.
  Example service call (YAML):

  ```yaml
  action: ashly.recall_preset
  target:
    device_id: 9a2c…
  data:
    preset: "Evening Mode"
  ```

### Sensors (27 total — 1 enabled by default)
- **Last recalled preset** (state = preset name or unknown; attribute `modified`)
- **Firmware version** — disabled, diagnostic
- **Preset count** — disabled, diagnostic; attribute `presets` lists all
- **Input N signal level** × 12 — disabled, diagnostic, dB; pushed at 1 Hz
- **Mixer input N signal level** × 12 — disabled, diagnostic, dB; pushed at 1 Hz

---

## Architecture

```mermaid
flowchart TD
    Device["AQM1208 device on the LAN"]
    Device -->|"port 8000<br>REST, cookie auth"| Client["AshlyClient<br>(aiohttp)"]
    Device -->|"port 8001<br>socket.io"| MeterClient["AshlyMeterClient<br>(python-socketio)"]
    Device -.->|"port 80, web UI<br>(not used)"| Browser["AquaControl Portal<br>(browser only)"]

    Client --> Coordinator["AshlyCoordinator<br>DataUpdateCoordinator<br>30 s poll: power, chains, dvca,<br>crosspoints, presets, phantom,<br>mic preamp, gpo, last-recalled"]
    MeterClient -->|"1 Hz publish"| MeterSensors["24 ChannelMeterSensor entities"]

    Coordinator --> Switch[switch]
    Coordinator --> Number[number]
    Coordinator --> Select[select]
    Coordinator --> Button[button]
    Coordinator --> Sensor[sensor]
```

**Cookie auth (`port 8000`)** — primary control path. The integration logs in
to `/v1.0-beta/session/login`, stores the `ashly-sid` cookie in a dedicated
cookie jar, and reuses it for all REST calls. Auto-reauth on HTTP 401 (and
HTTP 400 from credentials that fail the device's alphanumeric schema).

**Socket.io (`port 8001`)** — live meter stream. After login we open a
websocket, emit `join "Channel Meters"` and `startMeters`, and listen for
the device's flat-integer meter array. Capped exponential backoff (1 s →
30 s) with anti-flap dwell on reconnect. The cookie jar is shared with the
REST client, so a REST-side re-auth transparently refreshes the next
websocket reconnect's credentials.

**Coordinator** — single `DataUpdateCoordinator` per device. Bulk-polls all
state in parallel with `asyncio.gather`; auth errors only escalate to HA's
reauth flow if no concurrent connection errors are present (so a rebooting
device doesn't trigger spurious credential prompts). Optional endpoints
(presets, last-recalled, phantom power, mic preamp, GPO) are best-effort —
a transient failure on one of them reuses the prior value rather than
tanking the whole poll.

---

## API compatibility

This integration targets the AquaControl Portal 2.0 REST API (`v1.0-beta`
path prefix) on AQM-family devices. Verified live against firmware **1.1.8**
on an AQM1208.

### What's used

| Endpoint                                                    | Purpose                              |
|-------------------------------------------------------------|--------------------------------------|
| `POST /v1.0-beta/session/login`                             | Authentication                       |
| `GET  /v1.0-beta/system/info`                               | Device identity                      |
| `GET  /v1.0-beta/system/frontPanel/info`                    | Power + LED enable state             |
| `POST /v1.0-beta/system/frontPanel/info`                    | Set power / LED enable               |
| `GET  /v1.0-beta/system/identify`                           | Identify (blink COM LED)             |
| `GET  /v1.0-beta/network`                                   | MAC address (unique ID)              |
| `GET  /v1.0-beta/phantomPower`                              | Per-input phantom-power state        |
| `POST /v1.0-beta/phantomPower/{id}`                         | Set phantom power                    |
| `GET  /v1.0-beta/micPreamp`                                 | Per-input mic preamp gain            |
| `POST /v1.0-beta/micPreamp/{id}`                            | Set mic preamp gain                  |
| `GET  /v1.0-beta/workingsettings/dsp/channel`               | Channel topology                     |
| `GET  /v1.0-beta/workingsettings/dsp/chain`                 | Chain mutes + output→mixer mapping   |
| `POST /v1.0-beta/workingsettings/dsp/chain/mute/{id}`       | Set channel mute                     |
| `POST /v1.0-beta/workingsettings/dsp/chain/mixer/{id}`      | Assign mixer to output               |
| `GET  /v1.0-beta/workingsettings/dsp/mixer/config/parameter`| Crosspoint levels/mutes              |
| `POST /v1.0-beta/workingsettings/dsp/mixer/config/parameter/{id}` | Set crosspoint level/mute      |
| `GET  /v1.0-beta/workingsettings/virtualDVCA/parameters`    | DCA levels/mutes/names               |
| `POST /v1.0-beta/workingsettings/virtualDVCA/parameters/{id}` | Set DCA level/mute                 |
| `GET  /v1.0-beta/workingsettings/generalPurposeOutputConfiguration` | GPO pin state                |
| `POST /v1.0-beta/workingsettings/generalPurposeOutputConfiguration/{id}` | Set GPO pin              |
| `GET  /v1.0-beta/preset`                                    | List of stored presets               |
| `GET  /v1.0-beta/preset/lastRecalled`                       | Last recalled preset name + dirty    |
| `POST /v1.0-beta/preset/recall/{name}`                      | Recall preset by name (via `ashly.recall_preset` service) |
| `GET  /v1.0-beta/workingsettings/dsp/channel/metermap`      | Meter-index → channel map (one-shot) |
| **Socket.IO**: `join "Channel Meters"` + `startMeters` on `:8001` | Live meter stream         |

### What's intentionally not implemented

- **Preset *save / overwrite / delete*** — only **recall** is exposed (via
  the `ashly.recall_preset` service). Saving and deleting presets is a
  configuration-time concern handled by the AquaControl Portal web UI; the
  underlying endpoints exist (`POST /preset/full`, `POST /preset/update/{id}`,
  `DELETE /preset/{id}`) so adding HA services for them is a small follow-up
  if desired.

- **Per-block gain controls** — Simple Control exposes a
  `/workingsettings/dsp/block/gain/level` endpoint that returns / sets the
  level of *Gain DSP blocks* inserted into the signal chain. Newly-shipped
  devices have no Gain blocks configured, so the endpoint returns an empty
  list. Users who insert Gain blocks via AquaControl Portal can use the web
  UI to drive them; the equivalent HA control is the mic preamp gain
  (input stage) plus the DCA / crosspoint level controls (post-DSP).

- **Per-block meters** — the socket.io `"Block Meters"` room is documented
  but, like Gain blocks, only fires when DSP blocks are configured. We
  subscribe only to `"Channel Meters"`.

- **Scheduled / triggered events**, **remote management**, **user/security
  management**, **firmware upload**, **config import/export** — all
  out-of-scope for HA control; manage these via AquaControl Portal directly.

---

## Development

### Prerequisites

- Python 3.12+
- An AQM device on the same LAN as your test machine (for live tests)

### Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install pytest-homeassistant-custom-component aioresponses pypdf
```

### Run the unit-test suite

```bash
pytest                              # 190 tests, no network access
pytest -m integration               # live tests, skipped without ASHLY_HOST
```

### Run the live-integration suite against a real device

```bash
ASHLY_HOST=192.168.1.114 pytest -m integration tests/integration/
```

Optional env vars: `ASHLY_PORT` (default 8000), `ASHLY_USERNAME` (default
`admin`), `ASHLY_PASSWORD` (default `secret`).

The live suite (31 tests) round-trips device state safely — every test that
mutates state restores the original value in a `try/finally`, and tests
target the *last* output channel / DCA / crosspoint to minimise impact on a
running install. Power and source-mixer assignment are read-only-tested.

### Lint

```bash
pip install ruff
ruff check custom_components/ tests/
```

### Troubleshooting: `[Errno 65] No route to host` from Homebrew Python on macOS Tahoe / Sequoia

On macOS 15 (Sequoia) and 26 (Tahoe), Python's outbound connections to
**devices on the same Wi-Fi subnet** are silently denied by the OS's
Local Network Privacy enforcement. `curl` (an Apple-signed binary)
reaches the device fine, but the same call from `.venv/bin/python` fails
instantly with `OSError: [Errno 65] No route to host` — no permission
prompt, no entry in *System Settings → Privacy & Security → Local
Network*, no diagnostic surfaced to the user.

The root cause is how macOS classifies the Homebrew Python binary
(ad-hoc signed, no Team Identifier). In our testing, no per-binary CLI
fix actually worked: `tccutil reset LocalNetwork` doesn't manage this
state (Apple confirms LNP isn't stored in the TCC database), and
neither embedding `NSLocalNetworkUsageDescription` into the Mach-O,
forcing a fresh `LC_UUID`, nor re-signing with a self-signed
code-signing certificate triggered the prompt in macOS 26.

Two workarounds that do work:

1. **Move the AQM device to a different subnet from your dev machine**
   — typically a separate VLAN or guest network. macOS only applies
   Local Network Privacy to traffic on directly-attached subnets, so
   once the device is reachable only via your router (no direct ARP),
   Python connects to it normally and the live tests run without any
   further setup.

2. **Install Python from a source other than Homebrew** — `python.org`'s
   official installer ships a Python build signed with the Python
   Software Foundation's Apple Developer ID, which macOS prompts for
   on first use and (when you click Allow) permanently allows. Recreate
   the venv pointing at that Python and live tests work directly. Other
   distributions whose binaries carry a real Team Identifier
   (Anaconda's signed builds, Astral `uv`'s downloaded Pythons in some
   configurations) should also work.

Tracking issue for native Homebrew support:
[Homebrew/brew#15054](https://github.com/Homebrew/brew/discussions/15054).

---

## References

Ashly Audio's official AquaControl Portal 2.0 documentation, which this
integration is built against. Local copies are included under `docs/` and
the originals are:

1. **AquaControl REST API documentation** — the live interactive Swagger UI
   reference, available at `http://<device-ip>:8000/documentation` on any
   AQM device.
   [Static PDF (Ashly, Dec 2024)](https://ashly.com/wp-content/uploads/2025/01/AquaControl_API_Documentation.pdf)
   · [Local copy](docs/AquaControl_API_Documentation.pdf)

2. **How to use the AquaControl REST API** — primer on the cookie-auth flow
   plus Python `requests` examples for GET / POST / DELETE.
   [Static PDF (Ashly, Jan 2025)](https://ashly.com/wp-content/uploads/2025/01/AquaControl_REST_API_usage.pdf)
   · [Local copy](docs/AquaControl_REST_API_usage.pdf)

3. **AquaControl Portal access via other devices or software** — describes
   the cookie-authed admin flow and the Simple Control alternative for
   cookie-incapable clients.
   [Static PDF (Ashly, Jan 2025)](https://ashly.com/wp-content/uploads/2025/01/AquaControl_Portal_access_via_other_devices_or_software.pdf)
   · [Local copy](docs/AquaControl_Portal_access_via_other_devices_or_software.pdf)

4. **AquaControl Simple Control Integration Guide** — documents the
   `/simplecontrol/*` endpoint family used by control-system clients
   (Crestron, AMX, Q-SYS, Control4) that can't easily handle cookies. This
   integration uses the equivalent cookie-authed endpoints throughout
   (verified via live device testing — including preset recall), so the
   `SimpleControl` user account this guide describes is **not** required.
   [Static PDF (Ashly, Sep 2025)](https://ashly.com/wp-content/uploads/2025/09/AquaControl_Simple_Control_Guide.pdf)
   · [Local copy](docs/AquaControl_Simple_Control_Guide.pdf)

5. **AQM1208 Operating Manual** — hardware reference; specs, panel layout,
   DSP feature list, AquaControl UI screenshots.
   [Static PDF (Ashly, Apr 2025)](https://ashly.com/wp-content/uploads/2025/04/AQM1208-manual-r3.pdf)
   · [Local copy](docs/AQM1208-manual-r3.pdf)

---

## License

This project is not affiliated with Ashly Audio, Inc. "Ashly" and
"AquaControl" are trademarks of Ashly Audio, Inc.
