# `aquacontrol` — Python client for the Ashly AquaControl push API

Async Python library that connects to an Ashly AQM-series audio processor
(AQM1208, AQM408, etc.) over the device's undocumented Socket.IO push
channel and delivers parsed state-change events to your application.

Originally factored out of the
[ha-ashly-aqm](https://github.com/limecooler/ha-ashly-aqm) Home Assistant
integration so the same protocol layer can serve other automation
frameworks (Node-RED via PyExec, custom dashboards, monitoring scripts).
The protocol reference lives in
[`docs/WEBSOCKET-API.md`](../docs/WEBSOCKET-API.md) and the security model
in [`docs/SECURITY-API.md`](../docs/SECURITY-API.md) of that repo.

## Why this exists

The AQM's REST API on port 8000 is well-documented and easy to consume,
but a polling loop misses real-time UX. The device ALSO runs a
Socket.IO server on port 8001 that pushes ~50 distinct event types
across 10 topics in real time — power toggles, mute changes, mixer
parameter mutations, preset recalls, etc. That channel is not in the
official Ashly docs; this library is the reverse-engineered client for
it.

## Quick start

```python
import asyncio
from aquacontrol import AquaControlClient

async def main() -> None:
    async with AquaControlClient(
        host="192.168.1.100",
        username="haassistant",   # recommended: dedicated service account
        password="…",
    ) as client:
        client.on_event("Set Chain Mute", on_mute)
        client.on_event("Modify system info", on_front_panel)
        client.on_topic("Preset", on_preset_lifecycle)
        await asyncio.sleep(3600)  # listen for an hour

async def on_mute(event):
    record = event.records[0]
    print(f"{record['id']}: muted = {record['muted']}")

async def on_front_panel(event):
    record = event.records[0]
    if "powerState" in record:
        print(f"Power: {record['powerState']}")
    if "frontPanelLEDEnable" in record:
        print(f"Front-panel LEDs: {record['frontPanelLEDEnable']}")

async def on_preset_lifecycle(event):
    print(f"{event.topic}/{event.name} uniqueId={event.unique_id}")

asyncio.run(main())
```

## API surface

### `AquaControlClient`

| Method | Purpose |
|---|---|
| `await client.connect()` | REST-login, open WebSocket, start reconnect loop. **Re-authenticates on every reconnect** so long-running deployments survive device reboots / cookie expiry. |
| `await client.disconnect()` | Tear down |
| `client.on_event(name, fn)` | Listen for events with a specific inner `name` |
| `client.on_topic(topic, fn)` | Listen for every event on a topic |
| `client.on_any(fn)` | Catch-all (heartbeats included) |
| `@client.listen(name=...)` | Decorator form (handler kept until the client is destroyed) |
| `await client.join(topic)` | Subscribe to an additional topic at runtime |
| `await client.leave(topic)` | Unsubscribe from a topic |
| `client.set_session_id(...)` | Pin own session ID for echo filtering |
| `client.is_own_event(event)` | Convenience: did *this* client trigger that event? |

Every `on_*` returns an `Unsubscribe` callable; invoke it to remove the
listener. Handlers may be sync or async. Async handlers are bounded by
a 5-second timeout so a hung handler can't stall the dispatch chain.

Constructor accepts an optional `session: aiohttp.ClientSession` so the
caller (e.g. a Home Assistant integration) can share its own session
for connection-pool reuse, and an optional `logger` so library logs
appear under the caller's namespace.

Decorator usage example:

```python
@client.listen(name="Set Chain Mute")
async def handle_mute(event):
    if client.is_own_event(event):
        return  # already applied optimistically
    record = event.records[0]
    apply_mute(record["id"], record["muted"])
```

### `Event`

```python
@dataclass(frozen=True, slots=True)
class Event:
    topic: str            # outer Socket.IO event name (the topic)
    name: str             # inner event name from data["name"]
    operations: tuple[Operation, ...]
    unique_id: str | int | None
    raw: Mapping[str, Any]
```

Convenience accessors for single-operation events (the common case):

- `event.api` — REST path of the mutation, or `None` for multi-op events
- `event.records` — changed fields (single-op) or empty list (multi-op)
- `event.type` — `"modify"` / `"new"` / `"delete"` / `"update"`

Classification:

- `event.is_ambient` — timer-driven heartbeat (System Info, DateTime, network)
- `event.is_meter` — Channel Meters frame
- `event.is_state_change` — neither of the above
- `event.is_from_session(session_id)` — own-echo filter

For multi-operation events (Preset Recall, DSP block lifecycle, Update *
event-scheduler events), iterate `event.operations`:

```python
async def on_preset_recall(event):
    for op in event.operations:
        if op.api == "/workingsettings/dsp/mixer":
            for mixer in op.records:
                ...
```

### Topics

Importable string constants in `aquacontrol.topics` for every confirmed
topic. `ALL_TOPICS` is a tuple of all 10:

```python
from aquacontrol import (
    CHANNEL_METERS, SYSTEM, WORKING_SETTINGS, PRESET, EVENTS,
    MIC_PREAMP, PHANTOM_POWER, NETWORK, FIRMWARE, SECURITY,
    ALL_TOPICS,
)
```

By default `AquaControlClient` subscribes to every topic in
`ALL_TOPICS`; pass `topics=[…]` to constrain.

### Exceptions

```
AquaControlError
├── AquaControlConnectionError
│   └── AquaControlTimeoutError
├── AquaControlAuthError
└── AquaControlProtocolError
```

## Authentication

`AquaControlClient` performs a REST login on connect, captures the
`ashly-sid` cookie returned by `POST /v1.0-beta/session/login`, and
includes it in the WebSocket handshake.

**Auth is recommended, not strictly required for state events on
current firmware.** Empirically (AQM1208 firmware 1.1.8) the device
delivers state-change events to unauthenticated subscribers too.
Earlier reverse-engineering notes suggested auth was a hard gate; that
was either wrong or was changed by a firmware update. The library still
authenticates by default because:

1. It's required for some endpoints the library may grow to use (mutations,
   user CRUD).
