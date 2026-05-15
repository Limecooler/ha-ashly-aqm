"""Diagnostics support for the Ashly Audio integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import AshlyConfigEntry

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import AshlyCoordinator
    from .push import AshlyPushClient

# session_id is a per-WS-connection UUID — not a credential, but it
# correlates logs across a support ticket. Redact for hygiene since
# diagnostics bundles are routinely pasted into public GitHub issues.
TO_REDACT = {"password", "host", "mac_address", "session_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AshlyConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Bug-report-oriented: includes the full polled state of the device,
    coordinator health (consecutive failures, repair-issue flag, last-update
    success/elapsed), client auth-epoch, and meter websocket connectivity
    state. Sensitive fields (password, host, MAC) are redacted via HA's
    standard redaction helper.
    """
    coordinator = entry.runtime_data.coordinator
    client = entry.runtime_data.client
    meter_client = entry.runtime_data.meter_client
    push_client = entry.runtime_data.push_client
    data = coordinator.data

    coordinator_diag: dict[str, Any] = {
        "last_update_success": coordinator.last_update_success,
        "consecutive_failures": coordinator._consecutive_failures,
        "unreachable_issue_raised": coordinator._unreachable_issue_raised,
        "update_interval_s": (
            coordinator.update_interval.total_seconds()
            if coordinator.update_interval is not None
            else None
        ),
        "crosspoint_patches_pending": len(coordinator._crosspoint_pending),
    }
    if coordinator.last_exception is not None:
        coordinator_diag["last_exception"] = repr(coordinator.last_exception)

    client_diag = {
        "auth_epoch": getattr(client, "_auth_epoch", None),
        "authenticated": getattr(client, "_authenticated", None),
    }

    meter_diag: dict[str, Any] = {
        "connected": meter_client.connected if meter_client is not None else None,
        "latest_records_count": (
            len(meter_client.latest_records) if meter_client is not None else 0
        ),
    }

    push_diag: dict[str, Any] = _build_push_diag(coordinator, push_client)

    return {
        "config_entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "config_entry_options": dict(entry.options),
        "coordinator": coordinator_diag,
        "client": client_diag,
        "meter": meter_diag,
        "push": async_redact_data(push_diag, TO_REDACT),
        "system_info": async_redact_data(asdict(data.system_info), TO_REDACT),
        "front_panel": asdict(data.front_panel),
        "power_on": data.power_on,
        "channels": {cid: asdict(c) for cid, c in data.channels.items()},
        "chains": {cid: asdict(s) for cid, s in data.chains.items()},
        "dvca": {str(idx): asdict(s) for idx, s in data.dvca.items()},
        "crosspoints": {f"{m}.{i}": asdict(s) for (m, i), s in data.crosspoints.items()},
        "presets": [asdict(p) for p in data.presets],
        "phantom_power": {str(k): v for k, v in data.phantom_power.items()},
        "mic_preamp_gain": {str(k): v for k, v in data.mic_preamp_gain.items()},
        "gpo": {str(k): v for k, v in data.gpo.items()},
        "last_recalled_preset": asdict(data.last_recalled_preset),
    }


def _build_push_diag(
    coordinator: AshlyCoordinator, push_client: AshlyPushClient | None
) -> dict[str, Any]:
    """Construct the ``push`` diagnostics sub-block.

    Returns a fully-populated dict even when ``push_client`` is None so
    bug reports always carry the connectivity shape, signalling
    "push isn't running" rather than producing a confusing absent key.
    The dict is fed through :func:`async_redact_data` by the caller so
    ``session_id`` lands as ``**REDACTED**`` in the bundle.
    """
    if push_client is None:
        return {
            "connected": None,
            "session_id": None,
            "last_event_at": None,
            "seconds_since_last_event": None,
            "subscribed_topics": [],
            "events_received": 0,
            "events_received_by_kind": {},
            "last_error": None,
        }
    last_event_at: float | None = push_client.last_event_at
    seconds_since: float | None = None
    if last_event_at is not None:
        seconds_since = coordinator._now() - last_event_at
    stats = push_client.stats
    return {
        "connected": push_client.connected,
        "session_id": push_client.session_id,
        "last_event_at": last_event_at,
        "seconds_since_last_event": seconds_since,
        "subscribed_topics": list(push_client.subscribed_topics),
        "events_received": stats.events_received,
        "events_received_by_kind": dict(stats.events_received_by_kind),
        "last_error": stats.last_error,
    }
