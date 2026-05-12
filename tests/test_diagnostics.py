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
async def loaded_entry(
    hass: HomeAssistant, mock_config_entry, mock_client, patched_session
):
    with patch("custom_components.ashly.AshlyClient", return_value=mock_client):
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    yield mock_config_entry


async def test_diagnostics_redacts_sensitive(
    hass: HomeAssistant, loaded_entry
) -> None:
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
