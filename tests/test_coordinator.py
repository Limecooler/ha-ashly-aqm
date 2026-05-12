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
)
from custom_components.ashly.coordinator import AshlyCoordinator


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

    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_USERNAME: "alice", CONF_PASSWORD: "hunter2"},
    )
    mock_config_entry.add_to_hass(hass)
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

    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={"poll_interval": "not-a-number"},
    )
    mock_config_entry.add_to_hass(hass)
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


async def test_apply_patch_noop_when_data_none(coordinator):
    """apply_patch must not crash when first refresh hasn't completed yet."""
    coordinator.data = None
    coordinator.apply_patch(power_on=True)  # no exception
