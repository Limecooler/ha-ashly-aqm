"""Unit tests for the AshlyClient."""

from __future__ import annotations

import json
from typing import Any

import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.ashly.client import (
    AshlyApiError,
    AshlyAuthError,
    AshlyClient,
    AshlyConnectionError,
)

BASE = "http://192.168.1.100:8000/v1.0-beta"


def envelope(data: Any) -> dict[str, Any]:
    """Wrap `data` in the device's `{success, data}` response shape."""
    return {"success": True, "data": data}


@pytest.fixture
async def session():
    # Force the threaded (loop.getaddrinfo) resolver so we don't initialise
    # pycares — its background thread trips HA's verify_cleanup fixture.
    resolver = aiohttp.ThreadedResolver()
    s = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            resolver=resolver, force_close=True, enable_cleanup_closed=False
        ),
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    )
    yield s
    await s.close()


@pytest.fixture
async def client(session):
    return AshlyClient(
        host="192.168.1.100",
        port=8000,
        session=session,
        username="admin",
        password="secret",
    )


# ── login ───────────────────────────────────────────────────────────────


async def test_login_success(client):
    with aioresponses() as m:
        m.post(f"{BASE}/session/login", status=200, payload=envelope([{"loggedIn": True}]))
        await client.async_login()


async def test_login_unauthorized_raises_auth_error(client):
    with aioresponses() as m:
        m.post(f"{BASE}/session/login", status=401, body='"Authentication failed"')
        with pytest.raises(AshlyAuthError):
            await client.async_login()


async def test_login_bad_request_treated_as_auth_error(client):
    """Real device returns HTTP 400 when the password fails its alphanumeric
    schema (e.g. user typed a hyphen). That's still a credential problem,
    so it must surface as `AshlyAuthError` so HA's reauth flow fires."""
    with aioresponses() as m:
        m.post(
            f"{BASE}/session/login",
            status=400,
            payload={
                "statusCode": 400,
                "error": "Bad Request",
                "message": "Invalid request payload input",
            },
        )
        with pytest.raises(AshlyAuthError):
            await client.async_login()


async def test_login_connection_error(client):
    with aioresponses() as m:
        m.post(f"{BASE}/session/login", exception=aiohttp.ClientError("boom"))
        with pytest.raises(AshlyConnectionError):
            await client.async_login()


async def test_login_unexpected_status_raises_api_error(client):
    with aioresponses() as m:
        m.post(f"{BASE}/session/login", status=500, body="boom")
        with pytest.raises(AshlyApiError):
            await client.async_login()


# ── system_info / network ────────────────────────────────────────────────


async def test_get_system_info_combines_sys_and_network(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/system/info",
            payload=envelope(
                [
                    {
                        "name": "Living Room",
                        "deviceType": "AQM1208",
                        "softwareRevision": "1.1.8",
                        "hardwareRevision": "1.0.0",
                        "hasAutoMix": True,
                    }
                ]
            ),
        )
        m.get(
            f"{BASE}/network",
            payload=envelope([{"hwaddress": "00:14:aa:00:00:01"}]),
        )
        info = await client.async_get_system_info()
    assert info.model == "AQM1208"
    assert info.name == "Living Room"
    assert info.firmware_version == "1.1.8"
    assert info.hardware_revision == "1.0.0"
    assert info.mac_address == "00:14:aa:00:00:01"
    assert info.has_auto_mix is True


async def test_test_connection_returns_false_on_error(client):
    with aioresponses() as m:
        m.get(f"{BASE}/system/info", exception=aiohttp.ClientError("nope"))
        m.get(f"{BASE}/network", exception=aiohttp.ClientError("nope"))
        assert await client.async_test_connection() is False


# ── power ───────────────────────────────────────────────────────────────


async def test_get_power_on(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/system/frontPanel/info",
            payload=envelope([{"powerState": "On"}]),
        )
        assert await client.async_get_power() is True


async def test_get_power_off(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/system/frontPanel/info",
            payload=envelope([{"powerState": "Off"}]),
        )
        assert await client.async_get_power() is False


async def test_set_power_sends_string_enum(client):
    with aioresponses() as m:
        m.post(f"{BASE}/system/frontPanel/info", payload=envelope({}))
        await client.async_set_power(True)
        request = next(iter(m.requests.values()))[0]
        body = (
            json.loads(request.kwargs["json"])
            if isinstance(request.kwargs.get("json"), str)
            else request.kwargs.get("json")
        )
        assert body == {"powerState": "On"}


# ── channels ────────────────────────────────────────────────────────────


