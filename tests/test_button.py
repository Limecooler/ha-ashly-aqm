"""Tests for the button platform (identify + dynamic preset buttons)."""

from __future__ import annotations

import dataclasses

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ashly.button import (
    AshlyIdentifyButton,
    AshlyRecallPresetButton,
)
from custom_components.ashly.client import AshlyApiError, PresetInfo


async def test_identify_button_press_calls_client(mock_coordinator):
    btn = AshlyIdentifyButton(mock_coordinator)
    mock_coordinator.client.async_identify = mock_coordinator.client.async_login.__class__(
        return_value=None
    )
    await btn.async_press()
    mock_coordinator.client.async_identify.assert_awaited_once()


async def test_async_setup_entry_registers_identify_and_preset_buttons(
    hass, mock_config_entry, mock_coordinator
):
    """Setup adds the identify button plus one button per current preset."""
    from custom_components.ashly import button

    mock_config_entry.runtime_data = type(
        "RT",
        (),
        {"coordinator": mock_coordinator, "client": mock_coordinator.client},
    )()
    added = []
    await button.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))
    # 1 identify + 2 preset buttons (mock_coordinator.data.presets has Preset 1, Preset 2)
    assert len(added) == 1 + 2
    classes = {type(ent).__name__ for ent in added}
    assert classes == {"AshlyIdentifyButton", "AshlyRecallPresetButton"}


async def test_identify_button_press_client_error_raises(mock_coordinator):
    btn = AshlyIdentifyButton(mock_coordinator)
    mock_coordinator.client.async_identify = mock_coordinator.client.async_login.__class__(
        side_effect=AshlyApiError("boom")
    )
    with pytest.raises(HomeAssistantError):
        await btn.async_press()


# ── Dynamic recall-preset buttons ──────────────────────────────────────


async def test_recall_preset_button_press_calls_client(mock_coordinator):
    btn = AshlyRecallPresetButton(mock_coordinator, "Preset 1")
    mock_coordinator.client.async_recall_preset = mock_coordinator.client.async_login.__class__(
        return_value=None
    )
    await btn.async_press()
    mock_coordinator.client.async_recall_preset.assert_awaited_once_with("Preset 1")
    mock_coordinator.async_request_refresh.assert_awaited_once()


async def test_recall_preset_button_press_client_error_raises(mock_coordinator):
    btn = AshlyRecallPresetButton(mock_coordinator, "Preset 1")
    mock_coordinator.client.async_recall_preset = mock_coordinator.client.async_login.__class__(
        side_effect=AshlyApiError("boom")
    )
    with pytest.raises(HomeAssistantError):
        await btn.async_press()


async def test_recall_preset_button_available_only_when_preset_present(mock_coordinator):
    """The recall button is available only when its preset still exists on the device."""
    btn = AshlyRecallPresetButton(mock_coordinator, "Preset 1")
    assert btn.available is True
    # Drop Preset 1 from coordinator data.
    new_data = dataclasses.replace(
        mock_coordinator.data,
        presets=[PresetInfo(id="Other", name="Other")],
    )
    mock_coordinator.data = new_data
    assert btn.available is False


async def test_recall_preset_button_unavailable_when_data_none(mock_coordinator):
    btn = AshlyRecallPresetButton(mock_coordinator, "Preset 1")
    mock_coordinator.data = None
    assert btn.available is False


async def test_recall_preset_button_async_mark_removed_makes_unavailable(mock_coordinator):
    btn = AshlyRecallPresetButton(mock_coordinator, "Preset 1")
    # Stub out hass-tied write so we can call mark_removed without a real HA loop.
    btn.hass = None  # type: ignore[assignment]
    btn.async_mark_removed()
    assert btn.available is False


async def test_recall_preset_button_mark_removed_writes_state_when_hass(mock_coordinator):
    """When attached to hass, mark_removed schedules a state write."""
    from unittest.mock import MagicMock

    btn = AshlyRecallPresetButton(mock_coordinator, "Preset 1")
    btn.hass = MagicMock()  # type: ignore[assignment]
    btn.async_write_ha_state = MagicMock()
    btn.async_mark_removed()
    btn.async_write_ha_state.assert_called_once()


