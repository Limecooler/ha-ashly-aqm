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