async def test_get_channels_parses_topology(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/dsp/channel",
            payload=envelope(
                [
                    {
                        "id": "InputChannel.1",
                        "name": "Mic 1",
                        "defaultName": "Mic/Line 1",
                        "baseType": "Input",
                        "channelNumber": 1,
                    },
                    {
                        "id": "OutputChannel.2",
                        "name": "Speaker B",
                        "defaultName": "Line Out 2",
                        "baseType": "Output",
                        "channelNumber": 2,
                    },
                ]
            ),
        )
        channels = await client.async_get_channels()
    assert {c.channel_id for c in channels} == {"InputChannel.1", "OutputChannel.2"}
    by_id = {c.channel_id: c for c in channels}
    assert by_id["InputChannel.1"].name == "Mic 1"
    assert by_id["OutputChannel.2"].base_type == "Output"


async def test_get_channels_skips_malformed_entries(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/dsp/channel",
            payload=envelope(
                [
                    "not-a-dict",
                    {"id": "InputChannel.1", "baseType": "Input", "channelNumber": 1},
                ]
            ),
        )
        channels = await client.async_get_channels()
    assert len(channels) == 1


# ── chain state ─────────────────────────────────────────────────────────


async def test_get_chain_state_normalizes_none_mixer(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/dsp/chain",
            payload=envelope(
                [
                    {"id": "InputChannel.1", "muted": False, "mixerId": None},
                    {"id": "OutputChannel.1", "muted": True, "mixerId": "None"},
                    {"id": "OutputChannel.2", "muted": False, "mixerId": "Mixer.3"},
                ]
            ),
        )
        chains = await client.async_get_chain_state()
    assert chains["InputChannel.1"].mixer_id is None
    assert chains["OutputChannel.1"].mixer_id is None
    assert chains["OutputChannel.1"].muted is True
    assert chains["OutputChannel.2"].mixer_id == "Mixer.3"


async def test_set_chain_mute_posts_correct_path(client):
    with aioresponses() as m:
        m.post(
            f"{BASE}/workingsettings/dsp/chain/mute/InputChannel.5",
            payload=envelope({}),
        )
        await client.async_set_chain_mute("InputChannel.5", True)


async def test_set_output_mixer_posts_correct_path(client):
    with aioresponses() as m:
        m.post(
            f"{BASE}/workingsettings/dsp/chain/mixer/OutputChannel.2",
            payload=envelope({}),
        )
        await client.async_set_output_mixer("OutputChannel.2", "Mixer.3")


# ── DVCA ────────────────────────────────────────────────────────────────


async def test_get_dvca_state_groups_levels_mutes_names(client):
    params = []
    for i in range(1, 13):
        params.extend(
            [
                {
                    "DSPParameterTypeId": "Virtual DCA.Level",
                    "index": i,
                    "value": -10.0 + i,
                },
                {
                    "DSPParameterTypeId": "Virtual DCA.Mute",
                    "index": i,
                    "value": i % 2 == 0,
                },
                {
                    "DSPParameterTypeId": "Virtual DCA.Name",
                    "index": i,
                    "value": f"Zone {i}",
                },
            ]
        )
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/virtualDVCA/parameters",
            payload=envelope(params),
        )
        dvca = await client.async_get_dvca_state()
    assert len(dvca) == 12
    assert dvca[1].level_db == -9.0
    assert dvca[2].muted is True
    assert dvca[3].name == "Zone 3"


async def test_set_dvca_level_path(client):
    with aioresponses() as m:
        m.post(
            f"{BASE}/workingsettings/virtualDVCA/parameters/DCAChannel.4.Level",
            payload=envelope({}),
        )
        await client.async_set_dvca_level(4, -3.0)


async def test_set_dvca_mute_path(client):
    with aioresponses() as m:
        m.post(
            f"{BASE}/workingsettings/virtualDVCA/parameters/DCAChannel.7.Mute",
            payload=envelope({}),
        )
        await client.async_set_dvca_mute(7, True)


# ── crosspoints ─────────────────────────────────────────────────────────


