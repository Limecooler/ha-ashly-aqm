"""Switch entity tests."""

from __future__ import annotations

import dataclasses

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ashly.client import AshlyApiError
from custom_components.ashly.switch import (
    AshlyChainMuteSwitch,
    AshlyCrosspointMuteSwitch,
    AshlyDVCAMuteSwitch,
    AshlyFrontPanelLEDSwitch,
    AshlyGPOSwitch,
    AshlyPhantomPowerSwitch,
    AshlyPowerSwitch,
)

# ── power ───────────────────────────────────────────────────────────────


async def test_power_switch_state(mock_coordinator) -> None:
    sw = AshlyPowerSwitch(mock_coordinator)
    assert sw.is_on is True


async def test_power_switch_turn_on_calls_client_and_pushes_optimistic(mock_coordinator):
    # Start with power off (patch through front_panel since power_on is a property)
    data = mock_coordinator.data
    mock_coordinator.data = dataclasses.replace(
        data,
        front_panel=dataclasses.replace(data.front_panel, power_on=False),
    )
    sw = AshlyPowerSwitch(mock_coordinator)
    await sw.async_turn_on()
    mock_coordinator.client.async_set_power.assert_awaited_once_with(True)
    args = mock_coordinator.async_set_updated_data.call_args[0][0]
    assert args.power_on is True


async def test_power_switch_turn_off(mock_coordinator):
    sw = AshlyPowerSwitch(mock_coordinator)
    await sw.async_turn_off()
    mock_coordinator.client.async_set_power.assert_awaited_once_with(False)


# ── chain mute ─────────────────────────────────────────────────────────


async def test_chain_mute_reads_state(mock_coordinator):
    sw = AshlyChainMuteSwitch(mock_coordinator, "InputChannel.1")
    assert sw.is_on is False


async def test_chain_mute_unavailable_when_chain_missing(mock_coordinator):
    chains = dict(mock_coordinator.data.chains)
    del chains["InputChannel.1"]
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, chains=chains)
    sw = AshlyChainMuteSwitch(mock_coordinator, "InputChannel.1")
    assert sw.available is False


async def test_chain_mute_turn_on_calls_client(mock_coordinator):
    sw = AshlyChainMuteSwitch(mock_coordinator, "OutputChannel.2")
    await sw.async_turn_on()
    mock_coordinator.client.async_set_chain_mute.assert_awaited_once_with("OutputChannel.2", True)
    pushed = mock_coordinator.async_set_updated_data.call_args[0][0]
    assert pushed.chains["OutputChannel.2"].muted is True


async def test_chain_mute_turn_off(mock_coordinator):
    chains = dict(mock_coordinator.data.chains)
    chains["InputChannel.3"] = dataclasses.replace(chains["InputChannel.3"], muted=True)
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, chains=chains)
    sw = AshlyChainMuteSwitch(mock_coordinator, "InputChannel.3")
    assert sw.is_on is True
    await sw.async_turn_off()
    mock_coordinator.client.async_set_chain_mute.assert_awaited_once_with("InputChannel.3", False)


# ── DVCA mute ──────────────────────────────────────────────────────────


async def test_dvca_mute_state(mock_coordinator):
    sw = AshlyDVCAMuteSwitch(mock_coordinator, 1)
    assert sw.is_on is False


async def test_dvca_mute_turn_on(mock_coordinator):
    sw = AshlyDVCAMuteSwitch(mock_coordinator, 4)
    await sw.async_turn_on()
    mock_coordinator.client.async_set_dvca_mute.assert_awaited_once_with(4, True)
    pushed = mock_coordinator.async_set_updated_data.call_args[0][0]
    assert pushed.dvca[4].muted is True


async def test_dvca_mute_unavailable_when_missing(mock_coordinator):
    dvca = dict(mock_coordinator.data.dvca)
    del dvca[1]
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, dvca=dvca)
    sw = AshlyDVCAMuteSwitch(mock_coordinator, 1)
    assert sw.available is False


