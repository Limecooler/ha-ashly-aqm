"""Tests for the event model + parser."""

from __future__ import annotations

import pytest

from aquacontrol import (
    Operation,
    is_ambient,
    is_meter,
    parse_event,
)
from aquacontrol.topics import (
    CHANNEL_METERS,
    NETWORK,
    SYSTEM,
    WORKING_SETTINGS,
)

# ── parse_event ──────────────────────────────────────────────────────────


def test_parse_single_op_event():
    """A typical single-op event (Set Chain Mute) parses with one operation."""
    payload = {
        "name": "Set Chain Mute",
        "data": [
            {
                "api": "/workingsettings/dsp/chain",
                "records": [{"id": "InputChannel.1", "muted": True}],
                "type": "modify",
            }
        ],
        "uniqueId": "abc-uuid-123",
    }
    event = parse_event(WORKING_SETTINGS, payload)
    assert event.topic == WORKING_SETTINGS
    assert event.name == "Set Chain Mute"
    assert event.unique_id == "abc-uuid-123"
    assert len(event.operations) == 1
    assert event.api == "/workingsettings/dsp/chain"
    assert event.type == "modify"
    assert event.records == [{"id": "InputChannel.1", "muted": True}]


def test_parse_multi_op_event_returns_none_for_single_op_accessors():
    """For preset-recall-style multi-op events, .api/.records/.type are None/empty."""
    payload = {
        "name": "Preset Recall",
        "data": [
            {"api": "/workingsettings/dsp/block", "records": [], "type": "delete"},
            {"api": "/workingsettings/dsp/mixer", "records": [{"id": "Mixer.1"}], "type": "new"},
        ],
        "uniqueId": "session-1",
    }
    event = parse_event("Preset", payload)
    assert len(event.operations) == 2
    assert event.api is None
    assert event.records == []
    assert event.type is None
    # The operations are accessible individually
    assert event.operations[0].type == "delete"
    assert event.operations[1].type == "new"


def test_parse_handles_system_emitted_unique_id_zero():
    """Some events use integer 0 to mean 'system-emitted (no session)'."""
    payload = {
        "name": "DateTime",
        "data": [{"api": "update", "records": ["2026-05-14T08:00:00"], "type": "modify"}],
        "uniqueId": 0,
    }
    event = parse_event(SYSTEM, payload)
    assert event.unique_id == 0


def test_parse_handles_public_broadcast_unique_id_null():
    """Public broadcasts (heartbeats) carry uniqueId: null."""
    payload = {
        "name": "System Info Values",
        "data": [{"api": "update", "records": [{"cpu": "9.66"}], "type": "modify"}],
        "uniqueId": None,
    }
    event = parse_event(SYSTEM, payload)
    assert event.unique_id is None


def test_parse_tolerates_empty_data():
    """Preset Recall Begin/End deliver `data: []` — no operations."""
    payload = {"name": "Preset Recall Begin", "data": [], "uniqueId": "session-1"}
    event = parse_event("Preset", payload)
    assert event.operations == ()
    assert event.api is None
    assert event.records == []


def test_parse_tolerates_missing_fields():
    """Defaults are safe rather than raising on partial payloads."""
    event = parse_event(SYSTEM, {})
    assert event.name == ""
    assert event.operations == ()
    assert event.unique_id is None


def test_parse_tolerates_non_dict_payload():
    """A non-dict payload (e.g. an unexpected protocol drift) gives an empty event."""
    event = parse_event(SYSTEM, "not a dict")
    assert event.name == ""
    assert event.operations == ()
    assert event.raw == {"_unparseable": "not a dict"}


def test_parse_skips_non_dict_data_entries():
    """A garbage entry inside data[] is dropped, others survive."""
    payload = {
        "name": "Edge Case",
        "data": [
            "not a dict",
            {"api": "/x", "records": [{"k": "v"}], "type": "modify"},
            None,
        ],
        "uniqueId": "s",
    }
    event = parse_event(SYSTEM, payload)
    assert len(event.operations) == 1
    assert event.operations[0].api == "/x"