async def test_get_crosspoints_pairs_levels_and_mutes(client):
    payload = [
        {
            "DSPMixerConfigParameterTypeId": "Mixer.Source Level",
            "DSPMixerConfigId": "Mixer.1",
            "channelId": "InputChannel.3",
            "value": -6.5,
        },
        {
            "DSPMixerConfigParameterTypeId": "Mixer.Source Mute",
            "DSPMixerConfigId": "Mixer.1",
            "channelId": "InputChannel.3",
            "value": False,
        },
        {
            "DSPMixerConfigParameterTypeId": "Mixer.Source Enabled",
            "DSPMixerConfigId": "Mixer.1",
            "channelId": "InputChannel.3",
            "value": True,
        },
    ]
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/dsp/mixer/config/parameter",
            payload=envelope(payload),
        )
        xp = await client.async_get_crosspoints()
    assert xp[(1, 3)].level_db == -6.5
    assert xp[(1, 3)].muted is False
    # Untouched defaults: 96 entries total (8 * 12)
    assert len(xp) == 96
    assert xp[(8, 12)].muted is True  # default sentinel


async def test_set_crosspoint_level_path(client):
    url = f"{BASE}/workingsettings/dsp/mixer/config/parameter/Mixer.2.InputChannel.5.Source Level"
    with aioresponses() as m:
        m.post(url, payload=envelope({}))
        await client.async_set_crosspoint_level(2, 5, -12.0)


async def test_set_crosspoint_mute_path(client):
    with aioresponses() as m:
        m.post(
            f"{BASE}/workingsettings/dsp/mixer/config/parameter/Mixer.2.InputChannel.5.Source Mute",
            payload=envelope({}),
        )
        await client.async_set_crosspoint_mute(2, 5, True)


# ── presets ─────────────────────────────────────────────────────────────


async def test_get_presets_parses_list(client):
    """The device uses the preset name as the `id` field — a string, not int."""
    with aioresponses() as m:
        m.get(
            f"{BASE}/preset",
            payload=envelope(
                [
                    {"id": "Day", "name": "Day", "type": "Preset"},
                    {"id": "Night", "name": "Night", "type": "Preset"},
                    "garbage",
                ]
            ),
        )
        presets = await client.async_get_presets()
    assert [p.name for p in presets] == ["Day", "Night"]
    # IDs are strings (the device keys presets by name).
    assert all(isinstance(p.id, str) for p in presets)


async def test_get_presets_raises_on_non_list(client):
    with aioresponses() as m:
        m.get(f"{BASE}/preset", payload=envelope({}))
        with pytest.raises(AshlyApiError):
            await client.async_get_presets()


# ── error envelopes / re-auth ───────────────────────────────────────────


async def test_request_retries_after_401(client):
    """A first 401 triggers re-auth and the request is retried successfully."""
    with aioresponses() as m:
        m.get(f"{BASE}/preset", status=401)
        m.post(f"{BASE}/session/login", payload=envelope([{"loggedIn": True}]))
        m.get(f"{BASE}/preset", payload=envelope([{"id": 1, "name": "P"}]))
        presets = await client.async_get_presets()
    assert presets[0].name == "P"


async def test_envelope_failure_raises_api_error(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/preset",
            payload={"success": False, "error": "boom"},
        )
        with pytest.raises(AshlyApiError):
            await client.async_get_presets()


async def test_404_raises_api_error(client):
    with aioresponses() as m:
        m.get(f"{BASE}/preset", status=404)
        with pytest.raises(AshlyApiError):
            await client.async_get_presets()


# Note: AshlyClient does not own the session lifecycle (it's HA-managed via
# `async_create_clientsession`), so there is no `async_close` method to test.


# ── ID validation ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_id",
    [
        "InputChannel.1/../etc/passwd",
        "InputChannel..1",
        "Input Channel.1",
        "InputChannel.",
        "InputChannel.x",
        "OutputChannel.99999",
    ],
)
async def test_set_chain_mute_rejects_bad_channel_id(client, bad_id):
    with pytest.raises(ValueError):
        await client.async_set_chain_mute(bad_id, True)


@pytest.mark.parametrize(
    "bad_mixer",
    ["mixer.1", "Mixer.99999999", "Mixer.1.Sub", "garbage"],
)
async def test_set_output_mixer_rejects_bad_mixer_id(client, bad_mixer):
    with pytest.raises(ValueError):
        await client.async_set_output_mixer("OutputChannel.1", bad_mixer)


@pytest.mark.parametrize("bad_idx", [0, -1, 99])
async def test_set_dvca_level_rejects_out_of_range_index(client, bad_idx):
    with pytest.raises(ValueError):
        await client.async_set_dvca_level(bad_idx, 0.0)


@pytest.mark.parametrize("bad", [(0, 1), (1, 0), (99, 1), (1, 99)])
async def test_set_crosspoint_rejects_out_of_range(client, bad):
    m, i = bad
    with pytest.raises(ValueError):
        await client.async_set_crosspoint_level(m, i, 0.0)