# ── crosspoint mute ────────────────────────────────────────────────────


async def test_crosspoint_mute_state(mock_coordinator):
    sw = AshlyCrosspointMuteSwitch(mock_coordinator, 1, 1)
    # default fixture has muted=True
    assert sw.is_on is True


async def test_crosspoint_mute_disabled_by_default(mock_coordinator):
    sw = AshlyCrosspointMuteSwitch(mock_coordinator, 1, 1)
    assert sw.entity_description.entity_registry_enabled_default is False


async def test_crosspoint_mute_turn_off(mock_coordinator):
    sw = AshlyCrosspointMuteSwitch(mock_coordinator, 3, 7)
    await sw.async_turn_off()
    mock_coordinator.client.async_set_crosspoint_mute.assert_awaited_once_with(3, 7, False)
    # Optimistic update routes through the coordinator's debouncer rather
    # than firing async_set_updated_data directly (so bulk crosspoint
    # changes coalesce).
    mock_coordinator.queue_crosspoint_patch.assert_called_once_with((3, 7), muted=False)


# ── platform setup ─────────────────────────────────────────────────────


async def test_async_setup_entry_registers_all_switches(hass, mock_config_entry, mock_coordinator):
    """power(1) + 12 in + 8 out chain mutes + 12 dvca mutes + 96 xp mutes
    + front-panel LED(1) + 12 phantom power + 2 GPO = 144."""
    from custom_components.ashly import switch

    mock_config_entry.runtime_data = type(
        "RT",
        (),
        {"coordinator": mock_coordinator, "client": mock_coordinator.client},
    )()
    added = []
    await switch.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))
    assert len(added) == 1 + 12 + 8 + 12 + 8 * 12 + 1 + 12 + 2


# ── Front-panel LED switch ─────────────────────────────────────────────


async def test_front_panel_led_state(mock_coordinator):
    sw = AshlyFrontPanelLEDSwitch(mock_coordinator)
    assert sw.is_on is True


async def test_front_panel_led_turn_off(mock_coordinator):
    sw = AshlyFrontPanelLEDSwitch(mock_coordinator)
    await sw.async_turn_off()
    mock_coordinator.client.async_set_front_panel_leds.assert_awaited_once_with(False)
    pushed = mock_coordinator.async_set_updated_data.call_args[0][0]
    assert pushed.front_panel.leds_enabled is False
    # Power state must be preserved by the optimistic push
    assert pushed.front_panel.power_on is True


# ── Phantom power switch ───────────────────────────────────────────────


async def test_phantom_power_state(mock_coordinator):
    sw = AshlyPhantomPowerSwitch(mock_coordinator, 1)
    assert sw.is_on is False


async def test_phantom_power_turn_on(mock_coordinator):
    sw = AshlyPhantomPowerSwitch(mock_coordinator, 3)
    await sw.async_turn_on()
    mock_coordinator.client.async_set_phantom_power.assert_awaited_once_with(3, True)
    pushed = mock_coordinator.async_set_updated_data.call_args[0][0]
    assert pushed.phantom_power[3] is True


async def test_phantom_power_unavailable_when_missing(mock_coordinator):
    pp = dict(mock_coordinator.data.phantom_power)
    del pp[1]
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, phantom_power=pp)
    sw = AshlyPhantomPowerSwitch(mock_coordinator, 1)
    assert sw.available is False


# ── GPO switch ─────────────────────────────────────────────────────────


async def test_gpo_state(mock_coordinator):
    sw = AshlyGPOSwitch(mock_coordinator, 1)
    assert sw.is_on is False


async def test_gpo_turn_on(mock_coordinator):
    sw = AshlyGPOSwitch(mock_coordinator, 2)
    await sw.async_turn_on()
    mock_coordinator.client.async_set_gpo.assert_awaited_once_with(2, True)
    pushed = mock_coordinator.async_set_updated_data.call_args[0][0]
    assert pushed.gpo[2] is True


