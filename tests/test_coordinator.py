"""Tests for AshlyCoordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.ashly.client import (
    AshlyApiError,
    AshlyAuthError,
    AshlyConnectionError,
    AshlyTimeoutError,
)
from custom_components.ashly.coordinator import (
    _UNREACHABLE_REPAIR_THRESHOLD,
    AshlyCoordinator,
)


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry) -> AshlyCoordinator:
    mock_config_entry.add_to_hass(hass)
    return AshlyCoordinator(hass, mock_client, mock_config_entry)


async def test_setup_populates_system_info_and_channels(coordinator, mock_client):
    await coordinator._async_setup()
    assert coordinator.system_info is not None
    assert coordinator.system_info.model == "AQM1208"
    assert "InputChannel.1" in coordinator.channels


async def test_setup_auth_error_raises_config_entry_auth_failed(coordinator, mock_client):
    mock_client.async_get_system_info.side_effect = AshlyAuthError("nope")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_setup()


async def test_setup_connection_error_raises_update_failed(coordinator, mock_client):
    mock_client.async_get_system_info.side_effect = AshlyConnectionError("nope")
    with pytest.raises(UpdateFailed):
        await coordinator._async_setup()


async def test_update_aggregates_all_endpoints(coordinator):
    await coordinator._async_setup()
    data = await coordinator._async_update_data()
    assert data.power_on is True
    assert "InputChannel.1" in data.chains
    assert data.dvca[1].name == "DCA 1"
    assert (1, 1) in data.crosspoints
    assert len(data.presets) == 2


async def test_update_auth_error_takes_priority(coordinator, mock_client):
    await coordinator._async_setup()
    mock_client.async_get_chain_state.side_effect = AshlyAuthError("nope")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_update_connection_error_raises_update_failed(coordinator, mock_client):
    await coordinator._async_setup()
    mock_client.async_get_chain_state.side_effect = AshlyConnectionError("nope")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_api_error_raises_update_failed(coordinator, mock_client):
    await coordinator._async_setup()
    mock_client.async_get_dvca_state.side_effect = AshlyApiError("nope")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_without_setup_raises(coordinator):
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_setup_api_error_raises_update_failed(coordinator, mock_client):
    """An AshlyApiError during _async_setup is treated as not-ready, not a
    permanent failure, so HA retries with backoff."""
    mock_client.async_get_system_info.side_effect = AshlyApiError("malformed")
    with pytest.raises(UpdateFailed):
        await coordinator._async_setup()


async def test_setup_no_mac_raises_update_failed(coordinator, mock_client):
    """Without a MAC the unique_id can't be formed — not ready."""
    from custom_components.ashly.client import SystemInfo

    mock_client.async_get_system_info.return_value = SystemInfo(
        model="AQM1208",
        name="No MAC Device",
        firmware_version="1.1.8",
        hardware_revision="1.0.0",
        mac_address="",
        has_auto_mix=True,
    )
    with pytest.raises(UpdateFailed):
        await coordinator._async_setup()


async def test_update_auth_with_concurrent_connection_does_not_escalate(coordinator, mock_client):
    """If one endpoint auth-fails but another connection-fails, treat as a
    transient outage (UpdateFailed), not a credential problem."""
    await coordinator._async_setup()
    mock_client.async_get_chain_state.side_effect = AshlyAuthError("401")
    mock_client.async_get_dvca_state.side_effect = AshlyConnectionError("nope")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_preset_connection_error_reuses_last_value(coordinator, mock_client):
    """A transient preset-endpoint failure should not tank the whole poll."""
    await coordinator._async_setup()
    # First poll succeeds; second poll loses presets.
    first = await coordinator._async_update_data()
    coordinator.async_set_updated_data(first)
    mock_client.async_get_presets.side_effect = AshlyConnectionError("flaky")
    second = await coordinator._async_update_data()
    assert second.presets == first.presets