# ── Boolean / numeric coercion ─────────────────────────────────────────


async def test_chain_mute_handles_string_boolean(client):
    """Device occasionally returns 'true'/'false' strings; we should normalise."""
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/dsp/chain",
            payload=envelope(
                [
                    {"id": "InputChannel.1", "muted": "false", "mixerId": None},
                    {"id": "InputChannel.2", "muted": "true", "mixerId": None},
                ]
            ),
        )
        chains = await client.async_get_chain_state()
    assert chains["InputChannel.1"].muted is False
    assert chains["InputChannel.2"].muted is True


async def test_dvca_handles_string_numeric_values(client):
    payload = [
        {"DSPParameterTypeId": "Virtual DCA.Level", "index": 1, "value": "-3.5"},
        {"DSPParameterTypeId": "Virtual DCA.Mute", "index": 1, "value": "true"},
    ]
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/virtualDVCA/parameters",
            payload=envelope(payload),
        )
        dvca = await client.async_get_dvca_state()
    assert dvca[1].level_db == -3.5
    assert dvca[1].muted is True


# ── Re-auth dedupe via auth epoch ──────────────────────────────────────


async def test_auth_epoch_increments_on_login(client):
    with aioresponses() as m:
        m.post(f"{BASE}/session/login", payload=envelope([{"loggedIn": True}]))
        before = client._auth_epoch
        await client.async_login()
        assert client._auth_epoch == before + 1


async def test_login_no_op_when_epoch_already_advanced(client):
    """If another caller already logged in, expected_epoch protects us."""
    with aioresponses() as m:
        # Only one login response is configured; if the second call hits the
        # network the test fails with an unmatched request error.
        m.post(f"{BASE}/session/login", payload=envelope([{"loggedIn": True}]))
        await client.async_login()
        snapshot = client._auth_epoch - 1
        await client.async_login(expected_epoch=snapshot)  # no-op
    assert client._auth_epoch == snapshot + 1


# ── _unwrap rejects null data ──────────────────────────────────────────


async def test_unwrap_rejects_null_data(client):
    with aioresponses() as m:
        m.get(f"{BASE}/preset", payload={"success": True, "data": None})
        with pytest.raises(AshlyApiError):
            await client.async_get_presets()


# ── identify ────────────────────────────────────────────────────────────


async def test_identify(client):
    with aioresponses() as m:
        m.get(f"{BASE}/system/identify", payload=envelope({}))
        await client.async_identify()


# ── Front panel (LED + power) ──────────────────────────────────────────


async def test_get_front_panel_combines_power_and_leds(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/system/frontPanel/info",
            payload=envelope([{"powerState": "On", "frontPanelLEDEnable": False}]),
        )
        info = await client.async_get_front_panel()
    assert info.power_on is True
    assert info.leds_enabled is False


async def test_set_front_panel_leds_does_not_touch_power(client):
    """The LED setter must POST only `frontPanelLEDEnable` so power is preserved."""
    with aioresponses() as m:
        m.post(f"{BASE}/system/frontPanel/info", payload=envelope({}))
        await client.async_set_front_panel_leds(False)
        request = next(iter(m.requests.values()))[0]
        assert request.kwargs.get("json") == {"frontPanelLEDEnable": False}


# ── Phantom power ──────────────────────────────────────────────────────


async def test_get_phantom_power_returns_dict(client):
    payload = [
        {"id": i, "DSPChannelId": f"InputChannel.{i}", "phantomPowerEnabled": i % 2 == 0}
        for i in range(1, 13)
    ]
    with aioresponses() as m:
        m.get(f"{BASE}/phantomPower", payload=envelope(payload))
        result = await client.async_get_phantom_power()
    assert result[1] is False
    assert result[2] is True
    assert len(result) == 12


async def test_set_phantom_power_path_and_body(client):
    with aioresponses() as m:
        m.post(f"{BASE}/phantomPower/3", payload=envelope({}))
        await client.async_set_phantom_power(3, True)
        body = next(iter(m.requests.values()))[0].kwargs.get("json")
        assert body == {"phantomPowerEnabled": True}


@pytest.mark.parametrize("bad", [0, -1, 99])
async def test_set_phantom_power_rejects_bad_input(client, bad):
    with pytest.raises(ValueError):
        await client.async_set_phantom_power(bad, True)


# ── Mic preamp gain ────────────────────────────────────────────────────