def test_parse_coerces_non_list_records_to_single_element_list():
    """If the device sends `records: {...}` (not a list), wrap it."""
    payload = {
        "name": "Edge Case",
        "data": [{"api": "/x", "records": {"id": "X"}, "type": "modify"}],
        "uniqueId": "s",
    }
    event = parse_event(SYSTEM, payload)
    assert event.records == [{"id": "X"}]


# ── classification helpers ──────────────────────────────────────────────


def test_is_ambient_known_pairs():
    assert is_ambient(SYSTEM, "System Info Values")
    assert is_ambient(SYSTEM, "DateTime")
    assert is_ambient(NETWORK, "detected updated network parameters")


def test_is_ambient_rejects_unknown():
    assert not is_ambient(SYSTEM, "Modify system info")
    assert not is_ambient(CHANNEL_METERS, "Channel Meters")  # meters aren't ambient
    assert not is_ambient(WORKING_SETTINGS, "Set Chain Mute")


def test_is_meter_only_channel_meters():
    assert is_meter(CHANNEL_METERS, "Channel Meters")
    assert not is_meter(SYSTEM, "Channel Meters")  # wrong topic
    assert not is_meter(CHANNEL_METERS, "Block Meters")  # wrong name


def test_event_classification_properties():
    """is_ambient / is_meter / is_state_change are mutually exclusive."""
    state = parse_event(WORKING_SETTINGS, {"name": "Set Chain Mute", "data": [], "uniqueId": "s"})
    ambient = parse_event(SYSTEM, {"name": "System Info Values", "data": [], "uniqueId": None})
    meter = parse_event(CHANNEL_METERS, {"name": "Channel Meters", "data": [], "uniqueId": None})

    assert state.is_state_change and not state.is_ambient and not state.is_meter
    assert ambient.is_ambient and not ambient.is_state_change and not ambient.is_meter
    assert meter.is_meter and not meter.is_state_change and not meter.is_ambient


# ── echo filtering ──────────────────────────────────────────────────────


def test_is_from_session_matches_uuid():
    event = parse_event(WORKING_SETTINGS, {"name": "x", "data": [], "uniqueId": "my-uuid"})
    assert event.is_from_session("my-uuid")
    assert not event.is_from_session("other-uuid")


def test_is_from_session_returns_false_when_caller_id_is_none():
    """Until set_session_id has been called, is_from_session is always False."""
    event = parse_event(WORKING_SETTINGS, {"name": "x", "data": [], "uniqueId": "my-uuid"})
    assert not event.is_from_session(None)


def test_is_from_session_handles_integer_zero():
    """System-emitted events (uniqueId: 0) should never match a string UUID."""
    event = parse_event(SYSTEM, {"name": "Identify", "data": [], "uniqueId": 0})
    assert not event.is_from_session("any-uuid")


# ── Operation dataclass ────────────────────────────────────────────────


def test_operation_is_immutable():
    op = Operation(api="/x", records=[], type="modify")
    with pytest.raises(AttributeError):
        op.api = "/y"  # type: ignore[misc]


def test_event_is_immutable():
    event = parse_event(SYSTEM, {"name": "x", "data": [], "uniqueId": None})
    with pytest.raises(AttributeError):
        event.name = "y"  # type: ignore[misc]


# ── exhaustiveness ──────────────────────────────────────────────────────


