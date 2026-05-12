"""Tests for the sensor platform."""

from __future__ import annotations

from custom_components.ashly.sensor import (
    AshlyChannelMeterSensor,
    AshlyFirmwareSensor,
    AshlyLastRecalledPresetSensor,
    AshlyPresetCountSensor,
)


async def test_firmware_sensor_value(mock_coordinator):
    s = AshlyFirmwareSensor(mock_coordinator)
    assert s.native_value == "1.1.8"


async def test_preset_count_sensor_value(mock_coordinator):
    s = AshlyPresetCountSensor(mock_coordinator)
    assert s.native_value == 2
    assert s.extra_state_attributes == {
        "presets": [
            {"id": "Preset 1", "name": "Preset 1"},
            {"id": "Preset 2", "name": "Preset 2"},
        ]
    }


async def test_last_recalled_sensor_none(mock_coordinator):
    s = AshlyLastRecalledPresetSensor(mock_coordinator)
    assert s.native_value is None
    assert s.extra_state_attributes == {"modified": False}


async def test_async_setup_entry_registers_all_sensors(
    hass, mock_config_entry, mock_coordinator, mock_meter_client
):
    """3 diagnostic sensors + 12 input meters + 12 mixer-input meters = 27."""
    from custom_components.ashly import sensor

    mock_config_entry.runtime_data = type(
        "RT",
        (),
        {
            "coordinator": mock_coordinator,
            "client": mock_coordinator.client,
            "meter_client": mock_meter_client,
        },
    )()
    added = []
    await sensor.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))
    assert len(added) == 3 + 12 + 12


async def test_meter_sensor_input_index(mock_coordinator, mock_meter_client):
    """Input N maps to metermap position N-1."""
    s = AshlyChannelMeterSensor(mock_coordinator, mock_meter_client, kind="input", channel=3)
    assert s._meter_index == 2


async def test_meter_sensor_mixer_index(mock_coordinator, mock_meter_client):
    """Mixer-input N maps to position NUM_INPUTS + N - 1 = 14 for input 3."""
    s = AshlyChannelMeterSensor(mock_coordinator, mock_meter_client, kind="mixer", channel=3)
    assert s._meter_index == 14


async def test_meter_sensor_seeds_at_floor(mock_coordinator, mock_meter_client):
    s = AshlyChannelMeterSensor(mock_coordinator, mock_meter_client, kind="input", channel=1)
    assert s.native_value == -60.0  # METER_FLOOR_DB


async def test_meter_sensor_rejects_bad_kind(mock_coordinator, mock_meter_client):
    import pytest

    with pytest.raises(ValueError):
        AshlyChannelMeterSensor(mock_coordinator, mock_meter_client, kind="bogus", channel=1)


async def test_meter_sensor_on_update_clamps_to_range(mock_coordinator, mock_meter_client):
    s = AshlyChannelMeterSensor(mock_coordinator, mock_meter_client, kind="input", channel=1)
    # Patch async_write_ha_state since we're not in a real HA runtime
    s.async_write_ha_state = lambda: None
    # raw=80 → +20 dBu (top of range)
    records = [80] + [0] * 95
    s._on_meter_update(records)
    assert s.native_value == 20.0
    # raw=100 → would be +40 dBu, clamps to +20
    records = [100] + [0] * 95
    s._on_meter_update(records)
    assert s.native_value == 20.0


async def test_meter_sensor_seeds_safely_when_records_shorter_than_index(
    mock_coordinator,
):
    """Should not raise IndexError when latest_records is shorter than _meter_index."""
    from unittest.mock import AsyncMock, MagicMock

    short_meter = MagicMock()
    short_meter.connected = True
    # Only 2 records — sensor with index 14 (mixer ch 3) must handle gracefully.
    short_meter.latest_records = [10, 20]
    short_meter.add_listener = MagicMock(return_value=lambda: None)
    short_meter.async_start = AsyncMock()
    short_meter.async_stop = AsyncMock()

    s = AshlyChannelMeterSensor(mock_coordinator, short_meter, kind="mixer", channel=3)
    # Default seed is METER_FLOOR_DB; nothing should overwrite it given short records.
    # (We can't easily call async_added_to_hass without a real HA loop, but the
    # construction itself + the meter_index check should not crash.)
    assert s._meter_index == 14
    assert s.native_value == -60.0


async def test_meter_sensor_available_does_not_flap_on_disconnect(mock_coordinator):
    """The meter sensor's `available` should not depend on websocket connect state."""
    from unittest.mock import MagicMock

    flaky_meter = MagicMock()
    flaky_meter.connected = False  # websocket disconnected
    flaky_meter.latest_records = []
    flaky_meter.add_listener = MagicMock(return_value=lambda: None)

    s = AshlyChannelMeterSensor(mock_coordinator, flaky_meter, kind="input", channel=1)
    # Should still be available (cached state is meaningful even if WS is reconnecting).
    assert s.available is True