async def test_get_mic_preamp_returns_dict(client):
    payload = [
        {"id": i, "DSPChannelId": f"InputChannel.{i}", "gain": (i - 1) * 6} for i in range(1, 13)
    ]
    with aioresponses() as m:
        m.get(f"{BASE}/micPreamp", payload=envelope(payload))
        result = await client.async_get_mic_preamp()
    assert result[1] == 0
    assert result[12] == 66
    assert len(result) == 12


async def test_set_mic_preamp_path_and_body(client):
    with aioresponses() as m:
        m.post(f"{BASE}/micPreamp/5", payload=envelope({}))
        await client.async_set_mic_preamp(5, 24)
        body = next(iter(m.requests.values()))[0].kwargs.get("json")
        assert body == {"gain": 24}


# ── GPO ────────────────────────────────────────────────────────────────


async def test_get_gpo_returns_dict(client):
    payload = [
        {
            "id": "General Purpose Output Pin.1",
            "value": "high",
            "generalPurposeOutputId": 1,
        },
        {
            "id": "General Purpose Output Pin.2",
            "value": "low",
            "generalPurposeOutputId": 2,
        },
    ]
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/generalPurposeOutputConfiguration",
            payload=envelope(payload),
        )
        result = await client.async_get_gpo()
    assert result == {1: True, 2: False}


async def test_set_gpo_url_encodes_pin_id_and_sends_value(client):
    url = (
        f"{BASE}/workingsettings/generalPurposeOutputConfiguration/"
        "General%20Purpose%20Output%20Pin.2"
    )
    with aioresponses() as m:
        m.post(url, payload=envelope({}))
        await client.async_set_gpo(2, True)
        body = next(iter(m.requests.values()))[0].kwargs.get("json")
        assert body == {"value": "high"}


@pytest.mark.parametrize("bad", [0, 3])
async def test_set_gpo_rejects_out_of_range_pin(client, bad):
    with pytest.raises(ValueError):
        await client.async_set_gpo(bad, True)


# ── Last recalled preset ───────────────────────────────────────────────


async def test_get_last_recalled_returns_none_for_string_none(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/preset/lastRecalled",
            payload=envelope([{"lastRecalledPreset": "None", "modified": False}]),
        )
        info = await client.async_get_last_recalled_preset()
    assert info.name is None
    assert info.modified is False


async def test_get_last_recalled_returns_preset_name(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/preset/lastRecalled",
            payload=envelope([{"lastRecalledPreset": "Evening Mode", "modified": True}]),
        )
        info = await client.async_get_last_recalled_preset()
    assert info.name == "Evening Mode"
    assert info.modified is True


# ── recall_preset ──────────────────────────────────────────────────────


async def test_recall_preset_posts_correct_path(client):
    with aioresponses() as m:
        m.post(f"{BASE}/preset/recall/Evening", payload=envelope({}))
        await client.async_recall_preset("Evening")


async def test_recall_preset_url_encodes_name(client):
    """Preset names with spaces or unicode must be URL-encoded in the path."""
    with aioresponses() as m:
        m.post(f"{BASE}/preset/recall/Evening%20Mode", payload=envelope({}))
        await client.async_recall_preset("Evening Mode")


@pytest.mark.parametrize("bad", ["", None])
async def test_recall_preset_rejects_empty_name(client, bad):
    with pytest.raises(ValueError):
        await client.async_recall_preset(bad)  # type: ignore[arg-type]


async def test_recall_preset_404_surfaces_as_api_error(client):
    """A non-existent preset returns 422; bubbles up as AshlyApiError."""
    with aioresponses() as m:
        m.post(
            f"{BASE}/preset/recall/nope",
            status=422,
            payload={
                "statusCode": 422,
                "error": "Unprocessable Entity",
                "message": "Error, a Preset with the name: nope does not exist",
            },
        )
        with pytest.raises(AshlyApiError):
            await client.async_recall_preset("nope")


# ── Login envelope validation ──────────────────────────────────────────


async def test_login_silent_failure_envelope_raises_auth_error(client):
    """A 200 response with `success: false` must NOT advance the auth epoch.

    Otherwise a misbehaving device could trap callers in an infinite
    401-then-fake-success loop.
    """
    with aioresponses() as m:
        m.post(
            f"{BASE}/session/login",
            status=200,
            payload={"success": False, "error": "session locked"},
        )
        before = client._auth_epoch
        with pytest.raises(AshlyAuthError):
            await client.async_login()
    assert client._auth_epoch == before


# ── _request retry path ────────────────────────────────────────────────


