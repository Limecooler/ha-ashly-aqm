"""Diagnostics tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ashly.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)


@pytest.fixture
async def loaded_entry(hass: HomeAssistant, mock_config_entry, mock_client, patched_session):
    with patch("custom_components.ashly.AshlyClient", return_value=mock_client):
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    yield mock_config_entry


async def test_diagnostics_redacts_sensitive(hass: HomeAssistant, loaded_entry) -> None:
    diag = await async_get_config_entry_diagnostics(hass, loaded_entry)

    flat = repr(diag)
    # Password, MAC, and host should not appear anywhere in the diagnostics.
    assert "secret" not in flat
    assert "00:14:aa:11:22:33" not in flat
    assert "192.168.1.100" not in flat
    assert diag["config_entry_data"]["password"] == "**REDACTED**"
    assert diag["config_entry_data"]["host"] == "**REDACTED**"
    assert diag["system_info"]["mac_address"] == "**REDACTED**"


async def test_diagnostics_contains_expected_top_level_keys(
    hass: HomeAssistant, loaded_entry
) -> None:
    diag = await async_get_config_entry_diagnostics(hass, loaded_entry)
    assert set(diag) == {
        "config_entry_data",
        "config_entry_options",
        "coordinator",
        "client",
        "meter",
        "system_info",
        "front_panel",
        "power_on",
        "channels",
        "chains",
        "dvca",
        "crosspoints",
        "presets",
        "phantom_power",
        "mic_preamp_gain",
        "gpo",
        "last_recalled_preset",
    }


def test_to_redact_set() -> None:
    assert {"password", "host", "mac_address"} == TO_REDACT


async def test_diagnostics_includes_coordinator_health(hass: HomeAssistant, loaded_entry) -> None:
    """Coordinator health metrics surface in diagnostics for bug reports."""
    diag = await async_get_config_entry_diagnostics(hass, loaded_entry)
    coord = diag["coordinator"]
    assert coord["last_update_success"] is True
    assert coord["consecutive_failures"] == 0
    assert coord["unreachable_issue_raised"] is False
    assert coord["update_interval_s"] == 10
    assert coord["crosspoint_patches_pending"] == 0


async def test_diagnostics_includes_coordinator_last_exception(
    hass: HomeAssistant, loaded_entry, mock_client
):
    """If the coordinator has a last_exception, it surfaces in diagnostics."""
    from custom_components.ashly.client import AshlyApiError

    coordinator = loaded_entry.runtime_data.coordinator
    coordinator.last_exception = AshlyApiError("boom-for-diagnostics")
    diag = await async_get_config_entry_diagnostics(hass, loaded_entry)
    assert "boom-for-diagnostics" in diag["coordinator"]["last_exception"]


async def test_diagnostics_includes_client_auth_epoch(hass: HomeAssistant, loaded_entry) -> None:
    diag = await async_get_config_entry_diagnostics(hass, loaded_entry)
    assert "auth_epoch" in diag["client"]
    assert "authenticated" in diag["client"]


async def test_diagnostics_meter_section_reports_state(hass: HomeAssistant, loaded_entry) -> None:
    diag = await async_get_config_entry_diagnostics(hass, loaded_entry)
    meter = diag["meter"]
    assert "connected" in meter
    assert "latest_records_count" in meter


async def test_diagnostics_with_null_meter_client(hass: HomeAssistant, loaded_entry) -> None:
    """If meter_client is None (lifecycle race), diagnostics still render."""
    loaded_entry.runtime_data.meter_client = None
    diag = await async_get_config_entry_diagnostics(hass, loaded_entry)
    assert diag["meter"]["connected"] is None
    assert diag["meter"]["latest_records_count"] == 0


async def test_diagnostics_with_no_update_interval(hass: HomeAssistant, loaded_entry) -> None:
    """If update_interval has been cleared, diagnostics still render."""
    loaded_entry.runtime_data.coordinator.update_interval = None
    diag = await async_get_config_entry_diagnostics(hass, loaded_entry)
    assert diag["coordinator"]["update_interval_s"] is None