async def test_gpo_unavailable_when_missing(mock_coordinator):
    gpo = dict(mock_coordinator.data.gpo)
    del gpo[1]
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, gpo=gpo)
    sw = AshlyGPOSwitch(mock_coordinator, 1)
    assert sw.available is False


# ── Error paths: client raises → HomeAssistantError is raised ─────────


async def test_power_turn_on_client_error_raises(mock_coordinator):
    mock_coordinator.client.async_set_power.side_effect = AshlyApiError("nope")
    sw = AshlyPowerSwitch(mock_coordinator)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()


async def test_power_turn_off_client_error_raises(mock_coordinator):
    mock_coordinator.client.async_set_power.side_effect = AshlyApiError("nope")
    sw = AshlyPowerSwitch(mock_coordinator)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_off()


async def test_chain_mute_turn_on_client_error(mock_coordinator):
    mock_coordinator.client.async_set_chain_mute.side_effect = AshlyApiError("err")
    sw = AshlyChainMuteSwitch(mock_coordinator, "InputChannel.1")
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()


async def test_chain_mute_turn_off_client_error(mock_coordinator):
    mock_coordinator.client.async_set_chain_mute.side_effect = AshlyApiError("err")
    sw = AshlyChainMuteSwitch(mock_coordinator, "InputChannel.1")
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_off()


async def test_dvca_mute_turn_on_client_error(mock_coordinator):
    mock_coordinator.client.async_set_dvca_mute.side_effect = AshlyApiError("err")
    sw = AshlyDVCAMuteSwitch(mock_coordinator, 1)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()


async def test_dvca_mute_turn_off_client_error(mock_coordinator):
    mock_coordinator.client.async_set_dvca_mute.side_effect = AshlyApiError("err")
    sw = AshlyDVCAMuteSwitch(mock_coordinator, 1)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_off()


async def test_crosspoint_mute_turn_on_client_error(mock_coordinator):
    mock_coordinator.client.async_set_crosspoint_mute.side_effect = AshlyApiError("err")
    sw = AshlyCrosspointMuteSwitch(mock_coordinator, 1, 1)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()


async def test_crosspoint_mute_turn_off_client_error(mock_coordinator):
    mock_coordinator.client.async_set_crosspoint_mute.side_effect = AshlyApiError("err")
    sw = AshlyCrosspointMuteSwitch(mock_coordinator, 1, 1)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_off()


async def test_front_panel_led_turn_on_client_error(mock_coordinator):
    mock_coordinator.client.async_set_front_panel_leds.side_effect = AshlyApiError("err")
    sw = AshlyFrontPanelLEDSwitch(mock_coordinator)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()


async def test_front_panel_led_turn_off_client_error(mock_coordinator):
    mock_coordinator.client.async_set_front_panel_leds.side_effect = AshlyApiError("err")
    sw = AshlyFrontPanelLEDSwitch(mock_coordinator)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_off()


async def test_phantom_power_turn_on_client_error(mock_coordinator):
    mock_coordinator.client.async_set_phantom_power.side_effect = AshlyApiError("err")
    sw = AshlyPhantomPowerSwitch(mock_coordinator, 1)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()


async def test_phantom_power_turn_off_client_error(mock_coordinator):
    mock_coordinator.client.async_set_phantom_power.side_effect = AshlyApiError("err")
    sw = AshlyPhantomPowerSwitch(mock_coordinator, 1)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_off()


async def test_gpo_turn_on_client_error(mock_coordinator):
    mock_coordinator.client.async_set_gpo.side_effect = AshlyApiError("err")
    sw = AshlyGPOSwitch(mock_coordinator, 1)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()


async def test_gpo_turn_off_client_error(mock_coordinator):
    mock_coordinator.client.async_set_gpo.side_effect = AshlyApiError("err")
    sw = AshlyGPOSwitch(mock_coordinator, 1)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_off()


# ── _push_optimistic when coordinator.data is None — must no-op ───────