async def test_request_retry_then_401_raises_auth_error(client):
    """If retry after re-auth still returns 401, raise — don't loop."""
    with aioresponses() as m:
        m.get(f"{BASE}/preset", status=401)
        m.post(f"{BASE}/session/login", payload=envelope([{"loggedIn": True}]))
        m.get(f"{BASE}/preset", status=401)
        with pytest.raises(AshlyAuthError):
            await client.async_get_presets()


async def test_request_retry_when_login_fails_propagates_auth_error(client):
    """If re-auth itself fails, that error reaches the caller."""
    with aioresponses() as m:
        m.get(f"{BASE}/preset", status=401)
        m.post(f"{BASE}/session/login", status=401, body='"bad creds"')
        with pytest.raises(AshlyAuthError):
            await client.async_get_presets()


# ── Pure-helper edge cases (no HTTP) ───────────────────────────────────


def test_to_bool_handles_each_branch():
    from custom_components.ashly.client import _to_bool

    assert _to_bool(True) is True
    assert _to_bool(False) is False
    assert _to_bool("YES") is True
    assert _to_bool("0") is False
    # None branch
    assert _to_bool(None, default=True) is True
    assert _to_bool(None, default=False) is False
    # Fallback for non-bool / non-str / non-None
    assert _to_bool(1) is True
    assert _to_bool(0) is False


def test_to_float_handles_each_branch():
    from custom_components.ashly.client import _to_float

    assert _to_float(3.0) == 3.0
    assert _to_float(2) == 2.0
    assert _to_float("4.5") == 4.5
    # ValueError → default
    assert _to_float("not-a-number", default=-1.0) == -1.0
    # Unrecognised type → default
    assert _to_float({"a": 1}, default=-2.0) == -2.0


def test_unwrap_rejects_non_dict():
    from custom_components.ashly.client import AshlyClient

    with pytest.raises(AshlyApiError, match="not an object"):
        AshlyClient._unwrap("not a dict")


def test_unwrap_rejects_missing_data_field():
    from custom_components.ashly.client import AshlyClient

    with pytest.raises(AshlyApiError, match="missing 'data' field"):
        AshlyClient._unwrap({"success": True})


def test_unwrap_static_rejects_null_data():
    """Direct (no-HTTP) call to _unwrap with data=null."""
    from custom_components.ashly.client import AshlyClient

    with pytest.raises(AshlyApiError, match="data: null"):
        AshlyClient._unwrap({"success": True, "data": None})


def test_first_or_empty_handles_each_shape():
    from custom_components.ashly.client import AshlyClient

    assert AshlyClient._first_or_empty([{"a": 1}, {"b": 2}]) == {"a": 1}
    assert AshlyClient._first_or_empty([]) == {}
    assert AshlyClient._first_or_empty({"a": 1}) == {"a": 1}
    assert AshlyClient._first_or_empty("oops") == {}
    assert AshlyClient._first_or_empty(42) == {}


# ── HTTP parse / error-body branches ───────────────────────────────────


async def test_parse_json_invalid_json_raises_api_error(client):
    """Non-JSON body content surfaces as AshlyApiError."""
    with aioresponses() as m:
        m.get(f"{BASE}/preset", status=200, body="<html>not json</html>")
        with pytest.raises(AshlyApiError, match="Invalid JSON response"):
            await client.async_get_presets()


async def test_test_connection_returns_true_on_success(client):
    """async_test_connection wraps async_get_system_info; success path."""
    with aioresponses() as m:
        m.get(
            f"{BASE}/system/info",
            payload=envelope([{"name": "x", "model": "y", "softwareRevision": "z"}]),
        )
        m.get(
            f"{BASE}/network",
            payload=envelope([{"hwaddress": "00:14:aa:11:22:33"}]),
        )
        assert await client.async_test_connection() is True


# ── Channel/chain parser edge cases ────────────────────────────────────


async def test_get_channels_raises_when_not_a_list(client):
    with aioresponses() as m:
        m.get(f"{BASE}/workingsettings/dsp/channel", payload=envelope({"oops": "dict"}))
        with pytest.raises(AshlyApiError, match="channel list shape"):
            await client.async_get_channels()


async def test_get_channels_skips_non_dict_entries(client):
    """Non-dict entries are logged-and-skipped, not raised."""
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/dsp/channel",
            payload=envelope(
                [
                    "not-a-dict",
                    {
                        "id": "InputChannel.1",
                        "name": "in1",
                        "defaultName": "Input 1",
                        "baseType": "Input",
                        "channelNumber": 1,
                    },
                ]
            ),
        )
        chans = await client.async_get_channels()
    assert len(chans) == 1
    assert chans[0].channel_id == "InputChannel.1"


