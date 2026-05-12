"""Tests for the ashly.recall_preset service handler."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from custom_components.ashly.const import DOMAIN
from custom_components.ashly.services import (
    SERVICE_RECALL_PRESET,
    _resolve_preset_name,
)

# ── _resolve_preset_name (pure helper, no HA) ──────────────────────────


def test_resolve_preset_name_exact_match():
    assert _resolve_preset_name("Evening", ["Morning", "Evening"]) == "Evening"


def test_resolve_preset_name_numeric_index_one_based():
    assert _resolve_preset_name("1", ["Morning", "Evening"]) == "Morning"
    assert _resolve_preset_name("2", ["Morning", "Evening"]) == "Evening"


def test_resolve_preset_name_numeric_out_of_range():
    assert _resolve_preset_name("3", ["Morning", "Evening"]) is None
    assert _resolve_preset_name("0", ["Morning", "Evening"]) is None


def test_resolve_preset_name_unknown_string():
    assert _resolve_preset_name("Bogus", ["Morning", "Evening"]) is None


def test_resolve_preset_name_case_sensitive():
    """Device preset names are case-sensitive."""
    assert _resolve_preset_name("evening", ["Evening"]) is None


def test_resolve_preset_name_empty_list():
    assert _resolve_preset_name("Evening", []) is None
    assert _resolve_preset_name("1", []) is None


# ── service handler (integration with HA service registry) ─────────────


@pytest.fixture
async def loaded_entry(hass: HomeAssistant, mock_config_entry, mock_client, patched_session):
    """Set up the integration and return the loaded config entry."""
    with patch("custom_components.ashly.AshlyClient", return_value=mock_client):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    return mock_config_entry


async def test_recall_preset_service_registered(hass: HomeAssistant, loaded_entry):
    assert hass.services.has_service(DOMAIN, SERVICE_RECALL_PRESET)


async def test_recall_preset_service_by_exact_name(
    hass: HomeAssistant, loaded_entry, mock_client
):
    device_reg = dr.async_get(hass)
    device = next(iter(device_reg.devices.values()))
    await hass.services.async_call(
        DOMAIN,
        SERVICE_RECALL_PRESET,
        {"device_id": device.id, "preset": "Preset 1"},
        blocking=True,
    )
    mock_client.async_recall_preset.assert_awaited_once_with("Preset 1")


async def test_recall_preset_service_by_numeric_index(
    hass: HomeAssistant, loaded_entry, mock_client
):
    device_reg = dr.async_get(hass)
    device = next(iter(device_reg.devices.values()))
    await hass.services.async_call(
        DOMAIN,
        SERVICE_RECALL_PRESET,
        {"device_id": device.id, "preset": "2"},
        blocking=True,
    )
    mock_client.async_recall_preset.assert_awaited_once_with("Preset 2")


async def test_recall_preset_service_unknown_preset_raises(
    hass: HomeAssistant, loaded_entry
):
    device_reg = dr.async_get(hass)
    device = next(iter(device_reg.devices.values()))
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECALL_PRESET,
            {"device_id": device.id, "preset": "DoesNotExist"},
            blocking=True,
        )


async def test_recall_preset_service_unknown_device_raises(
    hass: HomeAssistant, loaded_entry
):
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RECALL_PRESET,
            {"device_id": "not-a-real-device-id", "preset": "Preset 1"},
            blocking=True,
        )


async def test_recall_preset_service_deregistered_on_last_unload(
    hass: HomeAssistant, mock_config_entry, mock_client, patched_session
):
    """When the last Ashly entry unloads, the service should disappear."""
    with patch("custom_components.ashly.AshlyClient", return_value=mock_client):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert hass.services.has_service(DOMAIN, SERVICE_RECALL_PRESET)
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, SERVICE_RECALL_PRESET)