2. Future firmware may tighten the gate again.
3. A logged-in client appears in the device's security audit log under
   its real account, which is what production deployments want.

**Recommendation:** create a dedicated service account on the device
(role: `Guest Admin`, with only the permissions you actually need) and
use those credentials here rather than `admin/secret`. The companion
`ha-ashly-aqm` integration's setup flow does this provisioning
automatically; for standalone scripts, see the device's `/security/users`
REST endpoints or use the helpers in the integration's `AshlyClient`.

## Echo filtering

Every event carries `unique_id`:

- `str` (UUID): the originating client's session ID
- `0`: system-emitted (no originating session)
- `None`: public broadcast

If your application performs both REST mutations and listens for push,
you'll receive your own changes back as echoes. Filter them with:

```python
client.set_session_id(my_uuid)

@client.on_event("Set Chain Mute")
async def handle(event):
    if event.is_from_session(client.session_id):
        return  # already applied optimistically
    apply_mute(...)
```

The session ID is the one carried in the `ashly-sid` cookie. The
companion integration's REST client surfaces it; standalone consumers
can extract it from the cookie set on the login response.

## Reconnection

`AquaControlClient.connect()` starts a background task that connects
once and stays connected. On any disconnect (network blip, device
reboot, etc.) the task backs off with exponential delay + ±30 % jitter
(1 s → 30 s) and reconnects. Multiple devices on the same LAN won't
synchronise their reconnects after a network outage.

Re-authentication is automatic — the same credentials are reused on
each connect. If the user changes the password on the device, the
client will log auth failures and retry forever; the caller is
responsible for surfacing reauth UX (in the HA integration's case, via
`ConfigEntryAuthFailed`).

## What this library does NOT do

- REST mutations — only reads enough to obtain the auth cookie. Use the
  device's `/v1.0-beta/*` API directly (or the
  [ha-ashly-aqm](https://github.com/limecooler/ha-ashly-aqm)
  `AshlyClient`) for state changes.
- Event-name → typed-dataclass mapping. Records are delivered as the
  raw dicts the device sends; consumers parse the structure they care
  about. The protocol reference enumerates every record shape.

## Development

```bash
pip install -e .[dev]
pytest                    # 79 offline tests, enforced 100 % coverage
ruff check src tests
mypy src
```

### Offline test suite

79 tests, runs in ~100 ms with no network. The Socket.IO and REST
surfaces are mocked. `pyproject.toml` enforces `--cov-fail-under=100`
so any new code path requires a test.

### Live-device test suite

An additional 21 tests under `tests/test_live.py` connect to a real AQM
device and verify the wire contract. They're gated behind the `live`
marker AND require `ASHLY_HOST` in the environment, so the default
`pytest` run skips them entirely.

```bash
ASHLY_HOST=192.168.1.100 \
ASHLY_USERNAME=haassistant \
ASHLY_PASSWORD=… \
pytest -m live --no-cov
```

Optional env vars: `ASHLY_USERNAME` (default `admin`), `ASHLY_PASSWORD`
(default `secret`), `ASHLY_PORT` (default `8000`). Tests restore any
state they touch — they're safe to run against a production device,
though the front-panel-LED toggle test does cause one brief LED flicker
on hardware that has the LED control wired.

What the live suite verifies:

**Core connectivity (8 tests)**

| Test | Verifies |
|---|---|
| `test_connect_succeeds` | REST login + WS upgrade work end-to-end |
| `test_authenticated_topics_emit_events` | A REST mutation produces the matching push event |
| `test_ambient_events_arrive` | `System Info Values` heartbeats land within 3 s |
| `test_echo_filter_skips_own_changes` | `set_session_id` + `is_from_session` filter own-echoes |
| `test_on_topic_receives_only_matching_topic` | Topic-filtered listener fires only for that topic |
| `test_unsubscribe_stops_dispatch` | `remove()` callback prevents further dispatch |
| `test_fetch_session_cookies_against_live_device` | Auth helper obtains the `ashly-sid` cookie |
| `test_fetch_session_cookies_wrong_password_raises` | Wrong password → `AquaControlAuthError` |