async def test_get_channels_skips_malformed_entry_keyerror(client):
    """A dict entry missing required fields is skipped (KeyError path)."""
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/dsp/channel",
            payload=envelope(
                [
                    {"defaultName": "missing id"},  # no 'id' key → KeyError
                ]
            ),
        )
        chans = await client.async_get_channels()
    assert chans == []


async def test_get_chain_state_raises_when_not_a_list(client):
    with aioresponses() as m:
        m.get(f"{BASE}/workingsettings/dsp/chain", payload=envelope({"oops": "dict"}))
        with pytest.raises(AshlyApiError, match="chain list shape"):
            await client.async_get_chain_state()


async def test_get_chain_state_skips_non_dict_entries(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/dsp/chain",
            payload=envelope(
                [
                    "not-a-dict",
                    {"no": "id"},  # missing id → skipped
                    {"id": "InputChannel.1", "muted": True},
                ]
            ),
        )
        chains = await client.async_get_chain_state()
    assert list(chains) == ["InputChannel.1"]


# ── DVCA parser edge cases ─────────────────────────────────────────────


async def test_get_dvca_raises_when_not_a_list(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/virtualDVCA/parameters",
            payload=envelope({"oops": "dict"}),
        )
        with pytest.raises(AshlyApiError, match="DVCA parameter shape"):
            await client.async_get_dvca_state()


async def test_get_dvca_skips_bad_entries(client):
    """Non-dicts, missing index, non-int index, out-of-range index — all skipped."""
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/virtualDVCA/parameters",
            payload=envelope(
                [
                    "not-a-dict",
                    {"DSPParameterTypeId": "Virtual DCA.Level"},  # no index
                    {"DSPParameterTypeId": "Virtual DCA.Level", "index": "abc"},  # not int
                    {"DSPParameterTypeId": "Virtual DCA.Level", "index": 999},  # out of range
                    # One valid entry to confirm parser still produces output
                    {
                        "DSPParameterTypeId": "Virtual DCA.Level",
                        "index": 1,
                        "value": -3.0,
                    },
                ]
            ),
        )
        dvca = await client.async_get_dvca_state()
    assert dvca[1].level_db == -3.0


# ── Crosspoint parser edge cases ───────────────────────────────────────


async def test_get_crosspoints_raises_when_not_a_list(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/dsp/mixer/config/parameter",
            payload=envelope({"oops": "dict"}),
        )
        with pytest.raises(AshlyApiError, match="mixer parameter shape"):
            await client.async_get_crosspoints()


async def test_get_crosspoints_skips_bad_entries(client):
    """Non-dicts, unknown type ids, unparseable ids, out-of-range — all skipped."""
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/dsp/mixer/config/parameter",
            payload=envelope(
                [
                    "not-a-dict",
                    {"DSPMixerConfigParameterTypeId": "Some.Other.Param"},  # wrong type
                    {
                        "DSPMixerConfigParameterTypeId": "Mixer.Source Level",
                        "DSPMixerConfigId": "Mixer.bad",  # split[-1]="bad" → ValueError
                        "channelId": "InputChannel.1",
                        "value": -3.0,
                    },
                    {
                        "DSPMixerConfigParameterTypeId": "Mixer.Source Level",
                        "DSPMixerConfigId": "Mixer.99",  # out of range
                        "channelId": "InputChannel.1",
                        "value": -3.0,
                    },
                ]
            ),
        )
        cps = await client.async_get_crosspoints()
    # Even with all bad entries, the parser returns the default-filled matrix.
    assert isinstance(cps, dict)
    assert len(cps) > 0  # NUM_MIXERS * NUM_INPUTS defaults


# ── Preset parser edge cases ───────────────────────────────────────────


async def test_get_presets_skips_non_dict_and_nameless(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/preset",
            payload=envelope(
                [
                    "not-a-dict",
                    {"id": ""},  # empty name → skipped
                    {"id": "Real", "name": "Real"},
                ]
            ),
        )
        presets = await client.async_get_presets()
    assert [p.name for p in presets] == ["Real"]


# ── Phantom / mic-preamp / GPO parser edge cases ───────────────────────


async def test_get_phantom_power_raises_when_not_a_list(client):
    with aioresponses() as m:
        m.get(f"{BASE}/phantomPower", payload=envelope({"oops": "dict"}))
        with pytest.raises(AshlyApiError, match="phantom-power list shape"):
            await client.async_get_phantom_power()


