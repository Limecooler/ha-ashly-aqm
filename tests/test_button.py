"""Tests for the identify button."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ashly.button import AshlyIdentifyButton
from custom_components.ashly.client import AshlyApiError


async def test_identify_button_press_calls_client(mock_coordinator):
    btn = AshlyIdentifyButton(mock_coordinator)
    mock_coordinator.client.async_identify = mock_coordinator.client.async_login.__class__(
        return_value=None
    )
    await btn.async_press()
    mock_coordinator.client.async_identify.assert_awaited_once()


async def test_async_setup_entry_registers_one_button(hass, mock_config_entry, mock_coordinator):
    from custom_components.ashly import button

    mock_config_entry.runtime_data = type(
        "RT",
        (),
        {"coordinator": mock_coordinator, "client": mock_coordinator.client},
    )()
    added = []
    await button.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))
    assert len(added) == 1


async def test_identify_button_press_client_error_raises(mock_coordinator):
    btn = AshlyIdentifyButton(mock_coordinator)
    mock_coordinator.client.async_identify = mock_coordinator.client.async_login.__class__(
        side_effect=AshlyApiError("boom")
    )
    with pytest.raises(HomeAssistantError):
        await btn.async_press()
