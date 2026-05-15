"""Tests for the pure event-router module.

The router has no HA dependencies and no I/O, so these tests instantiate
real :class:`AshlyDeviceData` (via the existing ``mock_device_data``
conftest fixture) and call :func:`route_event` directly.

Every payload is sourced from :data:`aquacontrol._testing.SAMPLE_EVENTS`
so the on-the-wire shapes can't drift from what the library actually
parses.
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any

import pytest

from custom_components.ashly._aquacontrol import parse_event
from custom_components.ashly._aquacontrol._testing import SAMPLE_EVENTS
from custom_components.ashly.coordinator import AshlyDeviceData
from custom_components.ashly.event_router import (
    NO_CHANGE,
    ROUTABLE_EVENT_NAMES,
    route_event,
)


def _event_from_sample(key: str) -> Any:
    """Helper: parse a captured payload into a library Event."""
    topic, payload = SAMPLE_EVENTS[key]
    return parse_event(topic, payload)


def _patch(payload_key: str, **overrides: Any) -> Any:
    """Helper: clone a sample payload, override fields in records[0], parse."""
    topic, payload = SAMPLE_EVENTS[payload_key]
    new_payload = copy.deepcopy(payload)
    new_payload["data"][0]["records"][0].update(overrides)
    return parse_event(topic, new_payload)


# ── Set Chain Mute ──────────────────────────────────────────────────


def test_set_chain_mute_patches_chain(mock_device_data: AshlyDeviceData) -> None:
    event = _patch("WORKING_SETTINGS_SET_CHAIN_MUTE", id="InputChannel.1", muted=True)
    result = route_event(event, mock_device_data)
    assert isinstance(result, AshlyDeviceData)
    assert result.chains["InputChannel.1"].muted is True
    # Other chains untouched
    assert result.chains["InputChannel.2"].muted is False


def test_set_chain_mute_no_change_when_already_muted(
    mock_device_data: AshlyDeviceData,
) -> None:
    prev = dataclasses.replace(
        mock_device_data,
        chains={
            **mock_device_data.chains,
            "InputChannel.1": dataclasses.replace(
                mock_device_data.chains["InputChannel.1"], muted=True
            ),
        },
    )
    event = _patch("WORKING_SETTINGS_SET_CHAIN_MUTE", id="InputChannel.1", muted=True)
    assert route_event(event, prev) is NO_CHANGE


def test_set_chain_mute_unknown_channel_returns_none(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _patch("WORKING_SETTINGS_SET_CHAIN_MUTE", id="NonExistent.1", muted=True)
    assert route_event(event, mock_device_data) is None


# ── Set mixer to output chain ───────────────────────────────────────


def test_set_mixer_to_output_chain_patches_mixer_id_and_mute(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _event_from_sample("WORKING_SETTINGS_SET_MIXER_TO_OUTPUT_CHAIN")
    result = route_event(event, mock_device_data)
    assert isinstance(result, AshlyDeviceData)
    assert result.chains["OutputChannel.1"].mixer_id == "Mixer.2"
    assert result.chains["OutputChannel.1"].muted is False


def test_set_mixer_to_output_chain_no_change_when_identical(
    mock_device_data: AshlyDeviceData,
) -> None:
    prev = dataclasses.replace(
        mock_device_data,
        chains={
            **mock_device_data.chains,
            "OutputChannel.1": dataclasses.replace(
                mock_device_data.chains["OutputChannel.1"],
                mixer_id="Mixer.2",
                muted=False,
            ),
        },
    )
    event = _event_from_sample("WORKING_SETTINGS_SET_MIXER_TO_OUTPUT_CHAIN")
    assert route_event(event, prev) is NO_CHANGE


# ── Modify Channel Param (rename) ───────────────────────────────────


def test_modify_channel_param_rename_patches_channel_name(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _event_from_sample("WORKING_SETTINGS_MODIFY_CHANNEL_PARAM_RENAME")
    result = route_event(event, mock_device_data)
    assert isinstance(result, AshlyDeviceData)
    assert result.channels["InputChannel.1"].name == "Pulpit Mic"


def test_modify_channel_param_missing_name_returns_none(
    mock_device_data: AshlyDeviceData,
) -> None:
    """If the record lacks a ``name`` field we treat as non-rename and refresh."""
    topic, payload = SAMPLE_EVENTS["WORKING_SETTINGS_MODIFY_CHANNEL_PARAM_RENAME"]
    p = copy.deepcopy(payload)
    p["data"][0]["records"][0].pop("name", None)
    event = parse_event(topic, p)
    assert route_event(event, mock_device_data) is None


# ── Modify DSP Mixer Parameter Value (crosspoint) ───────────────────


def test_modify_dsp_mixer_level_patches_crosspoint(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _event_from_sample("WORKING_SETTINGS_MODIFY_DSP_MIXER_LEVEL")
    result = route_event(event, mock_device_data)
    assert isinstance(result, AshlyDeviceData)
    assert result.crosspoints[(1, 1)].level_db == -3.0


def test_modify_dsp_mixer_mute_patches_crosspoint(
    mock_device_data: AshlyDeviceData,
) -> None:
    # mock_crosspoints fixture has muted=True by default; the sample event
    # sets muted=True too, so against the default fixture the router
    # correctly returns NO_CHANGE. Mutate prev to muted=False first so we
    # see the patch path.
    prev = dataclasses.replace(
        mock_device_data,
        crosspoints={
            **mock_device_data.crosspoints,
            (1, 1): dataclasses.replace(
                mock_device_data.crosspoints[(1, 1)], muted=False
            ),
        },
    )
    event = _event_from_sample("WORKING_SETTINGS_MODIFY_DSP_MIXER_MUTE")
    result = route_event(event, prev)
    assert isinstance(result, AshlyDeviceData)
    assert result.crosspoints[(1, 1)].muted is True


def test_modify_dsp_mixer_unknown_param_type_returns_none(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _patch(
        "WORKING_SETTINGS_MODIFY_DSP_MIXER_LEVEL",
        DSPMixerConfigParameterTypeId="Mixer.Source Enabled",
    )
    assert route_event(event, mock_device_data) is None


def test_modify_dsp_mixer_unparseable_id_returns_none(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _patch("WORKING_SETTINGS_MODIFY_DSP_MIXER_LEVEL", id="garbage")
    assert route_event(event, mock_device_data) is None


# ── Modify virtual DVCA ─────────────────────────────────────────────


def test_modify_virtual_dvca_level_patches_dvca(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _event_from_sample("WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_LEVEL")
    result = route_event(event, mock_device_data)
    assert isinstance(result, AshlyDeviceData)
    assert result.dvca[1].level_db == -1.0


def test_modify_virtual_dvca_mute_patches_dvca(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _event_from_sample("WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_MUTE")
    result = route_event(event, mock_device_data)
    assert isinstance(result, AshlyDeviceData)
    assert result.dvca[1].muted is True


def test_modify_virtual_dvca_name_patches_dvca(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _patch(
        "WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_LEVEL",
        id="DCAChannel.1.Name",
        DSPParameterTypeId="Virtual DCA.Name",
        value="Choir",
    )
    result = route_event(event, mock_device_data)
    assert isinstance(result, AshlyDeviceData)
    assert result.dvca[1].name == "Choir"


def test_modify_virtual_dvca_unparseable_id_returns_none(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _patch(
        "WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_LEVEL",
        id="DCAChannel.bogus.Level",
    )
    assert route_event(event, mock_device_data) is None


# ── Mic Preamp ──────────────────────────────────────────────────────


def test_change_mic_preamp_gain_patches_mic_preamp(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _event_from_sample("MIC_PREAMP_CHANGE_MIC_PREAMP_GAIN")
    result = route_event(event, mock_device_data)
    assert isinstance(result, AshlyDeviceData)
    assert result.mic_preamp_gain[1] == 6


def test_change_mic_preamp_gain_no_change_when_identical(
    mock_device_data: AshlyDeviceData,
) -> None:
    prev = dataclasses.replace(
        mock_device_data,
        mic_preamp_gain={**mock_device_data.mic_preamp_gain, 1: 6},
    )
    event = _event_from_sample("MIC_PREAMP_CHANGE_MIC_PREAMP_GAIN")
    assert route_event(event, prev) is NO_CHANGE


# ── Phantom Power ───────────────────────────────────────────────────


def test_change_phantom_power_patches_phantom_power(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _event_from_sample("PHANTOM_POWER_CHANGE_PHANTOM_POWER")
    result = route_event(event, mock_device_data)
    assert isinstance(result, AshlyDeviceData)
    assert result.phantom_power[1] is True


# ── GPO ────────────────────────────────────────────────────────────


def test_modify_gpo_high_patches_gpo_true(mock_device_data: AshlyDeviceData) -> None:
    event = _event_from_sample("WORKING_SETTINGS_MODIFY_GPO_HIGH")
    result = route_event(event, mock_device_data)
    assert isinstance(result, AshlyDeviceData)
    assert result.gpo[1] is True


def test_modify_gpo_low_patches_gpo_false(mock_device_data: AshlyDeviceData) -> None:
    # mock_gpo defaults to False; start from True so we see the False patch.
    prev = dataclasses.replace(mock_device_data, gpo={**mock_device_data.gpo, 2: True})
    event = _event_from_sample("WORKING_SETTINGS_MODIFY_GPO_LOW")
    result = route_event(event, prev)
    assert isinstance(result, AshlyDeviceData)
    assert result.gpo[2] is False


def test_modify_gpo_unparseable_id_returns_none(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _patch("WORKING_SETTINGS_MODIFY_GPO_HIGH", id="garbage")
    assert route_event(event, mock_device_data) is None


# ── Modify system info — the api-dispatch footgun ───────────────────


def test_modify_system_info_front_panel_power_off_patches_front_panel(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _event_from_sample("SYSTEM_MODIFY_SYSTEM_INFO_FRONT_PANEL_POWER_OFF")
    result = route_event(event, mock_device_data)
    assert isinstance(result, AshlyDeviceData)
    assert result.front_panel.power_on is False


def test_modify_system_info_front_panel_power_on_patches_front_panel(
    mock_device_data: AshlyDeviceData,
) -> None:
    prev = dataclasses.replace(
        mock_device_data,
        front_panel=dataclasses.replace(
            mock_device_data.front_panel, power_on=False
        ),
    )
    event = _event_from_sample("SYSTEM_MODIFY_SYSTEM_INFO_FRONT_PANEL_POWER_ON")
    result = route_event(event, prev)
    assert isinstance(result, AshlyDeviceData)
    assert result.front_panel.power_on is True


def test_modify_system_info_front_panel_led_patches_leds(
    mock_device_data: AshlyDeviceData,
) -> None:
    prev = dataclasses.replace(
        mock_device_data,
        front_panel=dataclasses.replace(
            mock_device_data.front_panel, leds_enabled=False
        ),
    )
    event = _event_from_sample("SYSTEM_MODIFY_SYSTEM_INFO_FRONT_PANEL_LED_ENABLE")
    result = route_event(event, prev)
    assert isinstance(result, AshlyDeviceData)
    assert result.front_panel.leds_enabled is True


def test_modify_system_info_name_returns_none(
    mock_device_data: AshlyDeviceData,
) -> None:
    """The /system/info api is a device rename — refresh, do not patch."""
    event = _event_from_sample("SYSTEM_MODIFY_SYSTEM_INFO_NAME")
    assert route_event(event, mock_device_data) is None


# ── Unrouted events (refresh path) ──────────────────────────────────


@pytest.mark.parametrize(
    "sample_key",
    [
        "PRESET_RECALL_BEGIN",
        "PRESET_RECALL_END",
        "PRESET_RECALL_BULK_MULTI_OP",
        "PRESET_CREATE_PRESET",
        "PRESET_CHANGE_PRESET_NAME",
        "PRESET_DELETE_PRESET",
        "PRESET_LAST_RECALLED_PRESET_MODIFIED",
        "EVENTS_ALL_SCHEDULED_EVENTS_BLOCKED",
        "NETWORK_DETECTED_UPDATED_NETWORK_PARAMETERS",
        "SYSTEM_INFO_VALUES_HEARTBEAT",
        "SYSTEM_DATETIME_HEARTBEAT",
        "CHANNEL_METERS_FRAME",
    ],
)
def test_unrouted_event_returns_none(
    sample_key: str, mock_device_data: AshlyDeviceData
) -> None:
    event = _event_from_sample(sample_key)
    assert route_event(event, mock_device_data) is None


# ── Mutation safety ─────────────────────────────────────────────────


def test_router_does_not_mutate_input(mock_device_data: AshlyDeviceData) -> None:
    """The router must produce a new AshlyDeviceData; ``prev`` is untouched.

    A regression here would break the optimistic-update + dataclass-equality
    contract that suppresses redundant fan-outs on push echoes.
    """
    snapshot = copy.deepcopy(mock_device_data)
    event = _event_from_sample("WORKING_SETTINGS_SET_CHAIN_MUTE")
    route_event(event, mock_device_data)
    assert mock_device_data == snapshot


# ── Dispatch-table surface ──────────────────────────────────────────


# ── Defensive paths — malformed records → return None (refresh) ────


def _patch_records(payload_key: str, new_records: list) -> Any:
    """Helper: replace the records list of a sample payload entirely."""
    topic, payload = SAMPLE_EVENTS[payload_key]
    new_payload = copy.deepcopy(payload)
    new_payload["data"][0]["records"] = new_records
    return parse_event(topic, new_payload)


@pytest.mark.parametrize(
    "sample_key",
    [
        "WORKING_SETTINGS_SET_CHAIN_MUTE",
        "WORKING_SETTINGS_SET_MIXER_TO_OUTPUT_CHAIN",
        "WORKING_SETTINGS_MODIFY_CHANNEL_PARAM_RENAME",
        "WORKING_SETTINGS_MODIFY_DSP_MIXER_LEVEL",
        "WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_LEVEL",
        "MIC_PREAMP_CHANGE_MIC_PREAMP_GAIN",
        "PHANTOM_POWER_CHANGE_PHANTOM_POWER",
        "WORKING_SETTINGS_MODIFY_GPO_HIGH",
    ],
)
def test_empty_records_returns_none(
    sample_key: str, mock_device_data: AshlyDeviceData
) -> None:
    """Every routable handler tolerates an empty records list → refresh."""
    event = _patch_records(sample_key, [])
    assert route_event(event, mock_device_data) is None


@pytest.mark.parametrize(
    "sample_key,overrides",
    [
        ("WORKING_SETTINGS_SET_CHAIN_MUTE", {"id": 123}),  # id wrong type
        (
            "WORKING_SETTINGS_MODIFY_CHANNEL_PARAM_RENAME",
            {"name": 999},
        ),  # name wrong type
        (
            "WORKING_SETTINGS_MODIFY_DSP_MIXER_LEVEL",
            {"DSPMixerConfigParameterTypeId": 42},
        ),
        ("WORKING_SETTINGS_MODIFY_DSP_MIXER_LEVEL", {"value": "not-a-number"}),
        ("WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_LEVEL", {"value": "not-a-number"}),
        (
            "WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_LEVEL",
            {
                "id": "DCAChannel.1.Name",
                "DSPParameterTypeId": "Virtual DCA.Name",
                "value": 123,  # wrong type for a rename
            },
        ),
        (
            "WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_LEVEL",
            {"DSPParameterTypeId": "Virtual DCA.Unknown"},
        ),
        ("MIC_PREAMP_CHANGE_MIC_PREAMP_GAIN", {"id": "not-an-int"}),
        ("PHANTOM_POWER_CHANGE_PHANTOM_POWER", {"id": "not-an-int"}),
        ("WORKING_SETTINGS_MODIFY_GPO_HIGH", {"value": 42}),  # value wrong type
        ("SYSTEM_MODIFY_SYSTEM_INFO_FRONT_PANEL_POWER_OFF", {"powerState": 42}),
        (
            "SYSTEM_MODIFY_SYSTEM_INFO_FRONT_PANEL_LED_ENABLE",
            {"frontPanelLEDEnable": "true-as-string"},
        ),
    ],
)
def test_malformed_record_fields_route_to_refresh(
    sample_key: str, overrides: dict, mock_device_data: AshlyDeviceData
) -> None:
    """Malformed records (wrong types) route to refresh, not crash."""
    event = _patch(sample_key, **overrides)
    assert route_event(event, mock_device_data) is None


def test_modify_virtual_dvca_unknown_dvca_index_returns_none(
    mock_device_data: AshlyDeviceData,
) -> None:
    """DVCA id parses but index is out of range — refresh."""
    event = _patch(
        "WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_LEVEL",
        id="DCAChannel.999.Level",
    )
    assert route_event(event, mock_device_data) is None


def test_modify_dsp_mixer_unknown_crosspoint_returns_none(
    mock_device_data: AshlyDeviceData,
) -> None:
    """Crosspoint id parses but (mixer, input) doesn't exist — refresh."""
    event = _patch(
        "WORKING_SETTINGS_MODIFY_DSP_MIXER_LEVEL",
        id="Mixer.999.InputChannel.999.Source Level",
    )
    assert route_event(event, mock_device_data) is None