async def test_power_push_optimistic_no_data_is_noop(mock_coordinator):
    sw = AshlyPowerSwitch(mock_coordinator)
    mock_coordinator.data = None
    sw._push_optimistic(True)  # should not crash
    mock_coordinator.async_set_updated_data.assert_not_called()


async def test_chain_mute_push_optimistic_no_data_is_noop(mock_coordinator):
    sw = AshlyChainMuteSwitch(mock_coordinator, "InputChannel.1")
    mock_coordinator.data = None
    sw._push_optimistic(True)
    mock_coordinator.async_set_updated_data.assert_not_called()


async def test_chain_mute_push_optimistic_missing_chain_is_noop(mock_coordinator):
    sw = AshlyChainMuteSwitch(mock_coordinator, "InputChannel.99")
    sw._push_optimistic(True)
    mock_coordinator.async_set_updated_data.assert_not_called()


async def test_dvca_push_optimistic_no_data_is_noop(mock_coordinator):
    sw = AshlyDVCAMuteSwitch(mock_coordinator, 1)
    mock_coordinator.data = None
    sw._push_optimistic(True)
    mock_coordinator.async_set_updated_data.assert_not_called()


async def test_dvca_push_optimistic_missing_index_is_noop(mock_coordinator):
    sw = AshlyDVCAMuteSwitch(mock_coordinator, 99)
    sw._push_optimistic(True)
    mock_coordinator.async_set_updated_data.assert_not_called()


async def test_crosspoint_push_optimistic_defers_to_queue(mock_coordinator):
    """The entity hands off to queue_crosspoint_patch unconditionally; the
    'no data / missing key' guard lives on the coordinator instead."""
    sw = AshlyCrosspointMuteSwitch(mock_coordinator, 1, 1)
    sw._push_optimistic(True)
    mock_coordinator.queue_crosspoint_patch.assert_called_once_with((1, 1), muted=True)
    mock_coordinator.async_set_updated_data.assert_not_called()


async def test_front_panel_push_optimistic_no_data_is_noop(mock_coordinator):
    sw = AshlyFrontPanelLEDSwitch(mock_coordinator)
    mock_coordinator.data = None
    sw._push_optimistic(True)
    mock_coordinator.async_set_updated_data.assert_not_called()


async def test_phantom_power_push_optimistic_no_data_is_noop(mock_coordinator):
    sw = AshlyPhantomPowerSwitch(mock_coordinator, 1)
    mock_coordinator.data = None
    sw._push_optimistic(True)
    mock_coordinator.async_set_updated_data.assert_not_called()


async def test_gpo_push_optimistic_no_data_is_noop(mock_coordinator):
    sw = AshlyGPOSwitch(mock_coordinator, 1)
    mock_coordinator.data = None
    sw._push_optimistic(True)
    mock_coordinator.async_set_updated_data.assert_not_called()


# ── is_on / available data=None branches ───────────────────────────────


async def test_chain_mute_is_on_no_data(mock_coordinator):
    sw = AshlyChainMuteSwitch(mock_coordinator, "InputChannel.1")
    mock_coordinator.data = None
    assert sw.is_on is False


async def test_dvca_is_on_no_data(mock_coordinator):
    sw = AshlyDVCAMuteSwitch(mock_coordinator, 1)
    mock_coordinator.data = None
    assert sw.is_on is False


async def test_crosspoint_is_on_no_data(mock_coordinator):
    sw = AshlyCrosspointMuteSwitch(mock_coordinator, 1, 1)
    mock_coordinator.data = None
    assert sw.is_on is False


async def test_front_panel_is_on_no_data(mock_coordinator):
    sw = AshlyFrontPanelLEDSwitch(mock_coordinator)
    mock_coordinator.data = None
    assert sw.is_on is False


async def test_phantom_power_is_on_no_data(mock_coordinator):
    sw = AshlyPhantomPowerSwitch(mock_coordinator, 1)
    mock_coordinator.data = None
    assert sw.is_on is False


