"""Pure event-routing logic for AquaControl push events.

Given a parsed :class:`aquacontrol.Event` and the current
:class:`AshlyDeviceData`, :func:`route_event` returns one of three things:

- A new :class:`AshlyDeviceData` with the relevant field patched — the
  push client should feed it to ``coordinator.async_set_updated_data``.
- :data:`NO_CHANGE` — the router recognised the event but the new value
  is identical to the current one. The push client should skip the
  state-update fan-out entirely.
- ``None`` — the router does not (or chooses not to) model this event
  shape. The push client should request a coordinator refresh so the
  next REST poll resyncs.

The module is intentionally HA-free. Every input is a plain dataclass
or :mod:`aquacontrol` value; every output is a plain dataclass or
sentinel. This keeps the dispatch logic unit-testable without spinning
up any HA harness.

See ``docs/WEBSOCKET-API.md`` §3 for the per-topic protocol reference.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:  # pragma: no cover
    from ._aquacontrol import Event
    from .coordinator import AshlyDeviceData

_LOGGER = logging.getLogger(__name__)


class _NoChange:
    """Sentinel type for :data:`NO_CHANGE`. Never construct directly."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "NO_CHANGE"


#: Returned when the router recognised an event but the value already
#: matches the current state. Distinct from ``None`` (which means
#: "refresh me, I don't understand this") so callers can skip the
#: state-update fan-out without paying for a dataclass copy + equality
#: comparison.
NO_CHANGE: Final[_NoChange] = _NoChange()

type RoutedResult = "AshlyDeviceData | _NoChange | None"
type _Handler = Callable[["Event", "AshlyDeviceData"], RoutedResult]


# ── id-format regexes (anchored — partial matches reject) ───────────


_CROSSPOINT_ID_RE = re.compile(
    r"^Mixer\.(?P<m>\d+)\.InputChannel\.(?P<i>\d+)\.(?P<kind>.+)$"
)
_DVCA_ID_RE = re.compile(r"^DCAChannel\.(?P<n>\d+)\.(?P<kind>Level|Mute|Name)$")
_GPO_ID_RE = re.compile(r"^General Purpose Output Pin\.(?P<n>\d+)$")


# ── helpers ─────────────────────────────────────────────────────────


def _first_record(event: Event) -> dict[str, Any] | None:
    """Return the first record dict, or None if the event has no usable record.

    Push events are single-op for the routable cases; multi-op events
    (Preset Recall, DSP block lifecycle) are routed unconditionally to
    refresh in :func:`route_event`'s topic fallthrough. So pulling the
    first record off the first operation is correct here.
    """
    if not event.is_single_operation or not event.records:
        return None
    rec = event.records[0]
    return rec if isinstance(rec, dict) else None


# ── per-event handlers ──────────────────────────────────────────────


def _route_set_chain_mute(event: Event, prev: AshlyDeviceData) -> RoutedResult:
    rec = _first_record(event)
    if rec is None:
        return None
    chan_id = rec.get("id")
    if not isinstance(chan_id, str) or chan_id not in prev.chains:
        return None
    new_muted = bool(rec.get("muted", False))
    existing = prev.chains[chan_id]
    if existing.muted == new_muted:
        return NO_CHANGE
    new_chains = dict(prev.chains)
    new_chains[chan_id] = dataclasses.replace(existing, muted=new_muted)
    return dataclasses.replace(prev, chains=new_chains)


def _route_set_mixer_to_output_chain(event: Event, prev: AshlyDeviceData) -> RoutedResult:
    """Output-chain mixer reassignment.

    The records carry both ``mixerId`` and ``muted``, so we update both
    fields when either differs — the device sends one event for the
    combined POST.
    """
    rec = _first_record(event)
    if rec is None:
        return None
    chan_id = rec.get("id")
    if not isinstance(chan_id, str) or chan_id not in prev.chains:
        return None
    new_mixer = rec.get("mixerId")
    new_mixer = new_mixer if isinstance(new_mixer, str) or new_mixer is None else None
    new_muted = bool(rec.get("muted", False))
    existing = prev.chains[chan_id]
    if existing.mixer_id == new_mixer and existing.muted == new_muted:
        return NO_CHANGE
    new_chains = dict(prev.chains)
    new_chains[chan_id] = dataclasses.replace(existing, mixer_id=new_mixer, muted=new_muted)
    return dataclasses.replace(prev, chains=new_chains)