async def test_update_preset_api_error_still_fails_loudly(coordinator, mock_client):
    """An API-level preset failure (malformed envelope) is NOT swallowed —
    that's a config bug worth surfacing."""
    await coordinator._async_setup()
    mock_client.async_get_presets.side_effect = AshlyApiError("malformed")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_setup_repair_issue_for_default_credentials(coordinator):
    """When the entry uses admin/secret, _async_setup creates a repair issue."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.ashly.const import DOMAIN

    await coordinator._async_setup()
    issue_reg = ir.async_get(coordinator.hass)
    issue_id = f"default_credentials_{coordinator.config_entry.entry_id}"
    issue = issue_reg.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.WARNING


async def test_setup_clears_repair_when_credentials_non_default(
    hass: HomeAssistant, mock_client, mock_config_entry
):
    """Non-default credentials must remove any existing repair issue."""
    from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
    from homeassistant.helpers import issue_registry as ir

    from custom_components.ashly.const import DOMAIN

    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_USERNAME: "alice", CONF_PASSWORD: "hunter2"},
    )
    issue_id = f"default_credentials_{mock_config_entry.entry_id}"
    # Pre-create the issue to verify it gets cleared.
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="default_credentials",
    )
    coordinator = AshlyCoordinator(hass, mock_client, mock_config_entry)
    await coordinator._async_setup()
    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None


async def test_invalid_poll_interval_falls_back_to_default(
    hass: HomeAssistant, mock_client, mock_config_entry
):
    """A non-integer poll_interval option falls back to DEFAULT_SCAN_INTERVAL."""
    from datetime import timedelta

    from custom_components.ashly.const import DEFAULT_SCAN_INTERVAL

    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={"poll_interval": "not-a-number"},
    )
    coord = AshlyCoordinator(hass, mock_client, mock_config_entry)
    assert coord.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)


async def test_update_critical_endpoint_generic_exception_raises_update_failed(
    coordinator, mock_client
):
    """Anything raised by a critical endpoint becomes UpdateFailed."""
    await coordinator._async_setup()
    mock_client.async_get_front_panel.side_effect = RuntimeError("unexpected")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_apply_patch_replaces_data(coordinator):
    """When data is set, apply_patch invokes async_set_updated_data."""
    from unittest.mock import MagicMock

    await coordinator._async_setup()
    await coordinator._async_update_data()
    coordinator.data = await coordinator._async_update_data()
    coordinator.async_set_updated_data = MagicMock()
    # Use a real field name from the dataclass.
    coordinator.apply_patch(presets=[])
    coordinator.async_set_updated_data.assert_called_once()
    pushed = coordinator.async_set_updated_data.call_args.args[0]
    assert pushed.presets == []


async def test_update_propagates_base_exception(coordinator, mock_client):
    """A BaseException (like CancelledError) returned by gather propagates intact."""
    await coordinator._async_setup()
    # Make one of the gathered awaitables raise CancelledError, which gather
    # captures into the results array as a non-Exception BaseException.
    import asyncio

    async def raise_cancelled():
        raise asyncio.CancelledError()

    mock_client.async_get_front_panel.side_effect = raise_cancelled
    with pytest.raises(asyncio.CancelledError):
        await coordinator._async_update_data()


async def test_apply_patch_noop_when_data_none(coordinator):
    """apply_patch must not crash when first refresh hasn't completed yet."""
    coordinator.data = None
    coordinator.apply_patch(power_on=True)  # no exception


# ── Critical-endpoint timeout-softening ─────────────────────────────────


async def test_critical_endpoint_timeout_reuses_prior_value(coordinator, mock_client):
    """A TimeoutError on a critical endpoint reuses the prior poll's value
    when one exists (instead of tanking the whole poll)."""
    await coordinator._async_setup()
    # Seed prev data with a full successful poll.
    first = await coordinator._async_update_data()
    coordinator.data = first

    # Now simulate the next poll: front_panel times out, others succeed.
    mock_client.async_get_front_panel.side_effect = AshlyTimeoutError("slow")
    data = await coordinator._async_update_data()
    # The poll succeeded with the prior front_panel value reused.
    assert data.front_panel == first.front_panel


async def test_critical_endpoint_timeout_tanks_first_poll(coordinator, mock_client):
    """If there's no prior data to fall back on, a critical timeout still tanks."""
    await coordinator._async_setup()
    mock_client.async_get_front_panel.side_effect = AshlyTimeoutError("slow")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


# ── device_unreachable repair issue lifecycle ───────────────────────────


