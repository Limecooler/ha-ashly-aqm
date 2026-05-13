"""Number entity tests."""

from __future__ import annotations

import dataclasses

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ashly.client import AshlyApiError
from custom_components.ashly.number import (
    AshlyCrosspointLevelNumber,
    AshlyDVCALevelNumber,
    AshlyMicPreampGainNumber,
)

# ── DVCA level ─────────────────────────────────────────────────────────


async def test_dvca_level_state(mock_coordinator):
    n = AshlyDVCALevelNumber(mock_coordinator, 1)
    assert n.native_value == 0.0


async def test_dvca_level_set(mock_coordinator):
    n = AshlyDVCALevelNumber(mock_coordinator, 5)
    await n.async_set_native_value(-3.5)
    mock_coordinator.client.async_set_dvca_level.assert_awaited_once_with(5, -3.5)
    pushed = mock_coordinator.async_set_updated_data.call_args[0][0]
    assert pushed.dvca[5].level_db == -3.5


async def test_dvca_level_unavailable_when_missing(mock_coordinator):
    dvca = dict(mock_coordinator.data.dvca)
    del dvca[2]
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, dvca=dvca)
    n = AshlyDVCALevelNumber(mock_coordinator, 2)
    assert n.available is False
    assert n.native_value is None


async def test_dvca_level_min_max_step(mock_coordinator):
    n = AshlyDVCALevelNumber(mock_coordinator, 1)
    assert n.native_min_value == -50.1
    assert n.native_max_value == 12.0
    assert n.native_step == 0.1
    assert n.native_unit_of_measurement == "dB"


# ── crosspoint level ───────────────────────────────────────────────────


async def test_crosspoint_level_state(mock_coordinator):
    n = AshlyCrosspointLevelNumber(mock_coordinator, 1, 1)
    assert n.native_value == 0.0


async def test_crosspoint_level_set(mock_coordinator):
    n = AshlyCrosspointLevelNumber(mock_coordinator, 4, 6)
    await n.async_set_native_value(-12.0)
    mock_coordinator.client.async_set_crosspoint_level.assert_awaited_once_with(4, 6, -12.0)
    # Crosspoint optimistic update goes through the coordinator's debouncer.
    mock_coordinator.queue_crosspoint_patch.assert_called_once_with((4, 6), level_db=-12.0)


async def test_crosspoint_level_disabled_by_default(mock_coordinator):
    n = AshlyCrosspointLevelNumber(mock_coordinator, 1, 1)
    assert n.entity_description.entity_registry_enabled_default is False


async def test_crosspoint_level_unavailable_when_missing(mock_coordinator):
    cps = dict(mock_coordinator.data.crosspoints)
    del cps[(1, 1)]
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, crosspoints=cps)
    n = AshlyCrosspointLevelNumber(mock_coordinator, 1, 1)
    assert n.available is False


# ── platform setup ─────────────────────────────────────────────────────


async def test_async_setup_entry_registers_all_numbers(hass, mock_config_entry, mock_coordinator):
    """12 DCA + 96 crosspoints + 12 mic preamp gains = 120."""
    from custom_components.ashly import number

    mock_config_entry.runtime_data = type(
        "RT",
        (),
        {"coordinator": mock_coordinator, "client": mock_coordinator.client},
    )()
    added = []
    await number.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))
    assert len(added) == 12 + 96 + 12


# ── Mic preamp gain ────────────────────────────────────────────────────


async def test_mic_preamp_state(mock_coordinator):
    n = AshlyMicPreampGainNumber(mock_coordinator, 1)
    assert n.native_value == 0.0


async def test_mic_preamp_set_snaps_to_6db_step(mock_coordinator):
    """Device only accepts 0..66 in 6dB steps. Slider rounds to nearest."""
    n = AshlyMicPreampGainNumber(mock_coordinator, 7)
    await n.async_set_native_value(25)  # rounds to 24
    mock_coordinator.client.async_set_mic_preamp.assert_awaited_once_with(7, 24)
    pushed = mock_coordinator.async_set_updated_data.call_args[0][0]
    assert pushed.mic_preamp_gain[7] == 24