async def test_dynamic_preset_buttons_added_on_new_preset(
    hass, mock_config_entry, mock_coordinator
):
    """When a new preset appears, the listener adds a new button entity."""
    from custom_components.ashly import button

    mock_config_entry.runtime_data = type(
        "RT",
        (),
        {"coordinator": mock_coordinator, "client": mock_coordinator.client},
    )()
    added: list = []
    await button.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))
    initial_count = len(added)

    # Capture the listener function.
    listener = mock_coordinator.async_add_listener.call_args[0][0]

    # Add a new preset to the coordinator's data and fire the listener.
    new_data = dataclasses.replace(
        mock_coordinator.data,
        presets=[
            *mock_coordinator.data.presets,
            PresetInfo(id="Preset 3", name="Preset 3"),
        ],
    )
    mock_coordinator.data = new_data
    listener()
    assert len(added) == initial_count + 1
    new_ent = added[-1]
    assert isinstance(new_ent, AshlyRecallPresetButton)
    assert new_ent.preset_id == "Preset 3"


async def test_dynamic_preset_buttons_removed_preset_marks_unavailable(
    hass, mock_config_entry, mock_coordinator
):
    """When a preset disappears, its button is marked permanently unavailable."""
    from unittest.mock import MagicMock

    from custom_components.ashly import button

    mock_config_entry.runtime_data = type(
        "RT",
        (),
        {"coordinator": mock_coordinator, "client": mock_coordinator.client},
    )()
    added: list = []
    await button.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))

    # Find the button for Preset 1 and stub its hass-tied write.
    preset_button = next(
        ent
        for ent in added
        if isinstance(ent, AshlyRecallPresetButton) and ent.preset_id == "Preset 1"
    )
    preset_button.hass = MagicMock()  # type: ignore[assignment]
    preset_button.async_write_ha_state = MagicMock()

    # Capture listener; remove Preset 1; fire.
    listener = mock_coordinator.async_add_listener.call_args[0][0]
    new_data = dataclasses.replace(
        mock_coordinator.data,
        presets=[p for p in mock_coordinator.data.presets if p.id != "Preset 1"],
    )
    mock_coordinator.data = new_data
    listener()
    assert preset_button.available is False
    preset_button.async_write_ha_state.assert_called_once()


async def test_dynamic_preset_buttons_readd_after_removal_gets_new_entity(
    hass, mock_config_entry, mock_coordinator
):
    """If a preset id is removed and re-added, a fresh entity is created."""
    from custom_components.ashly import button

    mock_config_entry.runtime_data = type(
        "RT",
        (),
        {"coordinator": mock_coordinator, "client": mock_coordinator.client},
    )()
    added: list = []
    await button.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))
    listener = mock_coordinator.async_add_listener.call_args[0][0]

    # Remove Preset 1 (stub out the unavailable state write).
    preset_btn = next(
        e for e in added if isinstance(e, AshlyRecallPresetButton) and e.preset_id == "Preset 1"
    )
    preset_btn.hass = None  # type: ignore[assignment]
    mock_coordinator.data = dataclasses.replace(
        mock_coordinator.data,
        presets=[p for p in mock_coordinator.data.presets if p.id != "Preset 1"],
    )
    listener()

    count_after_remove = len(added)
    # Re-add Preset 1.
    mock_coordinator.data = dataclasses.replace(
        mock_coordinator.data,
        presets=[*mock_coordinator.data.presets, PresetInfo(id="Preset 1", name="Preset 1")],
    )
    listener()
    assert len(added) == count_after_remove + 1
    assert added[-1].preset_id == "Preset 1"


async def test_dynamic_preset_listener_noop_when_data_none(
    hass, mock_config_entry, mock_coordinator
):
    """The listener is a no-op when coordinator.data is None (pre-first-poll race)."""
    from custom_components.ashly import button

    mock_config_entry.runtime_data = type(
        "RT",
        (),
        {"coordinator": mock_coordinator, "client": mock_coordinator.client},
    )()
    added: list = []
    await button.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))
    listener = mock_coordinator.async_add_listener.call_args[0][0]
    initial_count = len(added)
    mock_coordinator.data = None
    listener()
    assert len(added) == initial_count