async def test_gpo_is_on_no_data(mock_coordinator):
    sw = AshlyGPOSwitch(mock_coordinator, 1)
    mock_coordinator.data = None
    assert sw.is_on is False


# ── Happy-path turn-on/turn-off for entities missing one direction ────


async def test_dvca_mute_turn_off_happy_path(mock_coordinator):
    """DVCA turn_off when not currently muted — exercises post-await optimistic line."""
    dvca = dict(mock_coordinator.data.dvca)
    dvca[2] = dataclasses.replace(dvca[2], muted=True)
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, dvca=dvca)
    sw = AshlyDVCAMuteSwitch(mock_coordinator, 2)
    await sw.async_turn_off()
    mock_coordinator.client.async_set_dvca_mute.assert_awaited_once_with(2, False)
    pushed = mock_coordinator.async_set_updated_data.call_args[0][0]
    assert pushed.dvca[2].muted is False


async def test_crosspoint_mute_available_branch(mock_coordinator):
    """Crosspoint mute switch reports available=True when both data and key exist."""
    sw = AshlyCrosspointMuteSwitch(mock_coordinator, 1, 1)
    assert sw.available is True


async def test_crosspoint_mute_unavailable_when_missing(mock_coordinator):
    """Crosspoint mute switch reports unavailable when the key is missing."""
    cps = dict(mock_coordinator.data.crosspoints)
    del cps[(1, 1)]
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, crosspoints=cps)
    sw = AshlyCrosspointMuteSwitch(mock_coordinator, 1, 1)
    assert sw.available is False


async def test_crosspoint_mute_turn_on_happy_path(mock_coordinator):
    """Crosspoint mute turn_on issues a client call and queues an optimistic patch."""
    sw = AshlyCrosspointMuteSwitch(mock_coordinator, 2, 4)
    await sw.async_turn_on()
    mock_coordinator.client.async_set_crosspoint_mute.assert_awaited_once_with(2, 4, True)
    mock_coordinator.queue_crosspoint_patch.assert_called_once_with((2, 4), muted=True)


async def test_front_panel_led_turn_on_happy_path(mock_coordinator):
    """Front-panel LED turn_on issues the client call and pushes optimistic update."""
    # Default fixture has leds_enabled=True; flip to False so turn_on is meaningful.
    fp = dataclasses.replace(mock_coordinator.data.front_panel, leds_enabled=False)
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, front_panel=fp)
    sw = AshlyFrontPanelLEDSwitch(mock_coordinator)
    await sw.async_turn_on()
    mock_coordinator.client.async_set_front_panel_leds.assert_awaited_once_with(True)
    pushed = mock_coordinator.async_set_updated_data.call_args[0][0]
    assert pushed.front_panel.leds_enabled is True


async def test_phantom_power_turn_off_happy_path(mock_coordinator):
    """Phantom-power turn_off pushes False to the device and to the optimistic state."""
    pp = dict(mock_coordinator.data.phantom_power)
    pp[1] = True
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, phantom_power=pp)
    sw = AshlyPhantomPowerSwitch(mock_coordinator, 1)
    await sw.async_turn_off()
    mock_coordinator.client.async_set_phantom_power.assert_awaited_once_with(1, False)
    pushed = mock_coordinator.async_set_updated_data.call_args[0][0]
    assert pushed.phantom_power[1] is False


async def test_gpo_turn_off_happy_path(mock_coordinator):
    """GPO turn_off issues the client call and pushes optimistic update."""
    gpo = dict(mock_coordinator.data.gpo)
    gpo[1] = True
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, gpo=gpo)
    sw = AshlyGPOSwitch(mock_coordinator, 1)
    await sw.async_turn_off()
    mock_coordinator.client.async_set_gpo.assert_awaited_once_with(1, False)
    pushed = mock_coordinator.async_set_updated_data.call_args[0][0]
    assert pushed.gpo[1] is False