async def test_mic_preamp_clamps_above_max(mock_coordinator):
    n = AshlyMicPreampGainNumber(mock_coordinator, 4)
    await n.async_set_native_value(1000)  # well above 66
    mock_coordinator.client.async_set_mic_preamp.assert_awaited_once_with(4, 66)


async def test_mic_preamp_clamps_below_min(mock_coordinator):
    n = AshlyMicPreampGainNumber(mock_coordinator, 4)
    await n.async_set_native_value(-50)
    mock_coordinator.client.async_set_mic_preamp.assert_awaited_once_with(4, 0)


async def test_mic_preamp_unavailable_when_missing(mock_coordinator):
    gains = dict(mock_coordinator.data.mic_preamp_gain)
    del gains[2]
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, mic_preamp_gain=gains)
    n = AshlyMicPreampGainNumber(mock_coordinator, 2)
    assert n.available is False


# ── Error paths ────────────────────────────────────────────────────────


async def test_dvca_level_client_error_raises(mock_coordinator):
    mock_coordinator.client.async_set_dvca_level.side_effect = AshlyApiError("err")
    n = AshlyDVCALevelNumber(mock_coordinator, 1)
    with pytest.raises(HomeAssistantError):
        await n.async_set_native_value(-3.0)


async def test_crosspoint_level_client_error_raises(mock_coordinator):
    mock_coordinator.client.async_set_crosspoint_level.side_effect = AshlyApiError("err")
    n = AshlyCrosspointLevelNumber(mock_coordinator, 1, 1)
    with pytest.raises(HomeAssistantError):
        await n.async_set_native_value(-3.0)


async def test_mic_preamp_client_error_raises(mock_coordinator):
    mock_coordinator.client.async_set_mic_preamp.side_effect = AshlyApiError("err")
    n = AshlyMicPreampGainNumber(mock_coordinator, 1)
    with pytest.raises(HomeAssistantError):
        await n.async_set_native_value(12)


# ── _push_optimistic + native_value when data=None ─────────────────────


async def test_dvca_push_optimistic_no_data_noop(mock_coordinator):
    n = AshlyDVCALevelNumber(mock_coordinator, 1)
    mock_coordinator.data = None
    n._push_optimistic(-3.0)
    mock_coordinator.async_set_updated_data.assert_not_called()


async def test_dvca_push_optimistic_missing_index_noop(mock_coordinator):
    n = AshlyDVCALevelNumber(mock_coordinator, 99)
    n._push_optimistic(-3.0)
    mock_coordinator.async_set_updated_data.assert_not_called()


async def test_dvca_native_value_no_data(mock_coordinator):
    n = AshlyDVCALevelNumber(mock_coordinator, 1)
    mock_coordinator.data = None
    assert n.native_value is None


async def test_crosspoint_level_push_optimistic_defers_to_queue(mock_coordinator):
    """Entity hands off to the coordinator's queue; no-data / missing-key
    guards moved onto the coordinator (see test_coordinator)."""
    n = AshlyCrosspointLevelNumber(mock_coordinator, 1, 1)
    n._push_optimistic(-3.0)
    mock_coordinator.queue_crosspoint_patch.assert_called_once_with((1, 1), level_db=-3.0)
    mock_coordinator.async_set_updated_data.assert_not_called()


async def test_crosspoint_native_value_no_data(mock_coordinator):
    n = AshlyCrosspointLevelNumber(mock_coordinator, 1, 1)
    mock_coordinator.data = None
    assert n.native_value is None


async def test_mic_preamp_push_optimistic_no_data_noop(mock_coordinator):
    n = AshlyMicPreampGainNumber(mock_coordinator, 1)
    mock_coordinator.data = None
    n._push_optimistic(12)
    mock_coordinator.async_set_updated_data.assert_not_called()


async def test_mic_preamp_native_value_no_data(mock_coordinator):
    n = AshlyMicPreampGainNumber(mock_coordinator, 1)
    mock_coordinator.data = None
    assert n.native_value is None