async def test_get_phantom_power_skips_bad_entries(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/phantomPower",
            payload=envelope(
                [
                    "not-a-dict",
                    {"no": "id"},  # KeyError on item["id"]
                    {"id": "abc"},  # ValueError on int conversion
                    {"id": 99},  # out of range
                    {"id": 1, "phantomPowerEnabled": True},
                ]
            ),
        )
        pp = await client.async_get_phantom_power()
    assert pp == {1: True}


async def test_get_mic_preamp_raises_when_not_a_list(client):
    with aioresponses() as m:
        m.get(f"{BASE}/micPreamp", payload=envelope({"oops": "dict"}))
        with pytest.raises(AshlyApiError, match="mic-preamp list shape"):
            await client.async_get_mic_preamp()


async def test_get_mic_preamp_skips_bad_entries_and_handles_bad_gain(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/micPreamp",
            payload=envelope(
                [
                    "not-a-dict",
                    {"no": "id"},  # KeyError
                    {"id": "abc"},  # ValueError
                    {"id": 99},  # out of range — skipped silently
                    {"id": 1, "gain": "not-an-int"},  # gain parse error → 0
                    {"id": 2, "gain": 24},
                ]
            ),
        )
        pp = await client.async_get_mic_preamp()
    assert pp == {1: 0, 2: 24}


async def test_get_gpo_raises_when_not_a_list(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/generalPurposeOutputConfiguration",
            payload=envelope({"oops": "dict"}),
        )
        with pytest.raises(AshlyApiError, match="GPO list shape"):
            await client.async_get_gpo()


async def test_get_gpo_skips_bad_entries(client):
    with aioresponses() as m:
        m.get(
            f"{BASE}/workingsettings/generalPurposeOutputConfiguration",
            payload=envelope(
                [
                    "not-a-dict",
                    {"generalPurposeOutputId": "abc"},  # ValueError
                    {"generalPurposeOutputId": 99},  # out of range
                    {"generalPurposeOutputId": 1, "value": "high"},
                ]
            ),
        )
        gpo = await client.async_get_gpo()
    assert gpo == {1: True}


# ── Body-read failure paths in _parse_json + _request_once ─────────────


async def test_parse_json_body_read_failure_yields_placeholder(client):
    """When the JSON-decode fails and the text() fallback also fails, the
    error message uses '<unable to read body>'.

    Exercised directly because aioresponses doesn't let us simulate text() raising.
    """
    from unittest.mock import AsyncMock, MagicMock

    resp = MagicMock()
    resp.json = AsyncMock(side_effect=aiohttp.ContentTypeError(MagicMock(), tuple()))
    resp.text = AsyncMock(side_effect=aiohttp.ClientError("conn dropped"))
    with pytest.raises(AshlyApiError, match="unable to read body"):
        await client._parse_json(resp)


async def test_request_once_error_body_read_failure_yields_placeholder(client):
    """If the HTTP response is an error AND text() raises, the AshlyApiError
    message includes the '<unable to read body>' sentinel.
    """
    import contextlib
    from unittest.mock import AsyncMock, MagicMock

    fake_resp = MagicMock()
    fake_resp.status = 500
    fake_resp.text = AsyncMock(side_effect=aiohttp.ClientError("conn dropped"))

    @contextlib.asynccontextmanager
    async def fake_request(*a, **kw):
        yield fake_resp

    with (
        patch_object_request(client, fake_request),
        pytest.raises(AshlyApiError, match="unable to read body"),
    ):
        await client._request_once("GET", "/whatever")


async def test_request_defensive_double_401_raises_auth_error(client):
    """If retry_auth=False call still flags retry_again=True (shouldn't normally
    happen), the _request method raises the defensive AshlyAuthError.
    """
    from unittest.mock import AsyncMock

    calls = {"n": 0}

    async def fake_once(method, path, *, json=None, retry_auth=True):
        calls["n"] += 1
        # Both calls return need_retry=True regardless of retry_auth — simulates
        # a bug where the flag is ignored.
        return None, True, 0

    client._request_once = AsyncMock(side_effect=fake_once)
    client.async_login = AsyncMock()
    with pytest.raises(AshlyAuthError, match="Re-auth did not unblock"):
        await client._request("GET", "/whatever")
    assert calls["n"] == 2


# Helper used by test_request_once_error_body_read_failure_yields_placeholder
def patch_object_request(client, replacement):
    """Replace client._session.request with `replacement`, restore on exit."""
    import contextlib

    @contextlib.contextmanager
    def _swap():
        original = client._session.request
        client._session.request = replacement
        try:
            yield
        finally:
            client._session.request = original

    return _swap()