async def test_device_unreachable_issue_raised_after_threshold(coordinator, mock_client):
    """After N consecutive failures, a repair issue surfaces; cleared on recovery."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.ashly.const import DOMAIN

    await coordinator._async_setup()
    mock_client.async_get_front_panel.side_effect = AshlyConnectionError("offline")

    issue_id = f"device_unreachable_{coordinator.config_entry.entry_id}"
    issue_reg = ir.async_get(coordinator.hass)
    # Up to (but not at) the threshold: no issue yet.
    for _ in range(_UNREACHABLE_REPAIR_THRESHOLD - 1):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None

    # At the threshold: issue appears.
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None
    # And it doesn't double-create if we keep failing.
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None

    # Recover: issue clears.
    mock_client.async_get_front_panel.side_effect = None
    await coordinator._async_update_data()
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None


# ── crosspoint skip-when-disabled ──────────────────────────────────────


async def test_crosspoints_polled_when_an_entity_is_enabled(
    hass: HomeAssistant, mock_client, mock_config_entry
):
    """When at least one crosspoint entity is enabled, the poll fetches them."""
    from homeassistant.helpers import entity_registry as er

    mock_config_entry.add_to_hass(hass)
    coord = AshlyCoordinator(hass, mock_client, mock_config_entry)
    await coord._async_setup()
    # Add an enabled crosspoint entity to the registry.
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        domain="switch",
        platform="ashly",
        unique_id="aa-bb-cc-dd-ee-ff_xp_mute_m1_i1",
        config_entry=mock_config_entry,
    )
    mock_client.async_get_crosspoints.reset_mock()
    await coord._async_update_data()
    mock_client.async_get_crosspoints.assert_awaited()


async def test_crosspoints_skipped_when_no_entities_enabled(
    hass: HomeAssistant, mock_client, mock_config_entry
):
    """With no enabled crosspoint entity, the (expensive 96-entry) fetch is skipped."""
    mock_config_entry.add_to_hass(hass)
    coord = AshlyCoordinator(hass, mock_client, mock_config_entry)
    await coord._async_setup()
    mock_client.async_get_crosspoints.reset_mock()
    await coord._async_update_data()
    mock_client.async_get_crosspoints.assert_not_awaited()


async def test_crosspoints_skipped_first_poll_uses_default_matrix(
    hass: HomeAssistant, mock_client, mock_config_entry
):
    """On the very first poll with no prior data and no enabled crosspoint
    entities, the skip path returns a default-filled matrix (all muted)."""
    mock_config_entry.add_to_hass(hass)
    coord = AshlyCoordinator(hass, mock_client, mock_config_entry)
    await coord._async_setup()
    data = await coord._async_update_data()
    # All 96 default crosspoints, muted=True.
    assert len(data.crosspoints) == 8 * 12
    assert all(cp.muted for cp in data.crosspoints.values())


async def test_crosspoints_skipped_when_only_disabled_entity_present(
    hass: HomeAssistant, mock_client, mock_config_entry
):
    """A registered-but-disabled crosspoint entity does NOT force the fetch."""
    from homeassistant.helpers import entity_registry as er

    mock_config_entry.add_to_hass(hass)
    coord = AshlyCoordinator(hass, mock_client, mock_config_entry)
    await coord._async_setup()
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        domain="switch",
        platform="ashly",
        unique_id="aa-bb-cc-dd-ee-ff_xp_mute_m2_i2",
        config_entry=mock_config_entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    # Add a non-crosspoint enabled entity to confirm the per-entity loop
    # doesn't false-positive on non-crosspoint keys.
    ent_reg.async_get_or_create(
        domain="switch",
        platform="ashly",
        unique_id="aa-bb-cc-dd-ee-ff_power",
        config_entry=mock_config_entry,
    )
    # And an enabled entity from another domain to exercise the domain filter.
    ent_reg.async_get_or_create(
        domain="sensor",
        platform="ashly",
        unique_id="aa-bb-cc-dd-ee-ff_firmware_version",
        config_entry=mock_config_entry,
    )
    mock_client.async_get_crosspoints.reset_mock()
    await coord._async_update_data()
    mock_client.async_get_crosspoints.assert_not_awaited()


# ── queue_crosspoint_patch behavior ────────────────────────────────────


async def test_queue_crosspoint_patch_batches_within_window(
    hass: HomeAssistant, mock_client, mock_config_entry
):
    """Multiple queued patches within the debounce window collapse into one update."""
    import dataclasses as _dc

    mock_config_entry.add_to_hass(hass)
    coord = AshlyCoordinator(hass, mock_client, mock_config_entry)
    await coord._async_setup()
    coord.data = await coord._async_update_data()

    updates: list = []
    coord.async_set_updated_data = lambda d: updates.append(_dc.replace(d))

    # Queue three patches quickly.
    coord.queue_crosspoint_patch((1, 1), muted=False)
    coord.queue_crosspoint_patch((1, 2), level_db=-3.0)
    coord.queue_crosspoint_patch((1, 1), level_db=-6.0)  # second patch to same key
    # Nothing fired yet.
    assert updates == []
    # Wait for the flush.
    import asyncio

    await asyncio.sleep(0.1)
    # One coalesced update.
    assert len(updates) == 1
    pushed = updates[0]
    assert pushed.crosspoints[(1, 1)].muted is False
    assert pushed.crosspoints[(1, 1)].level_db == -6.0
    assert pushed.crosspoints[(1, 2)].level_db == -3.0


async def test_queue_crosspoint_patch_noop_when_no_data(
    hass: HomeAssistant, mock_client, mock_config_entry
):
    mock_config_entry.add_to_hass(hass)
    coord = AshlyCoordinator(hass, mock_client, mock_config_entry)
    # data is None — no _async_update_data has run.
    coord.queue_crosspoint_patch((1, 1), muted=False)
    assert coord._crosspoint_pending == {}
    assert coord._crosspoint_flush_handle is None


async def test_queue_crosspoint_patch_noop_when_key_missing(
    hass: HomeAssistant, mock_client, mock_config_entry
):
    """A patch for a key that doesn't exist in the current matrix is ignored."""
    mock_config_entry.add_to_hass(hass)
    coord = AshlyCoordinator(hass, mock_client, mock_config_entry)
    await coord._async_setup()
    coord.data = await coord._async_update_data()
    # Mutate prev to drop the (99, 99) entry — same effect as a sparse matrix.
    import dataclasses as _dc

    cps = dict(coord.data.crosspoints)
    coord.data = _dc.replace(coord.data, crosspoints=cps)
    coord.queue_crosspoint_patch((99, 99), muted=False)
    assert (99, 99) not in coord._crosspoint_pending


