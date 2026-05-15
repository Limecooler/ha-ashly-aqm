"""Captured push-event payloads for downstream test suites.

This module exposes :data:`SAMPLE_EVENTS` — a dict of ``(topic, payload)``
tuples for every routable plus many non-routable push-event shapes
observed on a live AQM1208. Consumers (notably the ha-ashly-aqm Home
Assistant integration's router tests) import these constants to avoid
duplicating literal payloads, which would drift from the on-the-wire
shape over time.

Each entry is shaped for direct consumption::

    from aquacontrol import parse_event
    from aquacontrol._testing import SAMPLE_EVENTS

    topic, payload = SAMPLE_EVENTS["WORKING_SETTINGS_SET_CHAIN_MUTE"]
    event = parse_event(topic, payload)

Naming convention for keys: ``<TOPIC>_<INNER_NAME>`` in screaming
snake case, with semantic suffixes when a single inner name has multiple
distinct shapes — e.g. ``SYSTEM_MODIFY_SYSTEM_INFO_FRONT_PANEL`` vs
``SYSTEM_MODIFY_SYSTEM_INFO_NAME`` (both share inner name
``Modify system info`` but carry different ``api`` paths; see WEBSOCKET-API
§7 for the disambiguation rationale).

Stability contract: the dict keys are stable across the 0.2.x line.
The payload contents are real captures; if the device begins emitting a
different shape, a consumer's tests should catch it. The module is
underscore-prefixed to signal "intended for our own ecosystem of test
suites" — it is not in the public API contract that
:class:`aquacontrol.AquaControlClient` and friends are.
"""

from __future__ import annotations

from typing import Any, Final

from .topics import (
    CHANNEL_METERS,
    EVENTS,
    MIC_PREAMP,
    NETWORK,
    PHANTOM_POWER,
    PRESET,
    SYSTEM,
    WORKING_SETTINGS,
)