def _route_modify_channel_param(event: Event, prev: AshlyDeviceData) -> RoutedResult:
    """Channel-rename event.

    Per WEBSOCKET-API.md §3.3 this event is only emitted for the
    ``POST /workingsettings/dsp/channel/name/{id}`` endpoint, so a
    ``name`` field in the record is always the new user-set name. If
    the record shape ever drifts, fall back to refresh.
    """
    rec = _first_record(event)
    if rec is None:
        return None
    chan_id = rec.get("id")
    new_name = rec.get("name")
    if not isinstance(chan_id, str) or not isinstance(new_name, str):
        return None
    if chan_id not in prev.channels:
        return None
    existing = prev.channels[chan_id]
    if existing.name == new_name:
        return NO_CHANGE
    new_channels = dict(prev.channels)
    new_channels[chan_id] = dataclasses.replace(existing, name=new_name)
    return dataclasses.replace(prev, channels=new_channels)


def _route_modify_dsp_mixer_parameter(event: Event, prev: AshlyDeviceData) -> RoutedResult:
    """Crosspoint parameter mutation: level, mute, or enabled.

    Only the Level and Mute subtypes are modelled in
    :class:`CrosspointState`; the Enabled subtype (rare) and any future
    parameter kind triggers a refresh.
    """
    rec = _first_record(event)
    if rec is None:
        return None
    xp_id = rec.get("id")
    param_type = rec.get("DSPMixerConfigParameterTypeId")
    if not isinstance(xp_id, str) or not isinstance(param_type, str):
        return None
    match = _CROSSPOINT_ID_RE.match(xp_id)
    if match is None:
        return None
    try:
        m, i = int(match["m"]), int(match["i"])
    except (TypeError, ValueError):  # pragma: no cover - regex guarantees digits
        return None
    key = (m, i)
    existing = prev.crosspoints.get(key)
    if existing is None:
        return None
    if param_type == "Mixer.Source Level":
        try:
            new_value = float(rec.get("value"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if existing.level_db == new_value:
            return NO_CHANGE
        patched = dataclasses.replace(existing, level_db=new_value)
    elif param_type == "Mixer.Source Mute":
        new_muted = bool(rec.get("value", False))
        if existing.muted == new_muted:
            return NO_CHANGE
        patched = dataclasses.replace(existing, muted=new_muted)
    else:
        # Source Enabled and any future parameter kind: refresh.
        return None
    new_crosspoints = dict(prev.crosspoints)
    new_crosspoints[key] = patched
    return dataclasses.replace(prev, crosspoints=new_crosspoints)


def _route_modify_virtual_dvca(event: Event, prev: AshlyDeviceData) -> RoutedResult:
    """DVCA parameter: level, mute, or name."""
    rec = _first_record(event)
    if rec is None:
        return None
    dvca_id = rec.get("id")
    param_type = rec.get("DSPParameterTypeId")
    if not isinstance(dvca_id, str) or not isinstance(param_type, str):
        return None
    match = _DVCA_ID_RE.match(dvca_id)
    if match is None:
        return None
    try:
        n = int(match["n"])
    except (TypeError, ValueError):  # pragma: no cover - regex guarantees digits
        return None
    existing = prev.dvca.get(n)
    if existing is None:
        return None
    if param_type == "Virtual DCA.Level":
        try:
            new_level = float(rec.get("value"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if existing.level_db == new_level:
            return NO_CHANGE
        patched = dataclasses.replace(existing, level_db=new_level)
    elif param_type == "Virtual DCA.Mute":
        new_muted = bool(rec.get("value", False))
        if existing.muted == new_muted:
            return NO_CHANGE
        patched = dataclasses.replace(existing, muted=new_muted)
    elif param_type == "Virtual DCA.Name":
        new_name = rec.get("value")
        if not isinstance(new_name, str):
            return None
        if existing.name == new_name:
            return NO_CHANGE
        patched = dataclasses.replace(existing, name=new_name)
    else:
        return None
    new_dvca = dict(prev.dvca)
    new_dvca[n] = patched
    return dataclasses.replace(prev, dvca=new_dvca)


def _route_change_mic_preamp_gain(event: Event, prev: AshlyDeviceData) -> RoutedResult:
    rec = _first_record(event)
    if rec is None:
        return None
    input_id = rec.get("id")
    gain = rec.get("gain")
    if not isinstance(input_id, int) or not isinstance(gain, int):
        return None
    if prev.mic_preamp_gain.get(input_id) == gain:
        return NO_CHANGE
    new_gains = dict(prev.mic_preamp_gain)
    new_gains[input_id] = gain
    return dataclasses.replace(prev, mic_preamp_gain=new_gains)


def _route_change_phantom_power(event: Event, prev: AshlyDeviceData) -> RoutedResult:
    rec = _first_record(event)
    if rec is None:
        return None
    input_id = rec.get("id")
    if not isinstance(input_id, int):
        return None
    new_enabled = bool(rec.get("enabled", False))
    if prev.phantom_power.get(input_id) == new_enabled:
        return NO_CHANGE
    new_phantom = dict(prev.phantom_power)
    new_phantom[input_id] = new_enabled
    return dataclasses.replace(prev, phantom_power=new_phantom)


def _route_modify_gpo(event: Event, prev: AshlyDeviceData) -> RoutedResult:
    """GPO toggle. Wire ``value`` is ``"high"`` / ``"low"`` (string)."""
    rec = _first_record(event)
    if rec is None:
        return None
    pin_id = rec.get("id")
    value = rec.get("value")
    if not isinstance(pin_id, str) or not isinstance(value, str):
        return None
    match = _GPO_ID_RE.match(pin_id)
    if match is None:
        return None
    try:
        n = int(match["n"])
    except (TypeError, ValueError):  # pragma: no cover - regex guarantees digits
        return None
    new_high = value.lower() == "high"
    if prev.gpo.get(n) == new_high:
        return NO_CHANGE
    new_gpo = dict(prev.gpo)
    new_gpo[n] = new_high
    return dataclasses.replace(prev, gpo=new_gpo)


def _route_modify_system_info(event: Event, prev: AshlyDeviceData) -> RoutedResult:
    """Dispatch on ``api`` path, not name.

    The inner name ``Modify system info`` is reused by two distinct
    events: ``/system/info`` (device-wide rename) and
    ``/system/frontPanel/info`` (power state + LED enable). Only the
    front-panel form maps to fields the integration models; the rename
    is on ``SystemInfo`` which we treat as static at setup, so refresh.
    """
    if event.api != "/system/frontPanel/info":
        return None
    rec = _first_record(event)
    if rec is None:
        return None
    fp = prev.front_panel
    new_power = fp.power_on
    new_leds = fp.leds_enabled
    if "powerState" in rec:
        ps = rec.get("powerState")
        if not isinstance(ps, str):
            return None
        new_power = ps.lower() == "on"
    if "frontPanelLEDEnable" in rec:
        leds = rec.get("frontPanelLEDEnable")
        if not isinstance(leds, bool):
            return None
        new_leds = leds
    if new_power == fp.power_on and new_leds == fp.leds_enabled:
        return NO_CHANGE
    return dataclasses.replace(
        prev,
        front_panel=dataclasses.replace(fp, power_on=new_power, leds_enabled=new_leds),
    )


# ── dispatch table + public surface ─────────────────────────────────


_DISPATCH: dict[str, _Handler] = {
    "Set Chain Mute": _route_set_chain_mute,
    "Set mixer to output chain": _route_set_mixer_to_output_chain,
    "Modify Channel Param": _route_modify_channel_param,
    "Modify DSP Mixer Parameter Value": _route_modify_dsp_mixer_parameter,
    "Modify virtual DVCA": _route_modify_virtual_dvca,
    "Change Mic Preamp Gain": _route_change_mic_preamp_gain,
    "Change Phantom Power": _route_change_phantom_power,
    "Modify generalPurposeOutputConfiguration": _route_modify_gpo,
    "Modify system info": _route_modify_system_info,
}


#: Inner event names the router knows how to patch directly. The push
#: client uses this to drive its per-event handler registration so the
#: dispatch table here is the single source of truth.
ROUTABLE_EVENT_NAMES: Final[frozenset[str]] = frozenset(_DISPATCH.keys())


def route_event(event: Event, prev: AshlyDeviceData) -> RoutedResult:
    """Apply a push event to coordinator state.

    Returns:
        - new :class:`AshlyDeviceData` for the caller to publish via
          ``coordinator.async_set_updated_data``;
        - :data:`NO_CHANGE` if the event was recognised but the value
          already matches (caller skips the fan-out);
        - ``None`` if the router doesn't model this event — caller
          should trigger a coordinator refresh.
    """
    handler = _DISPATCH.get(event.name)
    if handler is None:
        return None
    try:
        return handler(event, prev)
    except Exception:  # pragma: no cover - defensive; handlers are pure
        _LOGGER.exception(
            "Router handler for %s raised on payload %s", event.name, event.raw_truncated(512)
        )
        return None


__all__ = ["NO_CHANGE", "ROUTABLE_EVENT_NAMES", "route_event"]