async def test_flush_crosspoint_patches_noop_when_empty(
    hass: HomeAssistant, mock_client, mock_config_entry
):
    """Flushing with nothing pending is a no-op even if data is set."""
    import dataclasses as _dc

    mock_config_entry.add_to_hass(hass)
    coord = AshlyCoordinator(hass, mock_client, mock_config_entry)
    await coord._async_setup()
    coord.data = await coord._async_update_data()
    called: list = []
    coord.async_set_updated_data = lambda d: called.append(_dc.replace(d))
    coord._flush_crosspoint_patches()
    assert called == []


async def test_flush_crosspoint_patches_noop_when_data_none(
    hass: HomeAssistant, mock_client, mock_config_entry
):
    """If self.data became None between queue and flush, flush is a no-op."""
    mock_config_entry.add_to_hass(hass)
    coord = AshlyCoordinator(hass, mock_client, mock_config_entry)
    await coord._async_setup()
    coord.data = await coord._async_update_data()
    coord.queue_crosspoint_patch((1, 1), muted=False)
    # Cancel the scheduled flush so HA's test fixture doesn't flag a lingering
    # timer; then exercise the no-op path explicitly.
    if coord._crosspoint_flush_handle is not None:
        coord._crosspoint_flush_handle.cancel()
        coord._crosspoint_flush_handle = None
    coord.data = None  # forcibly drop before the timer fires
    coord._flush_crosspoint_patches()  # must not crash


async def test_repair_default_credentials_revaluated_per_poll(
    hass: HomeAssistant, mock_client, mock_config_entry
):
    """The default-credentials issue clears on a successful poll if creds were
    updated on the device side."""
    from homeassistant.const import CONF_PASSWORD
    from homeassistant.helpers import issue_registry as ir

    from custom_components.ashly.const import DOMAIN

    mock_config_entry.add_to_hass(hass)
    coord = AshlyCoordinator(hass, mock_client, mock_config_entry)
    await coord._async_setup()  # issue is raised (defaults)
    issue_id = f"default_credentials_{mock_config_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    # User updates credentials in HA via reconfigure flow:
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_PASSWORD: "nondefault"},
    )
    # On the next poll, the per-poll re-evaluation clears the issue.
    await coord._async_update_data()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