#: Mapping of stable name → ``(topic, payload)`` tuple. Pass each tuple
#: into :func:`aquacontrol.parse_event` to get a fully-formed
#: :class:`aquacontrol.Event` for use in tests.
SAMPLE_EVENTS: Final[dict[str, tuple[str, dict[str, Any]]]] = {
    # ── WorkingSettings: chain / channel mutations ─────────────────
    "WORKING_SETTINGS_SET_CHAIN_MUTE": (
        WORKING_SETTINGS,
        {
            "name": "Set Chain Mute",
            "data": [
                {
                    "api": "/workingsettings/dsp/chain",
                    "records": [
                        {
                            "id": "InputChannel.1",
                            "isWorkingSettings": True,
                            "muted": True,
                            "mixerId": None,
                            "DSPChannelId": "InputChannel.1",
                            "presetId": None,
                        }
                    ],
                    "type": "modify",
                }
            ],
            "uniqueId": "c462174b-78f8-41b0-b0b9-253882606470",
        },
    ),
    "WORKING_SETTINGS_SET_MIXER_TO_OUTPUT_CHAIN": (
        WORKING_SETTINGS,
        {
            "name": "Set mixer to output chain",
            "data": [
                {
                    "api": "/workingsettings/dsp/chain",
                    "records": [
                        {
                            "id": "OutputChannel.1",
                            "isWorkingSettings": True,
                            "muted": False,
                            "mixerId": "Mixer.2",
                            "DSPChannelId": "OutputChannel.1",
                            "presetId": None,
                        }
                    ],
                    "type": "modify",
                }
            ],
            "uniqueId": "f7caebd4-5f44-4c24-bb9b-e538da2d36ed",
        },
    ),
    "WORKING_SETTINGS_MODIFY_CHANNEL_PARAM_RENAME": (
        WORKING_SETTINGS,
        {
            "name": "Modify Channel Param",
            "data": [
                {
                    "api": "/workingsettings/dsp/channel",
                    "records": [
                        {
                            "id": "InputChannel.1",
                            "defaultName": "Mic/Line 1",
                            "name": "Pulpit Mic",
                            "type": "Mic/Line Input",
                            "baseType": "Input",
                            "channelNumber": 1,
                        }
                    ],
                    "type": "modify",
                }
            ],
            "uniqueId": "e8348862-dd48-42a3-b0eb-2cd7c36d1aea",
        },
    ),
    # ── WorkingSettings: crosspoint mutations ──────────────────────
    "WORKING_SETTINGS_MODIFY_DSP_MIXER_LEVEL": (
        WORKING_SETTINGS,
        {
            "name": "Modify DSP Mixer Parameter Value",
            "data": [
                {
                    "api": "/workingsettings/dsp/mixer/config/parameter",
                    "records": [
                        {
                            "id": "Mixer.1.InputChannel.1.Source Level",
                            "value": -3,
                            "index": 1,
                            "channelId": "InputChannel.1",
                            "DSPMixerConfigId": "Mixer.1",
                            "DSPMixerConfigParameterTypeId": "Mixer.Source Level",
                        }
                    ],
                    "type": "modify",
                }
            ],
            "uniqueId": "ee7335f7-d99d-4cf3-a68f-d1870b49fd7d",
        },
    ),
    "WORKING_SETTINGS_MODIFY_DSP_MIXER_MUTE": (
        WORKING_SETTINGS,
        {
            "name": "Modify DSP Mixer Parameter Value",
            "data": [
                {
                    "api": "/workingsettings/dsp/mixer/config/parameter",
                    "records": [
                        {
                            "id": "Mixer.1.InputChannel.1.Source Mute",
                            "value": True,
                            "index": 1,
                            "channelId": "InputChannel.1",
                            "DSPMixerConfigId": "Mixer.1",
                            "DSPMixerConfigParameterTypeId": "Mixer.Source Mute",
                        }
                    ],
                    "type": "modify",
                }
            ],
            "uniqueId": "ee7335f7-d99d-4cf3-a68f-d1870b49fd7d",
        },
    ),
    # ── WorkingSettings: DVCA ──────────────────────────────────────
    # NOTE: the api path uses capital-S "workingSettings" only on this
    # event — see WEBSOCKET-API §7 gotcha #1. The router must accept it.
    "WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_LEVEL": (
        WORKING_SETTINGS,
        {
            "name": "Modify virtual DVCA",
            "data": [
                {
                    "api": "/workingSettings/virtualDVCA/parameters",
                    "records": [
                        {
                            "id": "DCAChannel.1.Level",
                            "index": 1,
                            "DSPParameterTypeId": "Virtual DCA.Level",
                            "virtualDVCAConfigId": 1,
                            "value": -1,
                        }
                    ],
                    "type": "modify",
                }
            ],
            "uniqueId": "c462174b-78f8-41b0-b0b9-253882606470",
        },
    ),
    "WORKING_SETTINGS_MODIFY_VIRTUAL_DVCA_MUTE": (
        WORKING_SETTINGS,
        {
            "name": "Modify virtual DVCA",
            "data": [
                {
                    "api": "/workingSettings/virtualDVCA/parameters",
                    "records": [
                        {
                            "id": "DCAChannel.1.Mute",
                            "index": 1,
                            "DSPParameterTypeId": "Virtual DCA.Mute",
                            "virtualDVCAConfigId": 1,
                            "value": True,
                        }
                    ],
                    "type": "modify",
                }
            ],
            "uniqueId": "c462174b-78f8-41b0-b0b9-253882606470",
        },
    ),
    # ── WorkingSettings: GPO ───────────────────────────────────────
    "WORKING_SETTINGS_MODIFY_GPO_HIGH": (
        WORKING_SETTINGS,
        {
            "name": "Modify generalPurposeOutputConfiguration",
            "data": [
                {
                    "api": "/workingsettings/generalPurposeOutputConfiguration",
                    "records": [
                        {
                            "id": "General Purpose Output Pin.1",
                            "value": "high",
                            "presetId": None,
                            "isWorkingSettings": True,
                            "generalPurposeOutputId": 1,
                        }
                    ],
                    "type": "modify",
                }
            ],
            "uniqueId": "f7caebd4-5f44-4c24-bb9b-e538da2d36ed",
        },
    ),
    "WORKING_SETTINGS_MODIFY_GPO_LOW": (
        WORKING_SETTINGS,
        {
            "name": "Modify generalPurposeOutputConfiguration",
            "data": [
                {
                    "api": "/workingsettings/generalPurposeOutputConfiguration",
                    "records": [
                        {
                            "id": "General Purpose Output Pin.2",
                            "value": "low",
                            "presetId": None,
                            "isWorkingSettings": True,
                            "generalPurposeOutputId": 2,
                        }
                    ],
                    "type": "modify",
                }
            ],
            "uniqueId": "f7caebd4-5f44-4c24-bb9b-e538da2d36ed",
        },
    ),
    # ── MicPreamp ──────────────────────────────────────────────────
    "MIC_PREAMP_CHANGE_MIC_PREAMP_GAIN": (
        MIC_PREAMP,
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
    ),
    # ── PhantomPower ───────────────────────────────────────────────
    "PHANTOM_POWER_CHANGE_PHANTOM_POWER": (
        PHANTOM_POWER,
        {
            "name": "Change Phantom Power",
            "data": [
                {
                    "api": "/phantomPower",
                    "records": [
                        {
                            "id": 1,
                            "DSPChannelId": "InputChannel.1",
                            "enabled": True,
                        }
                    ],
                    "type": "modify",
                }
            ],
            "uniqueId": "session-uuid",
        },
    ),
    # ── System: the "Modify system info" footgun ───────────────────
    # Two distinct events share inner name "Modify system info";
    # disambiguate via api path. WEBSOCKET-API §7 gotcha #2.
    "SYSTEM_MODIFY_SYSTEM_INFO_FRONT_PANEL_POWER_OFF": (
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
            "uniqueId": "f7caebd4-5f44-4c24-bb9b-e538da2d36ed",
        },
    ),
    "SYSTEM_MODIFY_SYSTEM_INFO_FRONT_PANEL_POWER_ON": (
        SYSTEM,
        {
            "name": "Modify system info",
            "data": [
                {
                    "api": "/system/frontPanel/info",
                    "records": [{"powerState": "On"}],
                    "type": "modify",
                }
            ],
            "uniqueId": "f7caebd4-5f44-4c24-bb9b-e538da2d36ed",
        },
    ),
    "SYSTEM_MODIFY_SYSTEM_INFO_FRONT_PANEL_LED_ENABLE": (
        SYSTEM,
        {
            "name": "Modify system info",
            "data": [
                {
                    "api": "/system/frontPanel/info",
                    "records": [{"frontPanelLEDEnable": True}],
                    "type": "modify",
                }
            ],
            "uniqueId": "f7caebd4-5f44-4c24-bb9b-e538da2d36ed",
        },
    ),
    "SYSTEM_MODIFY_SYSTEM_INFO_NAME": (
        SYSTEM,
        {
            "name": "Modify system info",
            "data": [
                {
                    "api": "/system/info",
                    "records": [
                        {
                            "name": "AQM1208-LobbyRack",
                            "model": "AQM1208",
                            "softwareVersion": "1.0.5",
                        }
                    ],
                    "type": "modify",
                }
            ],
            "uniqueId": "f7caebd4-5f44-4c24-bb9b-e538da2d36ed",
        },
    ),
    # ── System: ambient heartbeats ─────────────────────────────────
    # Routers should NEVER route these to state-patch logic; the
    # is_ambient classifier filters them upstream. Included so
    # consumer tests can assert the router-does-not-touch contract.
    "SYSTEM_INFO_VALUES_HEARTBEAT": (
        SYSTEM,
        {
            "name": "System Info Values",
            "data": [
                {
                    "api": "update",
                    "records": [{"cpu": "9.66", "memory": "62.4"}],
                    "type": "modify",
                }
            ],
            "uniqueId": None,
        },
    ),
    "SYSTEM_DATETIME_HEARTBEAT": (
        SYSTEM,
        {
            "name": "DateTime",
            "data": [
                {
                    "api": "update",
                    "records": ["2026-05-14T08:00:00"],
                    "type": "modify",
                }
            ],
            "uniqueId": 0,
        },
    ),
    # ── Preset: lifecycle (unroutable → refresh) ───────────────────
    "PRESET_RECALL_BEGIN": (
        PRESET,
        {
            "name": "Preset Recall Begin",
            "data": [],
            "uniqueId": "ee7335f7-d99d-4cf3-a68f-d1870b49fd7d",
        },
    ),
    "PRESET_RECALL_END": (
        PRESET,
        {
            "name": "Preset Recall End",
            "data": [],
            "uniqueId": 0,
        },
    ),
    "PRESET_RECALL_BULK_MULTI_OP": (
        PRESET,
        {
            # Trimmed sample — a real Preset Recall middle phase
            # routinely reaches ~400 kB. Consumers stress-testing
            # large payloads should load a real capture from disk.
            "name": "Preset Recall",
            "data": [
                {
                    "api": "/preset/lastRecalled",
                    "records": [
                        {"lastRecalledPreset": "_haashly_test", "modified": False}
                    ],
                    "type": "modify",
                },
                {
                    "api": "/workingsettings/dsp/chain",
                    "records": [],
                    "type": "delete",
                },
                {
                    "api": "/workingsettings/dsp/mixer/config/parameter",
                    "records": [],
                    "type": "modify",
                },
            ],
            "uniqueId": "ee7335f7-d99d-4cf3-a68f-d1870b49fd7d",
        },
    ),
    "PRESET_CREATE_PRESET": (
        PRESET,
        {
            "name": "Create preset",
            "data": [
                {
                    "api": "/preset",
                    "records": [
                        {"id": "_haashly_test", "name": "_haashly_test", "type": "Preset"}
                    ],
                    "type": "new",
                }
            ],
            "uniqueId": "abc-uuid-123",
        },
    ),
    "PRESET_CHANGE_PRESET_NAME": (
        PRESET,
        {
            "name": "Change preset name",
            "data": [
                {
                    "api": "/preset",
                    "records": [
                        {
                            "id": "_haashly_test_v2",
                            "name": "_haashly_test_v2",
                            "previousName": "_haashly_test",
                            "type": "Preset",
                        }
                    ],
                    "type": "modify",
                }
            ],
            "uniqueId": "abc-uuid-123",
        },
    ),
    "PRESET_DELETE_PRESET": (
        PRESET,
        {
            "name": "Delete preset",
            "data": [
                {
                    "api": "/preset",
                    "records": [
                        {
                            "api": "/preset/lastRecalled",
                            "records": [{"lastRecalledPreset": "None", "modified": False}],
                            "type": "modify",
                        },
                        {
                            "api": "/preset",
                            "records": [
                                {
                                    "id": "_haashly_test_v2",
                                    "name": "_haashly_test_v2",
                                    "type": "Preset",
                                }
                            ],
                            "type": "delete",
                        },
                    ],
                    "type": "delete",
                }
            ],
            "uniqueId": "abc-uuid-123",
        },
    ),
    "PRESET_LAST_RECALLED_PRESET_MODIFIED": (
        PRESET,
        {
            "name": "Last Recalled Preset Modified",
            "data": [{"modified": True}],
            "uniqueId": None,
        },
    ),
    # ── Events topic: scheduler activity (mostly refresh) ──────────
    "EVENTS_ALL_SCHEDULED_EVENTS_BLOCKED": (
        EVENTS,
        {
            "name": "All Scheduled Events Blocked",
            "data": [{}],
            "uniqueId": 0,
        },
    ),
    # ── Network: ambient link state ────────────────────────────────
    "NETWORK_DETECTED_UPDATED_NETWORK_PARAMETERS": (
        NETWORK,
        {
            "name": "detected updated network parameters",
            "data": [
                {
                    "api": "update",
                    "records": [{"ip": "192.168.1.100"}],
                    "type": "modify",
                }
            ],
            "uniqueId": None,
        },
    ),
    # ── Channel Meters: distinct topic for completeness ────────────
    "CHANNEL_METERS_FRAME": (
        CHANNEL_METERS,
        {
            "name": "Channel Meters",
            "data": [
                {
                    "api": "update",
                    "records": [0] * 24,
                    "type": "modify",
                }
            ],
            "uniqueId": None,
        },
    ),
}


__all__ = ["SAMPLE_EVENTS"]