def test_real_world_payloads():
    """A small fixture set captured from a live AQM1208 parses correctly."""
    fixtures: list[tuple[str, dict, str, int]] = [
        # (topic, payload, expected_name, expected_op_count)
        (
            SYSTEM,
            {
                "name": "Modify system info",
                "data": [
                    {
                        "api": "/system/frontPanel/info",
                        "records": [{"powerState": "Off"}],
                        "type": "modify",
                    }
                ],
                "uniqueId": "cb0f9400-4f80-11f1-b4ba-43ebb4c1bdbc",
            },
            "Modify system info",
            1,
        ),
        (
            "Preset",
            {
                "name": "Preset Recall Begin",
                "data": [],
                "uniqueId": "1cc64f50-4f81-11f1-b4ba-43ebb4c1bdbc",
            },
            "Preset Recall Begin",
            0,
        ),
        (
            "Preset",
            {"name": "Preset Recall End", "data": [], "uniqueId": 0},
            "Preset Recall End",
            0,
        ),
        (
            "MicPreamp",
            {
                "name": "Change Mic Preamp Gain",
                "data": [
                    {
                        "api": "/micPreamp",
                        "records": [
                            {
                                "id": 1,
                                "DSPChannelId": "InputChannel.1",
                                "gain": 6,
                                "micPreampTypeId": 1,
                            }
                        ],
                        "type": "modify",
                    }
                ],
                "uniqueId": "session-uuid",
            },
            "Change Mic Preamp Gain",
            1,
        ),
        (
            "Events",
            {
                "name": "All Scheduled Events Blocked",
                "data": [{}],
                "uniqueId": 0,
            },
            "All Scheduled Events Blocked",
            1,
        ),
    ]
    for topic, payload, expected_name, expected_op_count in fixtures:
        event = parse_event(topic, payload)
        assert event.name == expected_name
        assert len(event.operations) == expected_op_count, (
            f"{expected_name}: expected {expected_op_count} ops, got {len(event.operations)}"
        )


# ── is_single_operation ────────────────────────────────────────────────


def test_is_single_operation_true_for_single_op():
    event = parse_event(
        WORKING_SETTINGS,
        {
            "name": "Set Chain Mute",
            "data": [{"api": "/x", "records": [], "type": "modify"}],
            "uniqueId": "s",
        },
    )
    assert event.is_single_operation


def test_is_single_operation_false_for_multi_op():
    event = parse_event(
        "Preset",
        {
            "name": "Preset Recall",
            "data": [
                {"api": "/x", "records": [], "type": "delete"},
                {"api": "/y", "records": [], "type": "new"},
            ],
            "uniqueId": "s",
        },
    )
    assert not event.is_single_operation


def test_is_single_operation_false_for_empty_ops():
    event = parse_event(SYSTEM, {"name": "Preset Recall Begin", "data": [], "uniqueId": "s"})
    assert not event.is_single_operation


# ── strict parsing mode ────────────────────────────────────────────────


def test_parse_event_strict_rejects_non_mapping():
    from aquacontrol.exceptions import AquaControlProtocolError

    with pytest.raises(AquaControlProtocolError, match="not a mapping"):
        parse_event(SYSTEM, "not a dict", strict=True)


def test_parse_event_strict_rejects_non_sequence_data():
    from aquacontrol.exceptions import AquaControlProtocolError

    with pytest.raises(AquaControlProtocolError, match="'data' is not a sequence"):
        parse_event(SYSTEM, {"name": "X", "data": "not a list", "uniqueId": "s"}, strict=True)


def test_parse_event_strict_rejects_non_mapping_op():
    from aquacontrol.exceptions import AquaControlProtocolError

    with pytest.raises(AquaControlProtocolError, match="Operation entry"):
        parse_event(
            SYSTEM,
            {"name": "X", "data": ["not a dict"], "uniqueId": "s"},
            strict=True,
        )


def test_parse_event_strict_passes_well_formed():
    """Strict mode still accepts the normal case."""
    event = parse_event(
        SYSTEM,
        {
            "name": "Set Chain Mute",
            "data": [{"api": "/x", "records": [], "type": "modify"}],
            "uniqueId": "s",
        },
        strict=True,
    )
    assert event.name == "Set Chain Mute"