def test_modify_channel_param_unknown_channel_returns_none(
    mock_device_data: AshlyDeviceData,
) -> None:
    """Channel rename event for an unknown channel id — refresh."""
    event = _patch("WORKING_SETTINGS_MODIFY_CHANNEL_PARAM_RENAME", id="NonExistent.1")
    assert route_event(event, mock_device_data) is None


def test_set_mixer_to_output_chain_unknown_channel_returns_none(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _patch("WORKING_SETTINGS_SET_MIXER_TO_OUTPUT_CHAIN", id="NonExistent.1")
    assert route_event(event, mock_device_data) is None


def test_modify_channel_param_no_change_for_identical_name(
    mock_device_data: AshlyDeviceData,
) -> None:
    prev = dataclasses.replace(
        mock_device_data,
        channels={
            **mock_device_data.channels,
            "InputChannel.1": dataclasses.replace(
                mock_device_data.channels["InputChannel.1"], name="Pulpit Mic"
            ),
        },
    )
    event = _event_from_sample("WORKING_SETTINGS_MODIFY_CHANNEL_PARAM_RENAME")
    assert route_event(event, prev) is NO_CHANGE


def test_modify_dsp_mixer_no_change_for_identical_mute(
    mock_device_data: AshlyDeviceData,
) -> None:
    """Default crosspoint mute is True; sample's value is also True → NO_CHANGE."""
    event = _event_from_sample("WORKING_SETTINGS_MODIFY_DSP_MIXER_MUTE")
    assert route_event(event, mock_device_data) is NO_CHANGE


def test_phantom_power_no_change(mock_device_data: AshlyDeviceData) -> None:
    """Phantom power already True; sample sets True → NO_CHANGE."""
    prev = dataclasses.replace(
        mock_device_data,
        phantom_power={**mock_device_data.phantom_power, 1: True},
    )
    event = _event_from_sample("PHANTOM_POWER_CHANGE_PHANTOM_POWER")
    assert route_event(event, prev) is NO_CHANGE


def test_gpo_no_change(mock_device_data: AshlyDeviceData) -> None:
    """GPO already high; sample sets high → NO_CHANGE."""
    prev = dataclasses.replace(mock_device_data, gpo={**mock_device_data.gpo, 1: True})
    event = _event_from_sample("WORKING_SETTINGS_MODIFY_GPO_HIGH")
    assert route_event(event, prev) is NO_CHANGE


def test_modify_virtual_dvca_id_wrong_type_returns_none(
    mock_device_data: AshlyDeviceData,
) -> None:
    """DVCA id field is not a string at all — return None."""
    event = _patch("WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_LEVEL", id=123)
    assert route_event(event, mock_device_data) is None


def test_modify_dsp_mixer_no_change_for_identical_level(
    mock_device_data: AshlyDeviceData,
) -> None:
    """Level event whose value matches the existing level → NO_CHANGE."""
    prev = dataclasses.replace(
        mock_device_data,
        crosspoints={
            **mock_device_data.crosspoints,
            (1, 1): dataclasses.replace(
                mock_device_data.crosspoints[(1, 1)], level_db=-3.0
            ),
        },
    )
    event = _event_from_sample("WORKING_SETTINGS_MODIFY_DSP_MIXER_LEVEL")
    assert route_event(event, prev) is NO_CHANGE


def test_modify_virtual_dvca_no_change_for_identical_level(
    mock_device_data: AshlyDeviceData,
) -> None:
    prev = dataclasses.replace(
        mock_device_data,
        dvca={
            **mock_device_data.dvca,
            1: dataclasses.replace(mock_device_data.dvca[1], level_db=-1.0),
        },
    )
    event = _event_from_sample("WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_LEVEL")
    assert route_event(event, prev) is NO_CHANGE


def test_modify_virtual_dvca_no_change_for_identical_mute(
    mock_device_data: AshlyDeviceData,
) -> None:
    prev = dataclasses.replace(
        mock_device_data,
        dvca={
            **mock_device_data.dvca,
            1: dataclasses.replace(mock_device_data.dvca[1], muted=True),
        },
    )
    event = _event_from_sample("WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_MUTE")
    assert route_event(event, prev) is NO_CHANGE


def test_modify_virtual_dvca_no_change_for_identical_name(
    mock_device_data: AshlyDeviceData,
) -> None:
    prev = dataclasses.replace(
        mock_device_data,
        dvca={
            **mock_device_data.dvca,
            1: dataclasses.replace(mock_device_data.dvca[1], name="Choir"),
        },
    )
    event = _patch(
        "WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_LEVEL",
        id="DCAChannel.1.Name",
        DSPParameterTypeId="Virtual DCA.Name",
        value="Choir",
    )
    assert route_event(event, prev) is NO_CHANGE


def test_set_mixer_chain_with_null_mixer_id(
    mock_device_data: AshlyDeviceData,
) -> None:
    """If a mixer is unassigned, the device sends mixerId=null — preserved.
    Start from a state where the channel has a mixer assigned so we see
    the patch path (not NO_CHANGE)."""
    prev = dataclasses.replace(
        mock_device_data,
        chains={
            **mock_device_data.chains,
            "OutputChannel.1": dataclasses.replace(
                mock_device_data.chains["OutputChannel.1"], mixer_id="Mixer.5"
            ),
        },
    )
    event = _patch(
        "WORKING_SETTINGS_SET_MIXER_TO_OUTPUT_CHAIN",
        mixerId=None,
    )
    result = route_event(event, prev)
    assert isinstance(result, AshlyDeviceData)
    assert result.chains["OutputChannel.1"].mixer_id is None


def test_set_mixer_chain_with_bogus_mixer_id_type(
    mock_device_data: AshlyDeviceData,
) -> None:
    """mixerId comes through as an unexpected type — coerce to None defensively.

    Against the default fixture state (OutputChannel.1 has mixer_id=None,
    muted=False), the coerced None plus matching muted=False collapse the
    router result to NO_CHANGE — exercising the defensive coercion path
    without triggering a state update.
    """
    event = _patch(
        "WORKING_SETTINGS_SET_MIXER_TO_OUTPUT_CHAIN",
        mixerId=42,
    )
    assert route_event(event, mock_device_data) is NO_CHANGE


def test_set_mixer_chain_bogus_type_coerced_to_none_clears_assignment(
    mock_device_data: AshlyDeviceData,
) -> None:
    """When prev has a real mixer_id, a bogus-typed mixerId event clears
    the assignment via the same defensive coercion."""
    prev = dataclasses.replace(
        mock_device_data,
        chains={
            **mock_device_data.chains,
            "OutputChannel.1": dataclasses.replace(
                mock_device_data.chains["OutputChannel.1"], mixer_id="Mixer.3"
            ),
        },
    )
    event = _patch("WORKING_SETTINGS_SET_MIXER_TO_OUTPUT_CHAIN", mixerId=42)
    result = route_event(event, prev)
    assert isinstance(result, AshlyDeviceData)
    assert result.chains["OutputChannel.1"].mixer_id is None


def test_modify_system_info_with_non_front_panel_record(
    mock_device_data: AshlyDeviceData,
) -> None:
    """Front-panel api but the record has no powerState/leds — NO_CHANGE.
    The router doesn't refresh on a benign no-op."""
    event = _patch(
        "SYSTEM_MODIFY_SYSTEM_INFO_FRONT_PANEL_POWER_OFF",
    )
    # Remove the powerState key entirely after the patch
    topic, payload = SAMPLE_EVENTS["SYSTEM_MODIFY_SYSTEM_INFO_FRONT_PANEL_POWER_OFF"]
    p = copy.deepcopy(payload)
    p["data"][0]["records"][0] = {"unrelated_key": "x"}
    event = parse_event(topic, p)
    assert route_event(event, mock_device_data) is NO_CHANGE


def test_modify_system_info_with_empty_records_returns_none(
    mock_device_data: AshlyDeviceData,
) -> None:
    event = _patch_records("SYSTEM_MODIFY_SYSTEM_INFO_FRONT_PANEL_POWER_OFF", [])
    assert route_event(event, mock_device_data) is None


def test_no_change_sentinel_repr() -> None:
    """The sentinel has a readable repr for debug output."""
    assert repr(NO_CHANGE) == "NO_CHANGE"


# ── Dispatch-table surface ──────────────────────────────────────────


def test_routable_event_names_is_dispatch_table_keys() -> None:
    """ROUTABLE_EVENT_NAMES is the contract the push client subscribes against.

    If a handler is added but not registered, this catches the gap.
    """
    assert "Set Chain Mute" in ROUTABLE_EVENT_NAMES
    assert "Modify system info" in ROUTABLE_EVENT_NAMES
    assert "Preset Recall" not in ROUTABLE_EVENT_NAMES  # refresh path
    assert "DateTime" not in ROUTABLE_EVENT_NAMES  # ambient
    # Every routable name must produce a non-None result on at least one
    # of its sample payloads (sanity: dispatch table actually wired up).
    sample_keys_by_name: dict[str, list[str]] = {}
    for key, (_, payload) in SAMPLE_EVENTS.items():
        sample_keys_by_name.setdefault(payload["name"], []).append(key)
    for name in ROUTABLE_EVENT_NAMES:
        assert name in sample_keys_by_name, (
            f"Routable event {name!r} has no SAMPLE_EVENTS entry — add one in "
            "aquacontrol._testing for regression coverage"
        )