**Per-topic round-trips (4 tests)**

| Test | Verifies |
|---|---|
| `test_working_settings_chain_mute_round_trip` | `WorkingSettings / Set Chain Mute` arrives on REST chain-mute toggle |
| `test_mic_preamp_round_trip` | `MicPreamp / Change Mic Preamp Gain` arrives on REST mic-gain change |
| `test_phantom_power_round_trip` | `PhantomPower / Change Phantom Power` arrives on REST phantom toggle |
| `test_gpo_round_trip` | `WorkingSettings / Modify generalPurposeOutputConfiguration` arrives on REST GPO toggle |

**Protocol-level (4 tests)**

| Test | Verifies |
|---|---|
| `test_preset_recall_three_phase_protocol` | `Preset Recall Begin → Recall → End` arrive in order with correct `uniqueId` shapes |
| `test_channel_meters_stream_emits_at_high_rate` | Meter stream emits >2 Hz, `is_meter` classification correct |
| `test_event_api_and_records_shape_on_crosspoint_mute` | Real `Modify DSP Mixer Parameter Value` event has expected `api`/`type`/`records` |
| `test_unauthenticated_ws_still_connects` | Documents the current "auth is recommended, not strictly required" reality |

**Dispatch + runtime API (5 tests)**

| Test | Verifies |
|---|---|
| `test_on_event_dispatch_by_name` | `on_event(name, …)` fires only for that name |
| `test_on_any_dispatch_sees_multiple_topics` | `on_any` receives events across topics |
| `test_runtime_join_subscribes_after_connect` | `client.join(topic)` after connect starts delivering that topic |
| `test_multi_listener_fan_out_in_documented_order` | `any → topic → event_name` dispatch order |
| `test_clean_disconnect_stops_background_task` | `client.disconnect()` exits within 3 s |

## Changelog

### 0.2.0

Hardening release driven by an independent multi-agent review (security,
resilience, HA-compat, best-practices). All findings except the HA-side
push-poll integration (lives in the consumer, not here) are addressed.

**Resilience**
- **Auth refresh on reconnect.** Cookies are obtained via a callback
  invoked on every connect attempt, so long-running deployments survive
  device reboots and session expiry. Previously the cookie from initial
  login was used forever.
- Per-instance jitter RNG — multiple `AquaControlClient` instances on
  the same LAN no longer synchronise their reconnects after an outage.
- Async handlers run under a 5-second timeout. A hung listener no
  longer stalls dispatch for other handlers on the same event.

**Security**
- Cleartext-HTTP warning when connecting to a non-private host
  (loopback / RFC 1918 / IPv6 ULA are quiet).
- Stream-level connect failures log only the exception **type**, not
  its repr (which could embed handshake URLs / headers).
- `socketio.ConnectionError` is re-raised with `from None` to drop the
  exception chain (the cause object can carry per-request headers).
- New `Event.raw` docstring warns against unconditional logging /
  serialisation (DoS vector — `Preset Recall` payloads can be ~400 kB).

**HA-integration prep**
- `AquaControlClient` now accepts a `session: aiohttp.ClientSession`
  for connection-pool sharing with HA's REST client.
- `logger=` argument (already present) documented for HA's child-logger
  pattern.
- `AquaControlProtocolError(transient: bool)` lets consumers map to
  `ConfigEntryNotReady` vs. `ConfigEntryError` cleanly.

**Best practices**
- `StreamConnection` removed from public `__all__`. Still importable
  via `aquacontrol.stream` for advanced use.
- New `client.listen(name=..., topic=...)` decorator alongside the
  existing direct `on_*` methods.
- New `client.is_own_event(event)` convenience wrapping the
  `set_session_id` + `is_from_session` pattern.
- New `event.is_single_operation` property to gate the single-op
  convenience accessors.
- New `parse_event(..., strict=True)` mode raises on protocol drift —
  useful for dev / CI; default mode stays permissive.
- `python-socketio` pinned to `>=5.10,<6.0` (the private `_trigger_event`
  hook is 5.x-only). Runtime guard raises `AquaControlProtocolError` if
  the hook is missing.
- `aiohttp` pinned to `>=3.9,<4.0`.
- Auth-helper `CookieJar(unsafe=True)` rationale inline-documented.
- Removed dead `pass # see set_session_id` block in dispatch.

**Testing**
- 100 offline tests (was 79), 100 % coverage maintained.
- 39 live tests against AQM1208 firmware 1.1.8 all pass.

### 0.1.0

Initial public release.

## License

MIT.
