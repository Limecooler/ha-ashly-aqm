# AquaControl Socket.IO Push API — Reverse-Engineered Reference

The Ashly AquaControl device exposes an undocumented push channel over
Socket.IO on TCP port 8001. The AquaControl Portal uses it for instant UI
sync; the device's published REST API and PDF documentation do not mention
it. This file documents the channel as observed against an **AQM1208 running
firmware 1.1.8**.

Coverage is exhaustive for everything that is testable on this device.
Items that require hardware not present (Dante card, FIR sample, PEQ/FBS/GEQ
blocks, firmware-update upload) or that were intentionally skipped for
safety (factory reset, network config change, security/user CRUD) are
flagged in §6.

If you reverse-engineer further events, please update this document.

---

## Table of contents

1. Connection (URL, transport, authentication, subscription)
2. Universal event envelope
3. Topic catalogue (`Channel Meters`, `System`, `WorkingSettings`, `Preset`, `Events`, `MicPreamp`, `PhantomPower`, `Network`, `Firmware`)
4. Trigger ↔ event matrix (REST endpoint → emitted event)
5. Traffic profile
6. Gaps and untestable items
7. Gotchas
8. Source

---

## 1. Connection

| Property | Value |
|---|---|
| URL | `ws://<host>:8001/socket.io/?EIO=3&transport=websocket` |
| Engine.IO version | 3 |
| Transport | `websocket` (polling fallback works but slower) |
| Authentication | Cookie-based — the `ashly-sid` cookie returned by `POST :8000/v1.0-beta/session/login` must be carried in the WebSocket upgrade `Cookie:` header |
| Reconnection | None server-side; client must reconnect |

### 1.1 Authentication is the gating mechanism

The most important and least obvious thing about this API:
**the device emits a different set of events to authenticated vs.
unauthenticated socket.io clients.**

- **Unauthenticated** (no cookie): only `Channel Meters`, `System / System Info Values`, `System / DateTime`, `System / System Log`, `System / System Log Entry`, `Network / detected updated network parameters`.
- **Authenticated** (carrying the `ashly-sid` cookie in the WS handshake): the full state-change firehose — every `Modify *`, `Set *`, `Change *`, `Preset Recall *`, event-scheduler event, and so on.

This single fact explains every failed reverse-engineering attempt. If you
join the right topics but receive only `Channel Meters`, you almost
certainly aren't authenticated on the socket.

#### Minimal Python connect example

```python
import aiohttp, socketio

rest = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
async with rest.post(
    "http://<host>:8000/v1.0-beta/session/login",
    json={"username": "admin", "password": "secret", "keepLoggedIn": True},
) as r:
    pass  # ashly-sid now in jar

cookie_header = "; ".join(f"{c.key}={c.value}" for c in rest.cookie_jar)
sio = socketio.AsyncClient()
await sio.connect(
    "http://<host>:8001",
    transports=["websocket"],
    headers={"Cookie": cookie_header},
)
```

### 1.2 Subscriptions

The server does not emit to any topic by default. The client must
explicitly subscribe:

```
socket.emit("join", "<TopicName>")
```

`leave` is the converse. Joining an unknown topic silently succeeds (no
ack, no error). There is no wildcard.

Confirmed topics (case-sensitive):

| Topic | Subscribe-time auth | What it carries |
|---|---|---|
| `Channel Meters` | Optional | Audio meter stream, ~5 Hz |
| `System` | Required for mutation events | Front-panel state, system info, system time, identify, log entries, premute, sync, import |
| `WorkingSettings` | Required | DSP mutations: chain mute, mixer parameters, DVCA, GPO, channel names, DSP blocks, templates |
| `Preset` | Required | Preset CRUD + 3-phase recall protocol + dirty signal |
| `Events` | Required | Event scheduler CRUD + scheduler state notifications |
| `MicPreamp` | Required | Mic preamp gain changes |
| `PhantomPower` | Required | Phantom power changes |
| `Network` | Optional | Periodic link-state heartbeat (~7 s) |
| `Firmware` | Required | Firmware update progress (not exercised) |
| `Security` | Required | User/role/permission CRUD events |

Topics that **do not exist** on this device despite being plausible names:
`DSP`, `Chain`, `Mixer`, `Channel`, `Channels`, `Hardware`, `Audio`,
`Device`, `Status`, `Identify`, `GPO`, `GPIO`, `Inputs`, `Outputs`,
`Preamps`, `Phantom`, `IO`, `License`, `Trigger`, `Schedule`, `Triggered`,
`Scheduled`, `EventScheduler`, `Remote`, `Remotes`, `Security`, `User`,
`Users`, `Account`, `Accounts`, `Dante`, `FIR`. Subscribing to these is a
silent no-op.

The Portal joins only four — `Preset`, `Firmware`, `WorkingSettings`,
`System` — and therefore receives mic preamp / phantom power / event
scheduler updates as side effects of refetching when the user navigates,
not via push. A full integration should join all nine.

### 1.3 Wire format

Standard Socket.IO Engine.IO v3 protocol on top of WebSocket. All event
payloads shown here are the JSON object inside the
`42["<TopicName>", <payload>]` envelope. Heartbeat (`2`/`3`) and
connect/ack frames are standard Socket.IO and not documented here.

---

## 2. Universal Event Envelope

Every state event has the same outer shape regardless of topic:

```json
{
  "name": "<inner-event-name>",
  "data": [
    {
      "api": "<rest-path-or-update>",
      "records": [ /* changed field(s), full record, or nested ops */ ],
      "type": "modify" | "update" | "new" | "delete"
    }
  ],
  "uniqueId": "<originating-session-id>" | 0 | null
}
```

### 2.1 Field semantics

- **`name`** — the inner event name. Outer Socket.IO event name is the
  topic; inner `name` identifies the specific change. Multiple inner
  events live on each topic.

- **`data`** — typically a single-element list. Multi-element occurs on:
  - `Preset Recall` (each operation per affected area)
  - `Preset / Delete preset` (the deletion plus the `lastRecalled` flip)
  - `New DSP Block` / `Delete DSP Block` / `Copy Paste DSP Block To Chain` / `Cut Paste DSP Block To Chain` (the block record plus its parameters and subparameters as separate operations)
  - `WorkingSettings / Template Loaded` (delete-old + new-from-template ops)
  - `Events / Update *` events (delete-old + new-with-same-data ops because the underlying record is replaced, not mutated)

