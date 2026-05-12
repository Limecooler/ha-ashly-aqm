"""Select entity tests."""

from __future__ import annotations

import dataclasses

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.ashly.client import AshlyApiError
from custom_components.ashly.const import NO_MIXER
from custom_components.ashly.select import AshlyOutputMixerSelect


async def test_options_list_includes_none_and_eight_mixers(mock_coordinator):
    sel = AshlyOutputMixerSelect(mock_coordinator, 1)
    assert sel.options[0] == NO_MIXER
    assert "Mixer.1" in sel.options
    assert "Mixer.8" in sel.options
    assert len(sel.options) == 9


async def test_current_option_is_none_when_unassigned(mock_coordinator):
    sel = AshlyOutputMixerSelect(mock_coordinator, 1)
    assert sel.current_option == NO_MIXER


async def test_current_option_reflects_assigned_mixer(mock_coordinator):
    chains = dict(mock_coordinator.data.chains)
    chains["OutputChannel.3"] = dataclasses.replace(chains["OutputChannel.3"], mixer_id="Mixer.4")
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, chains=chains)
    sel = AshlyOutputMixerSelect(mock_coordinator, 3)
    assert sel.current_option == "Mixer.4"


async def test_select_option_calls_client_and_pushes_optimistic(mock_coordinator):
    sel = AshlyOutputMixerSelect(mock_coordinator, 2)
    await sel.async_select_option("Mixer.5")
    mock_coordinator.client.async_set_output_mixer.assert_awaited_once_with(
        "OutputChannel.2", "Mixer.5"
    )
    pushed = mock_coordinator.async_set_updated_data.call_args[0][0]
    assert pushed.chains["OutputChannel.2"].mixer_id == "Mixer.5"


async def test_select_none_clears_optimistic_mixer(mock_coordinator):
    chains = dict(mock_coordinator.data.chains)
    chains["OutputChannel.1"] = dataclasses.replace(chains["OutputChannel.1"], mixer_id="Mixer.1")
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, chains=chains)
    sel = AshlyOutputMixerSelect(mock_coordinator, 1)
    await sel.async_select_option(NO_MIXER)
    pushed = mock_coordinator.async_set_updated_data.call_args[0][0]
    assert pushed.chains["OutputChannel.1"].mixer_id is None


async def test_select_invalid_option_rejected(mock_coordinator):
    sel = AshlyOutputMixerSelect(mock_coordinator, 1)
    with pytest.raises(ServiceValidationError):
        await sel.async_select_option("Mixer.99")
    mock_coordinator.client.async_set_output_mixer.assert_not_awaited()


async def test_async_setup_entry_registers_eight_selects(hass, mock_config_entry, mock_coordinator):
    from custom_components.ashly import select

    mock_config_entry.runtime_data = type(
        "RT",
        (),
        {"coordinator": mock_coordinator, "client": mock_coordinator.client},
    )()
    added = []
    await select.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))
    assert len(added) == 8


async def test_select_option_client_error_raises(mock_coordinator):
    mock_coordinator.client.async_set_output_mixer.side_effect = AshlyApiError("err")
    sel = AshlyOutputMixerSelect(mock_coordinator, 1)
    with pytest.raises(HomeAssistantError):
        await sel.async_select_option("Mixer.1")


async def test_select_current_option_no_data(mock_coordinator):
    sel = AshlyOutputMixerSelect(mock_coordinator, 1)
    mock_coordinator.data = None
    assert sel.current_option is None


async def test_select_current_option_missing_chain(mock_coordinator):
    chains = dict(mock_coordinator.data.chains)
    del chains["OutputChannel.1"]
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, chains=chains)
    sel = AshlyOutputMixerSelect(mock_coordinator, 1)
    assert sel.current_option is None


async def test_select_push_optimistic_no_data_noop(mock_coordinator):
    sel = AshlyOutputMixerSelect(mock_coordinator, 1)
    mock_coordinator.data = None
    sel._push_optimistic("Mixer.1")
    mock_coordinator.async_set_updated_data.assert_not_called()


async def test_select_push_optimistic_missing_chain_noop(mock_coordinator):
    chains = dict(mock_coordinator.data.chains)
    del chains["OutputChannel.1"]
    mock_coordinator.data = dataclasses.replace(mock_coordinator.data, chains=chains)
    sel = AshlyOutputMixerSelect(mock_coordinator, 1)
    sel._push_optimistic("Mixer.1")
    mock_coordinator.async_set_updated_data.assert_not_called()
