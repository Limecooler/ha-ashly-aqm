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
| `await client.connect()` | REST-login, open WebSocket, start reconnect loop |
| `await client.disconnect()` | Tear down |
| `client.on_event(name, fn)` | Listen for events with a specific inner `name` |
| `client.on_topic(topic, fn)` | Listen for every event on a topic |
| `client.on_any(fn)` | Catch-all (heartbeats included) |
| `await client.join(topic)` | Subscribe to an additional topic at runtime |
| `await client.leave(topic)` | Unsubscribe from a topic |
| `client.set_session_id(...)` | Pin own session ID for echo filtering |

Every `on_*` returns an `Unsubscribe` callable; invoke it to remove the
listener. Handlers may be sync or async.

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

The device's Socket.IO server emits state-change events ONLY to clients
that carry the `ashly-sid` cookie returned by
`POST /v1.0-beta/session/login` in their WebSocket handshake.
Unauthenticated clients receive only public broadcasts (Channel Meters,
System Info Values, etc.) — every `Modify *`, `Set *`, `Change *`, and
similar state event is silently dropped.

`AquaControlClient` handles this automatically: it performs a REST login
on connect, captures the cookie, and includes it in the WebSocket
handshake.

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

An additional 8 tests under `tests/test_live.py` connect to a real AQM
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

| Test | Verifies |
|---|---|
| `test_connect_succeeds` | REST login + WS upgrade work end-to-end |
| `test_authenticated_topics_emit_events` | Triggering a REST mutation produces the corresponding push event — confirms cookie-gated state events are being received |
| `test_ambient_events_arrive` | `System Info Values` heartbeats land within 3 s — confirms the WS pipe is open |
| `test_echo_filter_skips_own_changes` | `set_session_id` + `is_from_session` filter own-echoes |
| `test_on_topic_receives_only_matching_topic` | Topic-filtered listener fires only for that topic |
| `test_unsubscribe_stops_dispatch` | `remove()` callback prevents further dispatch |
| `test_fetch_session_cookies_against_live_device` | Auth helper obtains the `ashly-sid` cookie |
| `test_fetch_session_cookies_wrong_password_raises` | Wrong password → `AquaControlAuthError` |

## License

MIT.