- **`data[*].api`** — REST endpoint path of the mutation OR the literal `"update"` for ambient broadcasts (e.g. `System Info Values`, `DateTime`). Useful as a stable routing key but not unique — multiple events share the same `api` (see §7 gotcha #2).

- **`data[*].records`** — list of changed fields, the full new record, or for `Preset Recall` a nested object containing the full affected sub-graph.

- **`data[*].type`** —
  - `"modify"` — most common, for in-place field changes
  - `"new"` — creation events
  - `"delete"` — deletion events
  - `"update"` — used by ambient broadcasts (`System Info Values`, `DateTime`, `Network / detected updated network parameters`)

- **`uniqueId`** —
  - `"<uuid>"` — the originating client's session ID. Use to filter your
    own echoes when applying optimistic updates.
  - `0` — system-emitted (no originating session, e.g. `DateTime`,
    `Preset Recall End`, `System Identify`, `Sync`, `Emergency Mute`,
    `All Scheduled Events Blocked`, `detected updated network parameters`).
  - `null` — public broadcast (e.g. `System Info Values`, `System Log`,
    `Channel Meters`, `Last Recalled Preset Modified`).

### 2.2 Echo filtering

The device echoes your own mutations back to you on the socket. To prevent
double-applying when using optimistic updates, drop events where
`uniqueId` matches your client's session ID. Capture your session ID
from the first event you originate (e.g. the `Modify system info` echo
after your first `POST /system/frontPanel/info`).

---

## 3. Topic Catalogue

### 3.1 `Channel Meters`

Audio meter stream. ~5 Hz cadence.

| Event name | Trigger | Payload |
|---|---|---|
| `Channel Meters` | Timer-driven | See below |

```json
{
  "name": "Channel Meters",
  "data": [{"api": "update", "records": [/* 96 ints */], "type": "modify"}],
  "uniqueId": null
}
```

The 96-element `records` array on the AQM1208 packs 12 inputs + 24 internal
taps + 64 mixer-related + 8 outputs. Values on this firmware are quantised
to 0/1 (signal-present indicators); other AQM firmwares may emit normalised
dB scalars.

### 3.2 `System`

Front-panel, system info/time, identify, log, import/sync.

#### 3.2.1 Ambient (system-emitted, `uniqueId: null` or `0`)

| Inner name | `api` | Trigger | Rate / cadence |
|---|---|---|---|
| `System Info Values` | `update` | Internal timer | 1 Hz |
| `DateTime` | `update` | Internal timer | ~every 10–15 s |
| `System Log` | `update` | Security/system event (login, etc.) | On event |
| `System Log Entry` | `update` | Same trigger as `System Log` | On event; deduplicate against it |

```json
{"name":"System Info Values","data":[{"api":"update","records":[{"ram1":"0","ram2":"0","cpu":"9.6690","cpuReal":"12.9013","maxUsage":97,"displayValue":true}],"type":"modify"}],"uniqueId":null}

{"name":"DateTime","data":[{"api":"update","records":["2026-05-13T22:51:01"],"type":"modify"}],"uniqueId":0}

{"name":"System Log","data":[{"api":"update","records":[{"id":774,"message":"User Login: admin","statusType":"status","eventFilterType":"security","eventType":"userLogin","dateTime":"2026-05-14T06:36:40"}],"type":"modify"}],"uniqueId":null}
```

#### 3.2.2 Mutations

| Inner name | `api` | Trigger | Records contain |
|---|---|---|---|
| `Modify system info` | `/system/frontPanel/info` | `POST /system/frontPanel/info` | `{powerState}` OR `{frontPanelLEDEnable}` (only changed field) |
| `Modify system info` | `/system/info` | `POST /system/info` | `{name, group, notes}` — full record |
| `Modify system time` | `/system/time` | `POST /system/time` | `{timeZone, utc, offSet, isdst, autoTimeControlEnable, autoDstEnable, twelveHourFormatEnable, isTimeEdited}` |
| `System Identify` | `/system/identify` | `GET /system/identify` | `records: []` (empty), `uniqueId: 0` |
| `Emergency Mute` | n/a | `POST /system/premute` | `data: []`, `uniqueId: 0` |
| `Clear System Log` | `update` | `POST /system/log/clear` | `records: [null]`, `uniqueId: null` |
| `System Import End` | n/a | `GET /system/sync` | `data: []`, `uniqueId: 0` (fires on completion of database sync) |

Note that **two distinct events share the inner name `Modify system info`**, distinguished by the `api` field (`/system/frontPanel/info` vs `/system/info`). Router must dispatch on `api`, not just `name`.

Examples:

```json
{"name":"Modify system info","data":[{"api":"/system/frontPanel/info","records":[{"powerState":"Off"}],"type":"modify"}],"uniqueId":"cb0f9400-..."}

{"name":"Modify system info","data":[{"api":"/system/info","records":[{"name":"_test_name","group":"Default","notes":""}],"type":"modify"}],"uniqueId":"e0222aa0-..."}

{"name":"Modify system time","data":[{"api":"/system/time","records":[{"timeZone":"(UTC-05:00) America/New_York","utc":"America/New_York","offSet":-5,"isdst":true,"autoTimeControlEnable":true,"autoDstEnable":false,"twelveHourFormatEnable":true,"isTimeEdited":true}],"type":"modify"}],"uniqueId":"e0222aa0-..."}

{"name":"System Identify","data":[{"api":"/system/identify","records":[],"type":"modify"}],"uniqueId":0}

{"name":"Emergency Mute","data":[],"uniqueId":0}

{"name":"Clear System Log","data":[{"api":"update","records":[null],"type":"modify"}],"uniqueId":null}

{"name":"System Import End","data":[],"uniqueId":0}
```

### 3.3 `WorkingSettings`

The largest topic — DSP mutations of every kind.

| Inner name | `api` | Trigger | Records contain |
|---|---|---|---|
| `Set Chain Mute` | `/workingsettings/dsp/chain` | `POST /workingsettings/dsp/chain/mute/{channelId}` | `{id, isWorkingSettings, muted, mixerId, DSPChannelId, presetId}` |
| `Set mixer to output chain` | `/workingsettings/dsp/chain` | `POST /workingsettings/dsp/chain/mixer/{channelId}` | Same shape as `Set Chain Mute`. Distinguish by event name. |
| `Modify Channel Param` | `/workingsettings/dsp/channel` | `POST /workingsettings/dsp/channel/name/{channelId}` | `{id, defaultName, name, type, baseType, channelNumber}` — full record |
| `Modify DSP Mixer Parameter Value` | `/workingsettings/dsp/mixer/config/parameter` | `POST /workingsettings/dsp/mixer/config/parameter/{paramId}` or bulk version | `{id, value, index, channelId, DSPMixerConfigId, DSPMixerConfigParameterTypeId}` |
| `Modify virtual DVCA` | `/workingSettings/virtualDVCA/parameters` *(capital S — see gotcha)* | `POST /workingSettings/virtualDVCA/parameters/{paramId}` | `{id, index, DSPParameterTypeId, virtualDVCAConfigId, value}` |
| `Modify generalPurposeOutputConfiguration` | `/workingsettings/generalPurposeOutputConfiguration` | `POST /workingsettings/generalPurposeOutputConfiguration/{pinId}` | `{id, value, presetId, isWorkingSettings, generalPurposeOutputId}` (value is `"high"` or `"low"`) |
| `Update mixer display name` | `/workingsettings/dsp/mixer` | `POST /workingsettings/dsp/mixer/{mixerId}` | `{id, mixerNumber, defaultDisplayName, displayName}` — note key is `displayName` even though request body uses `name` |
| `New DSP Block` | multi (`/workingsettings/dsp/block` + `/workingsettings/dsp/block/parameter` + `.../subparameter`) | `POST /workingsettings/dsp/block` | Multi-op packet: the new block record plus all its parameters and subparameters as separate `data[*]` entries |
| `Delete DSP Block` | multi | `DELETE /workingsettings/dsp/block/{blockId}` | Multi-op packet mirroring `New DSP Block` with `type: "delete"` |
| `Modify DSP Parameter` | `/workingsettings/dsp/block/parameter` | `POST /workingsettings/dsp/block/parameter/{paramId}` | `{id, value, index, DSPParameterTypeId, DSPBlockId, dependentParameterValueId}` |
| `Copy DSP Chain Blocks` | multi | `POST /workingsettings/dsp/chain/copyTo/{channelId}` | Multi-op packet: delete dest chain's blocks, then create new from source |
| `Copy Paste DSP Block To Chain` | multi | `POST /workingsettings/dsp/block/copyto/{blockId}` | Multi-op packet: clear dest position, create new block + params from source |
| `Cut Paste DSP Block To Chain` | multi | `POST /workingsettings/dsp/block/cutpaste/{blockId}` | Multi-op packet: new block at dest (the original is moved, not duplicated) |
| `Clear DSP Chain` | `/workingsettings/dsp/block` | `POST /workingsettings/dsp/chain/clear/{channelId}` | Each removed block as a separate `data[*].records` entry, `type: "delete"` |
| `Template` | `/workingsettings/dsp/channel/template` | `POST /workingsettings/dsp/channel/template/save` (new) OR `POST .../template/name/{tplId}` (rename) | New: `{id, name, channelType}`, `type: "new"`. Rename: `{previousName, name}`, `type: "modify"` |
| `Template Loaded` | multi (`/workingsettings/dsp/block...`) | `POST /workingsettings/dsp/channel/template/load` | Multi-op packet: deletes the destination chain's existing blocks, creates new ones from the template |
| `Template Deleted` | `/workingsettings/dsp/channel/template` | `DELETE /workingsettings/dsp/channel/template/{tplId}` | `{id}`, `type: "delete"` |
| `Sync` | n/a | `GET /system/sync` | `data: []`, `uniqueId: 0` |

#### Example: chain mute

```json
{"name":"Set Chain Mute","data":[{"api":"/workingsettings/dsp/chain","records":[{"id":"InputChannel.1","isWorkingSettings":true,"muted":true,"mixerId":null,"DSPChannelId":"InputChannel.1","presetId":null}],"type":"modify"}],"uniqueId":"441d3770-..."}
```

#### Example: output mixer reassignment

```json
{"name":"Set mixer to output chain","data":[{"api":"/workingsettings/dsp/chain","records":[{"id":"OutputChannel.1","isWorkingSettings":true,"muted":false,"mixerId":"Mixer.2","DSPChannelId":"OutputChannel.1","presetId":null}],"type":"modify"}],"uniqueId":"cb0f9400-..."}
```

#### Example: channel rename

```json
{"name":"Modify Channel Param","data":[{"api":"/workingsettings/dsp/channel","records":[{"id":"InputChannel.1","defaultName":"Mic/Line 1","name":"TestRename","type":"Mic/Line Input","baseType":"Input","channelNumber":1}],"type":"modify"}],"uniqueId":"14347140-..."}
```

#### Example: crosspoint level

```json
{"name":"Modify DSP Mixer Parameter Value","data":[{"api":"/workingsettings/dsp/mixer/config/parameter","records":[{"id":"Mixer.1.InputChannel.1.Source Level","value":-3,"index":1,"channelId":"InputChannel.1","DSPMixerConfigId":"Mixer.1","DSPMixerConfigParameterTypeId":"Mixer.Source Level"}],"type":"modify"}],"uniqueId":"1cc64f50-..."}
```

`DSPMixerConfigParameterTypeId` discriminates crosspoint parameter kinds:

| Value | Meaning |
|---|---|
| `Mixer.Source Level` | dB level on the crosspoint |
| `Mixer.Source Mute` | per-crosspoint mute |
| `Mixer.Source Enabled` | enable/disable source contribution |

Crosspoint id format: `Mixer.{m}.InputChannel.{i}.<Param>`.

#### Example: DVCA level

```json
{"name":"Modify virtual DVCA","data":[{"api":"/workingSettings/virtualDVCA/parameters","records":[{"id":"DCAChannel.1.Level","index":1,"DSPParameterTypeId":"Virtual DCA.Level","virtualDVCAConfigId":1,"value":-1}],"type":"modify"}],"uniqueId":"441d3770-..."}
```

`DSPParameterTypeId` discriminates DVCA parameter kinds:

| Value | Meaning |
|---|---|
| `Virtual DCA.Level` | dB level |
| `Virtual DCA.Mute` | mute state |
| `Virtual DCA.Name` | DVCA's user-assigned name |

DVCA id format: `DCAChannel.{n}.{Level|Mute|Name}`.

#### Example: mixer rename

```json
{"name":"Update mixer display name","data":[{"api":"/workingsettings/dsp/mixer","records":[{"id":"Mixer.1","mixerNumber":1,"defaultDisplayName":"Mixer 1","displayName":"_test_mix"}],"type":"modify"}],"uniqueId":"8b6dca40-..."}
```

The request body uses `{"name": "..."}` but the event records carry the new name in the `displayName` field.

#### Example: GPO toggle

```json
{"name":"Modify generalPurposeOutputConfiguration","data":[{"api":"/workingsettings/generalPurposeOutputConfiguration","records":[{"id":"General Purpose Output Pin.1","value":"high","presetId":null,"isWorkingSettings":true,"generalPurposeOutputId":1}],"type":"modify"}],"uniqueId":"cb0f9400-..."}
```

#### Example: DSP block create

```json
{
  "name":"New DSP Block",
  "data":[
    {"api":"/workingsettings/dsp/block","records":[{"id":"OutputChannel.1.BlockPosition.1","sequence":1,"isWorkingSettings":true,"DSPChainId":"OutputChannel.1","DSPBlockTypeId":"Gain","meterId":null}],"type":"new"},
    {"api":"/workingsettings/dsp/block/parameter","records":[
      {"value":"No Link Group","id":"OutputChannel.1.BlockPosition.1.Link Group","index":null,"DSPParameterTypeId":"Gain.Link Group","DSPBlockId":"OutputChannel.1.BlockPosition.1","dependentParameterValueId":null},
      {"value":true,"id":"OutputChannel.1.BlockPosition.1.Bypass","index":null,"DSPParameterTypeId":"Gain.Bypass","DSPBlockId":"OutputChannel.1.BlockPosition.1","dependentParameterValueId":null}
    ],"type":"new"}
  ],
  "uniqueId":"..."
}
```

The `data[]` array enumerates every newly-created object: the block record, every parameter, every subparameter. A `Delete DSP Block` event is the mirror with `type: "delete"`.

#### Example: DSP block parameter set

```json
{"name":"Modify DSP Parameter","data":[{"api":"/workingsettings/dsp/block/parameter","records":[{"value":-3,"id":"OutputChannel.1.BlockPosition.1.Level","index":null,"DSPParameterTypeId":"Gain.Level","DSPBlockId":"OutputChannel.1.BlockPosition.1","dependentParameterValueId":null}],"type":"modify"}],"uniqueId":"12733840-..."}
```

#### Example: chain copy

```json
{"name":"Copy DSP Chain Blocks","data":[
  {"api":"/workingsettings/dsp/block","records":[],"type":"delete"},
  {"api":"/workingsettings/dsp/block/parameter","records":[],"type":"delete"},
  {"api":"/workingsettings/dsp/block/parameter/subparameter","records":[],"type":"delete"},
  {"api":"/workingsettings/dsp/block","records":[],"type":"new"},
  {"api":"/workingsettings/dsp/block/parameter","records":[],"type":"new"},
  {"api":"/workingsettings/dsp/block/parameter/subparameter","records":[],"type":"new"}
],"uniqueId":"..."}
```

Empty `records` here because the source chain was empty. With non-empty chains, the entries carry full block/parameter records.

#### Bulk operations

The endpoints
`POST /workingsettings/dsp/mixer/config/parameter/bulk` and
`POST /workingsettings/dsp/block/parameter/bulk` use the body shape
`{ids: [...], values: [...]}` and emit **one event per affected record**,
not one bulk event. Apply the standard `Modify DSP Mixer Parameter Value`
or `Modify DSP Parameter` handler per record.

### 3.4 `Preset`

Preset CRUD plus the 3-phase recall protocol.

| Inner name | Trigger | Records / data shape |
|---|---|---|
| `Create preset` | `POST /preset/full` OR `POST /preset/subpreset` | `{id, name, type}` where `type` is `"Preset"` or `"Subpreset"` |
| `Change preset name` | `POST /preset/{id}` (rename) | `{id, name, previousName, type}` |
| `Update preset` | `POST /preset/update/{id}` | `{id, name, type}` |
| `Update Sub-Preset` | `POST /preset/subpreset/update/{id}` | `{id, name, type: "Subpreset"}` |
| `Delete preset` | `DELETE /preset/{id}` (presets and subpresets) | Nested: `[{api:"/preset/lastRecalled", records:[{lastRecalledPreset:"None", modified:false}]}, {api:"/preset", records:[{id, name, type}], type:"delete"}]` |
| `Preset Recall Begin` | `POST /preset/recall/{id}` (start) | `data: []`, originator's `uniqueId` |
| `Preset Recall` | Same trigger, middle phase | Bulk delta — list of ops covering every changed area (see §3.4.1) |
| `Preset Recall End` | Same trigger, completion | `data: []`, `uniqueId: 0` |
| `Last Recalled Preset Modified` | First WS mutation after a recall that diverges from the recalled preset | `data: [{modified: true}]`, `uniqueId: null` |

#### 3.4.1 `Preset Recall` (middle phase)

A single packet that can reach ~400 kB. `data` is a list of operations covering every changed area. Observed `api` values on the AQM1208:

- `/preset/lastRecalled`
- `/workingSettings/virtualDVCA/parameters`
- `/workingsettings/dsp/block`
- `/workingsettings/dsp/block/parameter`
- `/workingsettings/dsp/block/parameter/subparameter`
- `/workingsettings/dsp/chain`
- `/workingsettings/dsp/mixer`
- `/workingsettings/dsp/mixer/config/parameter`
- `/workingsettings/generalPurposeOutputConfiguration`

For state-mirroring consumers, the simplest correct response is: on
`Preset Recall End`, force a full REST snapshot. Processing the middle
delta incrementally is possible but error-prone given the size.

#### Examples

```json
{"name":"Create preset","data":[{"api":"/preset","records":[{"id":"_haashly_test","name":"_haashly_test","type":"Preset"}],"type":"new"}],"uniqueId":"..."}

{"name":"Change preset name","data":[{"api":"/preset","records":[{"id":"_haashly_test_v2","name":"_haashly_test_v2","previousName":"_haashly_test","type":"Preset"}],"type":"modify"}],"uniqueId":"..."}

{"name":"Update preset","data":[{"api":"/preset","records":[{"id":"_haashly_test_v2","name":"_haashly_test_v2","type":"Preset"}],"type":"modify"}],"uniqueId":"..."}

{"name":"Update Sub-Preset","data":[{"api":"/preset","records":[{"id":"_haashly_sub","name":"_haashly_sub","type":"Subpreset"}],"type":"modify"}],"uniqueId":"..."}

{"name":"Delete preset","data":[{"api":"/preset","records":[{"api":"/preset/lastRecalled","records":[{"lastRecalledPreset":"None","modified":false}],"type":"modify"},{"api":"/preset","records":[{"id":"_haashly_test_v2","name":"_haashly_test_v2","type":"Preset"}],"type":"delete"}],"type":"delete"}],"uniqueId":"..."}

{"name":"Preset Recall Begin","data":[],"uniqueId":"..."}
{"name":"Preset Recall End","data":[],"uniqueId":0}

{"name":"Last Recalled Preset Modified","data":[{"modified":true}],"uniqueId":null}
```

### 3.5 `Events` (event scheduler)

Event scheduler CRUD and scheduler-state signals. Note the plural topic name — `Events`, not `Event`.

| Inner name | Trigger | Records / data shape |
|---|---|---|
| `New Toggled Trigger Event` | `POST /event/triggered/toggleOperation` | `[{"0": {id, name, eventTypeId, triggerConfiguration: {...}}}]` — note the numerically-indexed dict wrapper (device serialisation quirk) |
| `Update Toggled Trigger Event` | `POST /event/triggered/toggleOperation/{id}` (overwrite) | Two-op: delete-old + new-with-incremented-id (the underlying record is replaced, not mutated) |
| `New Trigger Action Sequence Event` | `POST /event/triggered/actionSequence` | `{id, name, eventTypeId, triggerConfiguration: {triggerOperationTypeId: 9, ...}}` |
| `Update Triggered Action Sequence Event` | `POST /event/triggered/actionSequence/{id}` | Two-op: delete-old + new |
| `New Scheduled Event` | `POST /event/scheduled` | `{id, name, eventTypeId, actions, schedule: {scheduleTypeId, scheduleParameters: [...]}}` |
| `Update Scheduled Event` | `POST /event/scheduled/{id}` | Two-op: delete-old + new |
| `Delete Event` | `DELETE /event/{id}` | `{id, name, eventTypeId}`, `type: "delete"` |
| `All Scheduled Events Blocked` | Test-fire of a triggerOperationTypeId 8 (Pause / Resume Schedules) event with `state=high` | `data: [{}]`, `uniqueId: 0` |
| `All Scheduled Events Enabled` | Test-fire of a triggerOperationTypeId 8 event with `state=low` (resume) | `data: [{}]`, `uniqueId: 0` |

Note the **`Update *` pattern**: overwriting an event is not a field-level mutation — the device deletes the old record and creates a new one with a new ID. Subsequent calls referencing the old ID will return 422. Always re-fetch the event ID after overwrite.

#### Examples

```json
{"name":"New Toggled Trigger Event","data":[{"api":"/event","records":[{"0":{"id":3,"name":"_haashly_evt","eventTypeId":1,"triggerConfiguration":{"id":3,"eventId":3,"triggerOperationTypeId":8,"generalPurposeInputId":1,"triggerOperationParameters":[]}}}],"type":"new"}],"uniqueId":"..."}

{"name":"Update Toggled Trigger Event","data":[
  {"api":"/event","records":[{"id":3,"name":"_haashly_evt","eventTypeId":1}],"type":"delete"},
  {"api":"/event","records":[{"id":4,"name":"_haashly_evt_renamed","eventTypeId":1,"triggerConfiguration":{...}}],"type":"new"}
],"uniqueId":"..."}

{"name":"New Scheduled Event","data":[{"api":"/event","records":[{"id":5,"name":"_haashly_sched","eventTypeId":2,"actions":[],"schedule":{"id":1,"eventId":5,"scheduleTypeId":1,"scheduleParameters":[{"id":1,"scheduleParameterTypeId":1,"scheduleId":1,"value":"2030-01-01T12:00:00"}]}}],"type":"new"}],"uniqueId":"..."}

{"name":"Delete Event","data":[{"api":"/event","records":[{"id":2,"name":"_haashly_evt_renamed","eventTypeId":1}],"type":"delete"}],"uniqueId":"..."}

{"name":"All Scheduled Events Blocked","data":[{}],"uniqueId":0}
{"name":"All Scheduled Events Enabled","data":[{}],"uniqueId":0}
```

#### 3.5.1 Operation types

`triggerConfiguration.triggerOperationTypeId` values, from
`GET /event/operation/type`:

| ID | Type |
|---|---|
| 1 | Paging |
| 3 | Channel Mute |
| 4 | Mixer Mute |
| 5 | Preset Toggle |
| 6 | A/B Source Select |
| 7 | General Purpose Output Toggle |
| 8 | Pause / Resume Schedules |
| 9 | Action Sequence (used for `/event/triggered/actionSequence` endpoint) |

Action type IDs, from `GET /event/actions/types`:

| ID | Type |
|---|---|
| 1 | Source Select |
| 2 | Preset Recall |
| 3 | Subpreset Recall |
| 4 | Power On |
| 5 | Power Off |
| 6 | GPO High |
| 7 | GPO Low |
| 8 | Gain Increment |
| 9 | … (continues in API) |

Schedule type IDs, from `GET /event/scheduled/type`: `1=One Time`, `2=Daily`, `3=Weekly`, `4=Monthly`, `5=Yearly`.

#### 3.5.2 `runTestEvent`

`POST /event/triggerEvent/runTestEvent/{id}` with body `{state: "high"|"low"}` fires the event as if its GPI had transitioned. The push event emitted depends on the operation type — e.g. operation type 8 (Pause/Resume) with `state=high` emits `All Scheduled Events Blocked` on the `Events` topic. Other operation types may emit different events on other topics (a Preset Toggle fires `Preset Recall *`, etc.). No generic "event fired" notification was observed.

The `state` field accepts only `"high"` or `"low"`; other values return HTTP 422 "Value (X) was not one of the allowed values".

### 3.6 `MicPreamp`

Topic dedicated to mic preamp gain. **Not in the Portal's join list**, but the device emits to it.

| Inner name | `api` | Trigger | Records |
|---|---|---|---|
| `Change Mic Preamp Gain` | `/micPreamp` | `POST /micPreamp/{n}` | `{id, DSPChannelId, gain, micPreampTypeId}` |

```json
{"name":"Change Mic Preamp Gain","data":[{"api":"/micPreamp","records":[{"id":1,"DSPChannelId":"InputChannel.1","gain":6,"micPreampTypeId":1}],"type":"modify"}],"uniqueId":"cb0f9400-..."}
```

`gain` is in dB, 0–66 in 6 dB steps (per device manual).

### 3.7 `PhantomPower`

Topic dedicated to phantom power. **Not in the Portal's join list**.

| Inner name | `api` | Trigger | Records |
|---|---|---|---|
| `Change Phantom Power` | `/phantomPower` | `POST /phantomPower/{n}` | `{id, DSPChannelId, phantomPowerEnabled}` |

```json
{"name":"Change Phantom Power","data":[{"api":"/phantomPower","records":[{"id":1,"DSPChannelId":"InputChannel.1","phantomPowerEnabled":true}],"type":"modify"}],"uniqueId":"cb0f9400-..."}
```

### 3.8 `Network`

Periodic link-state heartbeat.

| Inner name | `api` | Trigger | `uniqueId` |
|---|---|---|---|
| `detected updated network parameters` | `/network` | ~every 7 s, internal timer | `0` |

```json
{"name":"detected updated network parameters","data":[{"api":"/network","records":[{"pluggedIn":true}],"type":"modify"}],"uniqueId":0}
```

The cadence is too coarse to use as a liveness ping. Channel Meters (5 Hz) or System Info Values (1 Hz) are better signals.

### 3.9 `Security`

User / role / permission CRUD events. **Not in the Portal's join list** but the device emits to it.

| Inner name | Trigger | Records |
|---|---|---|
| `New User,` (trailing comma is intentional in firmware) | `POST /security/users` | Multi-op: `security/users` + `security/users/roles` + `security/users/permission` (full record set) with `type: "new"` |
| `Permissions Updated` | `POST /security/users/permission/{userId}` | Full updated permissions array on `api: "security/users/permission"` |
| `Update password,` (trailing comma) | `POST /security/users/password/{username}` | `[{api: "security/user", records: [{id, username}], type: "update"}]` |
| `Delete User,` (trailing comma) | `DELETE /security/users/{id}` | Multi-op mirror of `New User,` with `type: "delete"` |

#### Example: user creation

```json
{
  "name": "New User,",
  "data": [
    {"api":"security/users","records":[{"id":"haashlyprobe","username":"haashlyprobe"}],"type":"new"},
    {"api":"security/users/roles","records":[{"id":"haashlyprobe.Guest Admin","userId":"haashlyprobe","roleTypeId":"Guest Admin"}],"type":"new"},
    {"api":"security/users/permission","records":[
       {"id":"haashlyprobe.Guest Admin.View Accounts","enabled":true,"roleId":"haashlyprobe.Guest Admin","permissionTypeId":"Guest Admin.View Accounts"},
       /* ...one entry per permission for the role... */
    ],"type":"new"}
  ],
  "uniqueId":"42c25a60-..."
}
```

#### Notes
- The `api` field for security events uses `"security/..."` (no leading slash) — different from all other topics where `api` starts with `/`.
- Event names contain literal trailing commas (`New User,`, `Update password,`, `Delete User,`). These are firmware quirks and must be matched exactly.
- `Permissions Updated` is the only one without a trailing comma.
- `System Log` and `System Log Entry` also fire alongside each user-CRUD event with `eventType` `"userAccountCreated"` / `"userLogout"` / etc., providing an audit-trail breadcrumb on the `System` topic.

See `docs/SECURITY-API.md` for the full user-management REST API and the recommended HA integration flow.

### 3.10 `Firmware`

Not exercised — would require running an actual firmware update via `POST /system/upload/firmware`. Expected to carry chunk-upload progress and post-flash status. Topic and shape unconfirmed.

---

## 4. Trigger ↔ Event Matrix

Inverse view: REST endpoint → emitted event. Useful for implementing the router.

| REST endpoint | Method | Topic | Inner event name |
|---|---|---|---|
| `/session/login` | POST | `System` | `System Log` + `System Log Entry` (login security event) |
| `/system/frontPanel/info` (`powerState`) | POST | `System` | `Modify system info` (api `/system/frontPanel/info`) |
| `/system/frontPanel/info` (`frontPanelLEDEnable`) | POST | `System` | `Modify system info` (api `/system/frontPanel/info`) |
| `/system/info` | POST | `System` | `Modify system info` (api `/system/info`) |
| `/system/time` | POST | `System` | `Modify system time` |
| `/system/identify` | GET | `System` | `System Identify` |
| `/system/premute` | POST | `System` | `Emergency Mute` |
| `/system/log` | POST | `System` | `System Log` + `System Log Entry` |
| `/system/log/clear` | POST | `System` | `Clear System Log` |
| `/system/sync` | GET | `WorkingSettings` + `System` | `Sync` + `System Import End` |
| `/workingsettings/dsp/chain/mute/{id}` | POST | `WorkingSettings` | `Set Chain Mute` |
| `/workingsettings/dsp/chain/mute/alloutputs` | POST | `WorkingSettings` | `Set Chain Mute` × N (one per output) |
| `/workingsettings/dsp/chain/mixer/{id}` | POST | `WorkingSettings` | `Set mixer to output chain` |
| `/workingsettings/dsp/chain/clear/{id}` | POST | `WorkingSettings` | `Clear DSP Chain` |
| `/workingsettings/dsp/chain/copyTo/{id}` | POST | `WorkingSettings` | `Copy DSP Chain Blocks` |
| `/workingsettings/dsp/channel/name/{id}` | POST | `WorkingSettings` | `Modify Channel Param` |
| `/workingsettings/dsp/mixer/config/parameter/{id}` | POST | `WorkingSettings` | `Modify DSP Mixer Parameter Value` |
| `/workingsettings/dsp/mixer/config/parameter/bulk` | POST | `WorkingSettings` | `Modify DSP Mixer Parameter Value` × N |
| `/workingsettings/dsp/mixer/{id}` (rename) | POST | `WorkingSettings` | `Update mixer display name` |
| `/workingSettings/virtualDVCA/parameters/{id}` | POST | `WorkingSettings` | `Modify virtual DVCA` |
| `/workingsettings/generalPurposeOutputConfiguration/{id}` | POST | `WorkingSettings` | `Modify generalPurposeOutputConfiguration` |
| `/workingsettings/dsp/block` | POST | `WorkingSettings` | `New DSP Block` |
| `/workingsettings/dsp/block/{id}` | DELETE | `WorkingSettings` | `Delete DSP Block` |
| `/workingsettings/dsp/block/parameter/{id}` | POST | `WorkingSettings` | `Modify DSP Parameter` |
| `/workingsettings/dsp/block/parameter/bulk` | POST | `WorkingSettings` | `Modify DSP Parameter` × N |
| `/workingsettings/dsp/block/copyto/{id}` | POST | `WorkingSettings` | `Copy Paste DSP Block To Chain` |
| `/workingsettings/dsp/block/cutpaste/{id}` | POST | `WorkingSettings` | `Cut Paste DSP Block To Chain` |
| `/workingsettings/dsp/channel/template/save` | POST | `WorkingSettings` | `Template` (with `type:"new"`) |
| `/workingsettings/dsp/channel/template/name/{tplId}` | POST | `WorkingSettings` | `Template` (with `type:"modify"`) |
| `/workingsettings/dsp/channel/template/load` | POST | `WorkingSettings` | `Template Loaded` |
| `/workingsettings/dsp/channel/template/{tplId}` | DELETE | `WorkingSettings` | `Template Deleted` |
| `/micPreamp/{n}` | POST | `MicPreamp` | `Change Mic Preamp Gain` |
| `/phantomPower/{n}` | POST | `PhantomPower` | `Change Phantom Power` |
| `/preset/full` | POST | `Preset` | `Create preset` (with `type:"Preset"`) |
| `/preset/{id}` (rename) | POST | `Preset` | `Change preset name` |
| `/preset/update/{id}` | POST | `Preset` | `Update preset` |
| `/preset/subpreset` | POST | `Preset` | `Create preset` (with `type:"Subpreset"`) |
| `/preset/subpreset/update/{id}` | POST | `Preset` | `Update Sub-Preset` |
| `/preset/{id}` | DELETE | `Preset` | `Delete preset` |
| `/preset/recall/{id}` | POST | `Preset` | `Preset Recall Begin` → `Preset Recall` → `Preset Recall End` |
| any post-recall WS mutation | (varies) | `Preset` | `Last Recalled Preset Modified` (in addition to the WS event) |
| `/event/triggered/toggleOperation` | POST | `Events` | `New Toggled Trigger Event` |
| `/event/triggered/toggleOperation/{id}` | POST | `Events` | `Update Toggled Trigger Event` |
| `/event/triggered/actionSequence` | POST | `Events` | `New Trigger Action Sequence Event` |
| `/event/triggered/actionSequence/{id}` | POST | `Events` | `Update Triggered Action Sequence Event` |
| `/event/scheduled` | POST | `Events` | `New Scheduled Event` |
| `/event/scheduled/{id}` | POST | `Events` | `Update Scheduled Event` |
| `/event/{id}` | DELETE | `Events` | `Delete Event` |
| `/event/triggerEvent/runTestEvent/{id}` | POST | (varies by operation type) | For opType 8: `All Scheduled Events Blocked`. For other opTypes: equivalent to the action firing (Preset Recall, GPO toggle, etc.) |
| `/event/resumeSchedule` | POST | (none observed) | No specific event; 200 OK is the only signal |
| `/generalPurposeInput` | GET | n/a | (No push — GPI state is not exposed by the device, see §6) |

---

## 5. Traffic Profile

Measured over a 60 s authenticated capture with no triggers:

| Source | Rate | Avg size | Throughput |
|---|---|---|---|
| `System Info Values` | 1 Hz | 213 B | ~213 B/s |
| `DateTime` | ~every 15 s | 118 B | ~8 B/s |
| `Channel Meters` | ~5 Hz | ~400 B | ~2 KB/s |
| `Network / detected updated network parameters` | ~every 7 s | ~150 B | ~21 B/s |

**Idle baseline without meters: ~250 B/s ≈ 900 KB/h.**
**Idle baseline with meters: ~2.25 KB/s ≈ 8 MB/h.**

Per state-change event size:

| Event | Size |
|---|---|
| `Modify system info` | ~190 B |
| `Modify system time` | ~340 B |
| `Modify DSP Mixer Parameter Value` | ~250 B |
| `Modify virtual DVCA` | ~210 B |
| `Modify DSP Parameter` | ~280 B |
| `Set Chain Mute` | ~220 B |
| `Set mixer to output chain` | ~220 B |
| `Modify Channel Param` | ~270 B |
| `Modify generalPurposeOutputConfiguration` | ~240 B |
| `Update mixer display name` | ~210 B |
| `Change Mic Preamp Gain` | ~180 B |
| `Change Phantom Power` | ~170 B |
| `New DSP Block` | varies; per-parameter (a Gain block adds ~6 params); ~1–2 KB |
| `Delete DSP Block` | mirror of create |
| `Modify DSP Parameter` | ~280 B |
| `Copy/Cut Paste DSP Block` | similar to create (~1–2 KB) |
| `Copy DSP Chain Blocks` | proportional to chain depth |
| `Template`, `Template Loaded`, `Template Deleted` | ~200 B – 2 KB depending on template |
| `Preset Recall Begin` | 95 B |
| `Preset Recall` (full delta) | **~400 KB** |
| `Preset Recall End` | 56 B |
| `Create preset` / `Update preset` / `Update Sub-Preset` / `Delete preset` / `Change preset name` | ~200 B |
| `Last Recalled Preset Modified` | ~80 B |
| `New Toggled Trigger Event` / `New Scheduled Event` / similar | ~300 – 500 B |
| `Update *` event variants | ~500 B – 1 KB (delete-old + new combined) |
| `Delete Event` | ~150 B |
| `All Scheduled Events Blocked` | ~80 B |
| `System Identify` | ~120 B |
| `Emergency Mute` | 65 B |
| `Clear System Log` | ~120 B |
| `Sync` / `System Import End` | ~80 B |

For comparison: the current REST poll cycle (9 endpoints, 30 s) costs
~30 KB per cycle ≈ 2 MB/h, so push is ~2× cheaper at idle even before
counting state changes.

---

## 6. Gaps and Untestable Items

### 6.1 REST capabilities with NO push coverage

#### GPI input pin state

`/generalPurposeInput` returns only static pin configuration
(IDs and pin numbers, 8 pins on AQM1208) — no field carries the current
logical level. No `/generalPurposeInput/{n}` or `/generalPurposeInput/state`
endpoint exists. The device uses GPI internally for its event scheduler
(e.g. "GPI 1 high → recall preset X") but the raw pin state itself is
**not observable** via either REST or push.

Workaround for HA: configure each GPI to recall a preset via the device's
event scheduler in the Portal, then react to the (pushed) preset recall.

#### `POST /event/resumeSchedule`

Returns 200 OK but emits no specific push event. The action's side effect
(resuming paused scheduled events) does not surface as a notification —
clients must infer state from subsequent scheduled-event firings.

### 6.2 Push events likely emitted but UNVERIFIED on this device

#### PEQ / FBS / GEQ block parameter mutation, subparameter mutation, and flatten

These block types exist in the swagger but were not in
`GET /workingsettings/dsp/block/type`'s `availableChannels` list on the
AQM1208 used here. As a result, the following endpoints could not be
exercised:

- `POST /workingsettings/dsp/block/parameter/subparameter/{id}` (only relevant to filter blocks)
- `POST /workingsettings/dsp/block/parameter/subparameter/bulk`
- `POST /workingsettings/dsp/block/peq/flattenFilters/{id}`
- `POST /workingsettings/dsp/block/geq/flattenFilters/{id}`
- `POST /workingsettings/dsp/block/fbs/flattenFilters/{id}`
- `POST /workingsettings/dsp/block/fbs/flattenFloatingFilters/{id}`

Based on the mirror pattern with `Modify DSP Parameter` and `Modify DSP Mixer Parameter Value`, subparameter mutation is expected to emit a `Modify DSP Subparameter` event with records carrying `{id, value, index, DSPParameterTypeId, DSPBlockId, dependentParameterValueId}`. Flatten endpoints likely emit per-filter `Modify DSP Parameter` events.

#### Firmware update progress

`Firmware` topic is documented in the bundle as a subscription target but
was not exercised — would require running a real firmware update via
`POST /system/upload/firmware`. Inner event names and shape are unknown.

#### FIR file upload

`POST /workingsettings/dsp/fir/{channelId}` requires a binary FIR sample
file. Not tested. Likely emits an event on `WorkingSettings`.

#### Dante channel sources

`/workingsettings/dante/inputChannelSources` and
`/workingsettings/dante/mixerInputSources` return empty arrays on this
unit (no Dante card present). Push behaviour on Dante-equipped devices is
unknown.

#### Remotes (wallplate accessory config)

`POST /remotes`, `POST /remotes/{name}`, `DELETE /remotes/{name}` and the
related image-upload endpoints (`POST /remotes/images/{name}`) require
binary image files. Not tested.

#### Security / user CRUD

`POST /security/users`, `POST /security/users/{id}`,
`DELETE /security/users/{id}`, `POST /security/users/password/{username}`,
`POST /security/users/permission/{userId}` were **skipped intentionally** —
a misfire could lock out the only admin account.

### 6.3 Endpoints intentionally not tested (destructive or risky)

| Endpoint | Why skipped |
|---|---|
| `POST /system/upload/firmware` | Would flash new firmware. Catastrophic if wrong. |
| `POST /system/upload/firmware/begin` | Same. |
| `POST /system/upload/import/{flags}` | Would wipe and replace device config. |
| `POST /factoryReset` | Self-explanatory. |
| `POST /workingsettings/dsp/resetSignalChain` | Wipes the entire DSP graph. |
| `POST /workingsettings/dsp/clearRouting` | Clears all mixer routing (reversible but disruptive on a live system). |
| `POST /network` | Could change IP and lose connectivity. |
| Security/User CRUD | See §6.2. |

### 6.4 Read-only endpoints with no push (and no need for one)

These change so rarely or are derived state, that a push channel would be
overkill:

| Endpoint | Why no push needed |
|---|---|
| `/system/info` (GET) | Firmware/MAC/model — changes only on firmware update (pushed via `Firmware` then) |
| `/system/firmwareRev` | Same |
| `/system/features` | Static device capability flags |
| `/system/platform` | Static |
| `/system/errorCodes` | Static error-code dictionary |
| `/system/info/channels` | Static topology |
| `/system/rearPanel/info` | Static device topology |
| `/system/meterScreenType`, `/system/frontPanelMeterType` | Static |
| `/system/debugControl/info` | Diagnostic counters |
| `/session/checkCurrentLogin` | Per-request validation |
| `/preset/{name}` | Full preset content (read on recall, which already pushes) |
| `/preset/export/{presetId}` | Export blob |
| `/workingsettings/dsp/totalDSPCost` | Derived from current blocks (mutations push) |
| `/workingsettings/dsp/mixer/channels` | Static topology |
| `/workingsettings/dsp/mixer/config` | Mutations push individual params |
| `/system/upload/config`, `/remotes/upload/config` | Throttling config, static |

---

## 7. Gotchas

1. **Mixed casing in `api` field.** `/workingsettings/...` is lowercase except `/workingSettings/virtualDVCA/...` which has a capital S. This is a device-side inconsistency. String-match exactly; do not normalise.

2. **Multiple events share the same `api`.**
   - `Set Chain Mute` and `Set mixer to output chain` both have `api: "/workingsettings/dsp/chain"`. Distinguish by `name`.
   - Two distinct `Modify system info` events exist, distinguished by `api` (`/system/frontPanel/info` vs `/system/info`). Distinguish by both `name` and `api`.

3. **One event name covers multiple parameter kinds.**
   - `Modify DSP Mixer Parameter Value` covers level/mute/source-enable — distinguish by `records[0].DSPMixerConfigParameterTypeId`.
   - `Modify virtual DVCA` covers level/mute/name — distinguish by `records[0].DSPParameterTypeId`.
   - `Modify DSP Parameter` covers every block-parameter type — distinguish by `records[0].DSPParameterTypeId`.

4. **Preset recall fires three events.** Treat `Preset Recall Begin` and `Preset Recall End` as a bracket; ideally suspend other event processing between them and force a full REST snapshot on End.

5. **`Update *` event-scheduler events are replace-not-mutate.** Overwriting an event deletes the old record and creates a new one with a new ID. Subsequent calls using the old ID return 422. Re-fetch the ID after each overwrite.

6. **Bulk endpoints emit per-record events, not bulk events.** A bulk POST with N items emits N separate `Modify *` events.

7. **No ack to `join`.** The server does not respond to subscription requests. There's no way to validate that a topic name is real without actually receiving events on it.

8. **`uniqueId` field shape is inconsistent.** It's a UUID string for user-originated events, the integer `0` for system-emitted events, and `null` for public broadcasts. Don't assume a single type.

9. **Phantom power and mic preamp aren't in the Portal's topic list.** If you mirror the Portal's subscriptions exactly (`Preset`, `Firmware`, `WorkingSettings`, `System`), you'll miss those two topics. Subscribe to them explicitly.

10. **Heartbeat noise.** `System Info Values` (1 Hz) and `detected updated network parameters` (every 7 s) account for the majority of frames at idle. Filter early.

11. **No client-side presence broadcast.** A clean `disconnect` does not fire a counterpart event to other authenticated clients.

12. **Cookie expiry.** Sessions expire on the device side. When `/v1.0-beta` REST returns 401, re-login and **reconnect the socket** with the new cookie — there is no in-flight cookie refresh path.

13. **Inconsistent event-name spelling.** `New Trigger Action Sequence Event` and `Update Triggered Action Sequence Event` differ by one word ("Trigger" vs "Triggered"). The pair is the device's, not a typo on our side.

14. **Two near-duplicates in security log.** `System / System Log` and `System / System Log Entry` both fire for every security event with slightly different payloads. Dedupe on `id` from `System Log` (the `Entry` variant lacks the ID).

15. **`Modify Channel Param` is full-record, not delta.** Unlike most `Modify *` events, channel rename's `records` carry every channel field, not just the changed `name`.

---

## 8. Source

Captured against an Ashly AQM1208 (firmware 1.1.8, MAC `00:14:aa:03:64:62`) in May 2026. Methodology:

1. Drove the AquaControl Portal in headless Chromium via Playwright; observed the topics it joins and the events it receives.
2. Fetched the authoritative endpoint list from `/swagger.json` (Swagger 2.0, 153 paths).
3. Connected an authenticated `python-socketio` v5 client and subscribed to all observed plus all plausible topic names.
4. Triggered every REST endpoint listed in §4 paired with a state restore where applicable; each emitted event was captured and categorised by `(topic, inner_name)`.
5. Counted and verified 10 topics × 55 distinct inner event names, all listed in §3 and mapped in §4.
