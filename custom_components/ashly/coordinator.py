"""DataUpdateCoordinator for the Ashly Audio integration."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    AshlyApiError,
    AshlyAuthError,
    AshlyClient,
    AshlyConnectionError,
    AshlyTimeoutError,
    ChainState,
    CrosspointState,
    DSPChannel,
    DVCAState,
    FrontPanelInfo,
    LastRecalledPreset,
    PresetInfo,
    SystemInfo,
)
from .const import (
    DEFAULT_PASSWORD,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USERNAME,
    DOMAIN,
    NUM_INPUTS,
    NUM_MIXERS,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Hard floor on the poll interval — the schema enforces this for UI input,
# this is defence-in-depth for hand-edited config storage.
_MIN_POLL_INTERVAL = 5
_MAX_POLL_INTERVAL = 3600

# Debounce window for refresh requests issued from entity write callbacks.
_REFRESH_DEBOUNCE_SECONDS = 0.3

# Consecutive coordinator update failures after which we raise the
# "device unreachable" repair issue. At the default 30s poll, 20 polls
# is ~10 minutes — long enough that transient blips don't surface as a
# user-facing repair, short enough that a real outage (or DHCP-induced
# IP change) shows up before the day's automations break.
_UNREACHABLE_REPAIR_THRESHOLD = 20

# Window over which we coalesce optimistic crosspoint patches into a single
# coordinator update. Single user actions are imperceptibly delayed (~50 ms);
# scenes or automations that flip multiple crosspoints close together
# collapse into one state-update fan-out (~280 entities x N changes -> x1).
_CROSSPOINT_PATCH_DEBOUNCE_S = 0.05


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
        # Tracks consecutive update failures for the unreachable repair issue.
        self._consecutive_failures: int = 0
        self._unreachable_issue_raised: bool = False
        # Pending optimistic patches for the crosspoint matrix; coalesced via
        # a short-window timer (see `queue_crosspoint_patch`).
        self._crosspoint_pending: dict[tuple[int, int], CrosspointState] = {}
        self._crosspoint_flush_handle: asyncio.TimerHandle | None = None

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
        self._evaluate_repair_issues()

    def _evaluate_repair_issues(self) -> None:
        """Create or clear actionable repair issues based on entry state.

        Currently flags: factory-default device credentials still in use, which
        is a security concern on a networked audio device. The issue clears
        automatically when the user reconfigures with non-default credentials.
        """
        issue_id = f"default_credentials_{self.config_entry.entry_id}"
        data = self.config_entry.data
        is_default = (
            data.get(CONF_USERNAME, DEFAULT_USERNAME) == DEFAULT_USERNAME
            and data.get(CONF_PASSWORD, DEFAULT_PASSWORD) == DEFAULT_PASSWORD
        )
        if is_default:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="default_credentials",
                translation_placeholders={"host": self.client.host},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _track_update_outcome(self, success: bool) -> None:
        """Maintain the consecutive-failure counter + unreachable repair issue.

        Called from `_async_update_data`'s success/failure paths. Once the
        coordinator has missed `_UNREACHABLE_REPAIR_THRESHOLD` polls in a
        row, a Warning-severity repair issue surfaces in Settings → Repairs
        pointing the user at the reconfigure flow (in case the device's IP
        has drifted via DHCP). Cleared on the first successful poll.
        """
        issue_id = f"device_unreachable_{self.config_entry.entry_id}"
        if success:
            self._consecutive_failures = 0
            if self._unreachable_issue_raised:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                self._unreachable_issue_raised = False
            return
        self._consecutive_failures += 1
        if (
            self._consecutive_failures >= _UNREACHABLE_REPAIR_THRESHOLD
            and not self._unreachable_issue_raised
        ):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="device_unreachable",
                translation_placeholders={"host": self.client.host},
            )
            self._unreachable_issue_raised = True

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

        Repair issues are re-evaluated each poll so a credential change on
        the device (admin/secret → custom) clears the default-credentials
        issue on the next successful poll, not just at setup.
        """
        try:
            data = await self._do_update()
        except Exception:
            self._track_update_outcome(success=False)
            raise
        self._track_update_outcome(success=True)
        self._evaluate_repair_issues()
        return data

    def _crosspoints_needed(self) -> bool:
        """True if any crosspoint entity is enabled in the registry.

        Crosspoints are 96 per AQM1208 and read every poll. They're
        disabled-by-default, so most installations don't need them. Skipping
        the HTTP call when none are enabled removes 1 of 9 endpoints from
        each poll and saves ~30 KB of JSON parsing per poll for a typical
        device. The check runs each poll, so flipping an entity on
        triggers the next poll to start fetching.

        Entity unique_id format is `<mac>_<key>` where key for crosspoints
        is either `xp_level_m<n>_i<m>` or `xp_mute_m<n>_i<m>`.
        """
        ent_reg = er.async_get(self.hass)
        for entry in er.async_entries_for_config_entry(ent_reg, self.config_entry.entry_id):
            if entry.disabled_by is not None:
                continue
            if entry.domain not in (Platform.NUMBER, Platform.SWITCH):
                continue
            if "xp_level_m" in entry.unique_id or "xp_mute_m" in entry.unique_id:
                return True
        return False

    def _default_crosspoints(self) -> dict[tuple[int, int], CrosspointState]:
        """Fallback crosspoint matrix used when the poll is skipped on the very
        first refresh (no prior data to reuse).
        """
        return {
            (m, i): CrosspointState(mixer_index=m, input_index=i, level_db=0.0, muted=True)
            for m in range(1, NUM_MIXERS + 1)
            for i in range(1, NUM_INPUTS + 1)
        }

    async def _do_update(self) -> AshlyDeviceData:
        """The actual update logic; wrapped by `_async_update_data` for tracking."""
        if self.system_info is None:
            raise UpdateFailed("System info not available; setup did not run")

        prev = self.data

        async def _crosspoints_or_reuse() -> dict[tuple[int, int], CrosspointState]:
            """Skip the (expensive) 96-entry crosspoint fetch when no
            entity that depends on it is enabled. Reuses the prior poll's
            value (or default-fills on the first poll)."""
            if self._crosspoints_needed():
                return await self.client.async_get_crosspoints()
            return prev.crosspoints if prev else self._default_crosspoints()

        results = await asyncio.gather(
            self.client.async_get_front_panel(),
            self.client.async_get_chain_state(),
            self.client.async_get_dvca_state(),
            _crosspoints_or_reuse(),
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

        # Critical endpoints: front_panel, chains, dvca, crosspoints.
        # A *full* connection error or API error on any of these tanks the
        # poll. A per-endpoint timeout (subclass of connection error) is
        # softened to "reuse last value" when we have one — a single
        # endpoint timing out while the other three return cleanly is
        # almost certainly a slow embedded CPU, not a dead device.
        for idx in range(4):
            r = results[idx]
            if isinstance(r, AshlyTimeoutError) and prev is not None:
                _LOGGER.debug(
                    "[%s] %s timed out on a single endpoint; reusing last value",
                    self.client.host,
                    _TASK_LABELS[idx],
                )
                continue
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

        def _critical(idx: int, fallback: Any) -> Any:
            """Pick a critical result or the prior value if it timed out."""
            r = results[idx]
            if isinstance(r, AshlyTimeoutError):
                return fallback
            return r

        presets = _resolve(4, prev.presets if prev else [])
        phantom_power = _resolve(5, prev.phantom_power if prev else {})
        mic_preamp = _resolve(6, prev.mic_preamp_gain if prev else {})
        gpo = _resolve(7, prev.gpo if prev else {})
        last_recalled = _resolve(
            8,
            prev.last_recalled_preset if prev else LastRecalledPreset(name=None, modified=False),
        )

        front_panel = cast(
            FrontPanelInfo,
            _critical(0, prev.front_panel if prev else None),
        )
        chains = cast(
            "dict[str, ChainState]",
            _critical(1, prev.chains if prev else None),
        )
        dvca = cast(
            "dict[int, DVCAState]",
            _critical(2, prev.dvca if prev else None),
        )
        crosspoints = cast(
            "dict[tuple[int, int], CrosspointState]",
            _critical(3, prev.crosspoints if prev else None),
        )
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

    @callback
    def queue_crosspoint_patch(
        self,
        key: tuple[int, int],
        *,
        level_db: float | None = None,
        muted: bool | None = None,
    ) -> None:
        """Queue an optimistic crosspoint patch; coalesce into one update.

        Multiple crosspoint changes within `_CROSSPOINT_PATCH_DEBOUNCE_S`
        produce a single `async_set_updated_data` call (instead of N), so
        scenes that flip many crosspoints don't fan out ~280 entity state
        reads x N times.
        """
        if self.data is None:
            return
        base = self._crosspoint_pending.get(key) or self.data.crosspoints.get(key)
        if base is None:
            return
        self._crosspoint_pending[key] = dataclasses.replace(
            base,
            level_db=base.level_db if level_db is None else level_db,
            muted=base.muted if muted is None else muted,
        )
        if self._crosspoint_flush_handle is None:
            self._crosspoint_flush_handle = self.hass.loop.call_later(
                _CROSSPOINT_PATCH_DEBOUNCE_S,
                self._flush_crosspoint_patches,
            )

    @callback
    def _flush_crosspoint_patches(self) -> None:
        """Apply all queued crosspoint patches in a single state update."""
        self._crosspoint_flush_handle = None
        if self.data is None or not self._crosspoint_pending:
            return
        crosspoints = {**self.data.crosspoints, **self._crosspoint_pending}
        self._crosspoint_pending.clear()
        self.async_set_updated_data(dataclasses.replace(self.data, crosspoints=crosspoints))
