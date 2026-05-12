"""DataUpdateCoordinator for the Ashly Audio integration."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    AshlyApiError,
    AshlyAuthError,
    AshlyClient,
    AshlyConnectionError,
    ChainState,
    CrosspointState,
    DSPChannel,
    DVCAState,
    FrontPanelInfo,
    LastRecalledPreset,
    PresetInfo,
    SystemInfo,
)
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Hard floor on the poll interval — the schema enforces this for UI input,
# this is defence-in-depth for hand-edited config storage.
_MIN_POLL_INTERVAL = 5
_MAX_POLL_INTERVAL = 3600

# Debounce window for refresh requests issued from entity write callbacks.
_REFRESH_DEBOUNCE_SECONDS = 0.3


@dataclass(slots=True)
class AshlyDeviceData:
    """Polled state from one Ashly AQM device."""

    system_info: SystemInfo
    front_panel: FrontPanelInfo
    channels: dict[str, DSPChannel]
    chains: dict[str, ChainState]
    dvca: dict[int, DVCAState]
    crosspoints: dict[tuple[int, int], CrosspointState]
    presets: list[PresetInfo]
    phantom_power: dict[int, bool]
    mic_preamp_gain: dict[int, int]
    gpo: dict[int, bool]
    last_recalled_preset: LastRecalledPreset

    @property
    def power_on(self) -> bool:
        """Convenience: front-panel power state."""
        return self.front_panel.power_on


@dataclass(slots=True)
class AshlyData:
    """Runtime data attached to the config entry."""

    client: AshlyClient
    coordinator: AshlyCoordinator
    meter_client: Any = None  # AshlyMeterClient; typed as Any to avoid a cycle


type AshlyConfigEntry = ConfigEntry[AshlyData]


# Endpoint label per gather index, used to enrich UpdateFailed messages so a
# single 503 in HA logs identifies which call broke the poll.
_TASK_LABELS = (
    "front_panel",
    "chains",
    "dvca",
    "crosspoints",
    "presets",
    "phantom_power",
    "mic_preamp",
    "gpo",
    "last_recalled",
)


class AshlyCoordinator(DataUpdateCoordinator[AshlyDeviceData]):
    """Coordinator that polls one Ashly AQM device on a fixed interval."""

    config_entry: AshlyConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: AshlyClient,
        config_entry: AshlyConfigEntry,
    ) -> None:
        raw_interval = config_entry.options.get("poll_interval", DEFAULT_SCAN_INTERVAL)
        try:
            poll_interval = max(_MIN_POLL_INTERVAL, min(_MAX_POLL_INTERVAL, int(raw_interval)))
        except (TypeError, ValueError):
            poll_interval = DEFAULT_SCAN_INTERVAL

        # Per-host coordinator name so log lines from multiple AQMs are
        # distinguishable in HA logs.
        coordinator_name = f"{DOMAIN}.{client.host}"
        super().__init__(
            hass,
            _LOGGER,
            name=coordinator_name,
            config_entry=config_entry,
            update_interval=timedelta(seconds=poll_interval),
            always_update=False,
            request_refresh_debouncer=Debouncer(
                hass,
                _LOGGER,
                cooldown=_REFRESH_DEBOUNCE_SECONDS,
                immediate=False,
            ),
        )
        self.client = client
        self.system_info: SystemInfo | None = None
        # Channel topology is fetched once at setup (it's static for the device).
        self._channels: dict[str, DSPChannel] = {}

    async def _async_setup(self) -> None:
        """Fetch one-time data: system info + channel topology."""
        try:
            system_info, channels = await asyncio.gather(
                self.client.async_get_system_info(),
                self.client.async_get_channels(),
            )
        except AshlyAuthError as err:
            raise ConfigEntryAuthFailed from err
        except AshlyConnectionError as err:
            raise UpdateFailed(f"Cannot reach device {self.client.host}: {err}") from err
        except AshlyApiError as err:
            # Treat malformed/incomplete first-time response as "not ready"
            # so HA retries the setup with backoff rather than marking the
            # entry as failed permanently.
            raise UpdateFailed(
                f"Device {self.client.host} returned an unexpected response: {err}"
            ) from err

        if not system_info.mac_address:
            # Without a MAC we can't form stable unique_ids; treat as not ready.
            raise UpdateFailed(f"Device {self.client.host} did not return a MAC address")

        self.system_info = system_info
        self._channels = {c.channel_id: c for c in channels}

    @property
    def channels(self) -> dict[str, DSPChannel]:
        """Channel topology, populated by `_async_setup`."""
        return self._channels

    async def _async_update_data(self) -> AshlyDeviceData:
        """Fetch current device state.

        Auth errors only escalate to reauth when there is no concurrent
        connection error — a rebooting device returning 401 from one
        endpoint and timing out from another should be treated as a
        transient connection failure, not a credential problem.

        The presets endpoint is treated as best-effort: a transient
        connection failure there reuses the last known list rather than
        tanking the whole poll. Other endpoints remain critical.
        """
        if self.system_info is None:
            raise UpdateFailed("System info not available; setup did not run")

        results = await asyncio.gather(
            self.client.async_get_front_panel(),
            self.client.async_get_chain_state(),
            self.client.async_get_dvca_state(),
            self.client.async_get_crosspoints(),
            self.client.async_get_presets(),
            self.client.async_get_phantom_power(),
            self.client.async_get_mic_preamp(),
            self.client.async_get_gpo(),
            self.client.async_get_last_recalled_preset(),
            return_exceptions=True,
        )

        # Re-raise non-Exception BaseExceptions (e.g. CancelledError) intact.
        for r in results:
            if isinstance(r, BaseException) and not isinstance(r, Exception):
                raise r

        # Auth-error escalation, but only when nothing else looks like a
        # transient outage. A device that's rebooting can drop some
        # endpoints to 401 and others to TimeoutError — we don't want HA
        # to invalidate credentials in that case.
        has_connection_error = any(isinstance(r, AshlyConnectionError) for r in results)
        if not has_connection_error:
            for idx, r in enumerate(results):
                if isinstance(r, AshlyAuthError):
                    _LOGGER.warning(
                        "[%s] Auth failed during %s — initiating reauth",
                        self.client.host,
                        _TASK_LABELS[idx],
                    )
                    raise ConfigEntryAuthFailed from r

        # Critical endpoints: front_panel, chains, dvca, crosspoints. A
        # failure of any of these tanks the poll.
        for idx in range(4):
            r = results[idx]
            if isinstance(r, (AshlyAuthError, AshlyConnectionError, AshlyApiError)):
                raise UpdateFailed(
                    f"[{self.client.host}] {_TASK_LABELS[idx]} update failed: {r}"
                ) from r
            if isinstance(r, Exception):
                raise UpdateFailed(
                    f"[{self.client.host}] {_TASK_LABELS[idx]} unexpected error: {r}"
                ) from r

        # Best-effort endpoints (slow-changing or non-critical for daily use):
        # reuse the prior value on a transient connection failure. API errors
        # still surface so a real bug isn't silently swallowed.
        prev = self.data

        def _resolve(idx: int, fallback: Any) -> Any:
            r = results[idx]
            if isinstance(r, AshlyConnectionError):
                _LOGGER.debug(
                    "[%s] %s poll failed transiently; reusing last value",
                    self.client.host,
                    _TASK_LABELS[idx],
                )
                return fallback
            if isinstance(r, Exception):
                raise UpdateFailed(
                    f"[{self.client.host}] {_TASK_LABELS[idx]} update failed: {r}"
                ) from r
            return r

        presets = _resolve(4, prev.presets if prev else [])
        phantom_power = _resolve(5, prev.phantom_power if prev else {})
        mic_preamp = _resolve(6, prev.mic_preamp_gain if prev else {})
        gpo = _resolve(7, prev.gpo if prev else {})
        last_recalled = _resolve(
            8,
            prev.last_recalled_preset if prev else LastRecalledPreset(name=None, modified=False),
        )

        # Critical results were checked for exceptions above; narrow types for mypy.
        front_panel = cast(FrontPanelInfo, results[0])
        chains = cast("dict[str, ChainState]", results[1])
        dvca = cast("dict[int, DVCAState]", results[2])
        crosspoints = cast("dict[tuple[int, int], CrosspointState]", results[3])
        return AshlyDeviceData(
            system_info=self.system_info,
            front_panel=front_panel,
            channels=self._channels,
            chains=chains,
            dvca=dvca,
            crosspoints=crosspoints,
            presets=presets,
            phantom_power=phantom_power,
            mic_preamp_gain=mic_preamp,
            gpo=gpo,
            last_recalled_preset=last_recalled,
        )

    @callback
    def apply_patch(self, **fields: Any) -> None:
        """Apply an optimistic field patch to `self.data` and notify listeners.

        No-op if the coordinator hasn't received its first poll yet — that
        avoids `dataclasses.replace(None, ...)` crashes when an entity
        action races the first refresh.
        """
        if self.data is None:
            return
        self.async_set_updated_data(dataclasses.replace(self.data, **fields))
