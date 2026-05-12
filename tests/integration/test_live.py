"""End-to-end tests against a real Ashly AQM device.

These tests **mutate device state** — they take the *last* output channel,
the *last* DCA group, and the highest-numbered crosspoint, then restore
the original value in a `try/finally`. They never toggle power.

Opt in with::

    ASHLY_HOST=192.168.18.114 pytest -m integration

Optional overrides: ``ASHLY_PORT``, ``ASHLY_USERNAME``, ``ASHLY_PASSWORD``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.ashly.client import (
    AshlyAuthError,
    AshlyClient,
    AshlyConnectionError,
    input_channel_id,
    output_channel_id,
)
from custom_components.ashly.const import (
    ASHLY_MAC_PREFIX,
    DVCA_LEVEL_MAX_DB,
    DVCA_LEVEL_MIN_DB,
    NO_MIXER,
    NUM_DVCA_GROUPS,
    NUM_INPUTS,
    NUM_MIXERS,
    NUM_OUTPUTS,
)
from custom_components.ashly.coordinator import AshlyCoordinator

# ── Read-only sanity checks ────────────────────────────────────────────


async def test_login_succeeds(live_client: AshlyClient) -> None:
    """The fixture already logged in; we just confirm `_authenticated`."""
    assert live_client._authenticated is True
    assert live_client._auth_epoch >= 1


async def test_system_info_has_ashly_mac(live_client: AshlyClient) -> None:
    info = await live_client.async_get_system_info()
    assert info.mac_address, "device returned no MAC address"
    compact = info.mac_address.replace(":", "").upper()
    assert compact.startswith(ASHLY_MAC_PREFIX), (
        f"MAC {info.mac_address!r} doesn't start with the Ashly OUI"
    )
    assert info.firmware_version, "device returned no firmware version"
    assert info.model.startswith("AQM"), f"unexpected model {info.model!r}"


async def test_channel_topology_matches_constants(live_client: AshlyClient) -> None:
    channels = await live_client.async_get_channels()
    inputs = [c for c in channels if c.base_type == "Input"]
    outputs = [c for c in channels if c.base_type == "Output"]
    # We don't hard-fail on count mismatch (mXa devices report different
    # totals), but we assert at least the AQM1208's expected pair.
    assert {c.channel_id for c in inputs} >= {
        input_channel_id(n) for n in range(1, NUM_INPUTS + 1)
    }
    assert {c.channel_id for c in outputs} >= {
        output_channel_id(n) for n in range(1, NUM_OUTPUTS + 1)
    }


async def test_chain_state_covers_all_channels(live_client: AshlyClient) -> None:
    chains = await live_client.async_get_chain_state()
    for n in range(1, NUM_INPUTS + 1):
        assert input_channel_id(n) in chains
    for n in range(1, NUM_OUTPUTS + 1):
        assert output_channel_id(n) in chains


async def test_dvca_state_complete(live_client: AshlyClient) -> None:
    dvca = await live_client.async_get_dvca_state()
    assert set(dvca) == set(range(1, NUM_DVCA_GROUPS + 1))


async def test_crosspoints_complete(live_client: AshlyClient) -> None:
    xp = await live_client.async_get_crosspoints()
    expected = {
        (m, i)
        for m in range(1, NUM_MIXERS + 1)
        for i in range(1, NUM_INPUTS + 1)
    }
    assert set(xp) == expected


async def test_power_state_readable(live_client: AshlyClient) -> None:
    # Just ensure the call doesn't raise; we don't toggle power here.
    state = await live_client.async_get_power()
    assert isinstance(state, bool)


async def test_presets_readable(live_client: AshlyClient) -> None:
    presets = await live_client.async_get_presets()
    assert isinstance(presets, list)


async def test_identify_does_not_raise(live_client: AshlyClient) -> None:
    await live_client.async_identify()


async def test_test_connection_returns_true(live_client: AshlyClient) -> None:
    """The convenience method used by the config flow should succeed when
    pointed at a real device with valid credentials."""
    assert await live_client.async_test_connection() is True


# ── Authentication / connectivity failure paths ────────────────────────


@pytest.mark.parametrize(
    "bad_password",
    [
        # Alphanumeric password the device validates and rejects (→ HTTP 401)
        "wrongpass",
        # Password the device's schema rejects outright (→ HTTP 400);
        # historically this was misclassified as AshlyApiError, hiding
        # the real reauth prompt from users with special chars.
        "not-the-real-password",
    ],
)
async def test_invalid_credentials_raises_auth_error(
    live_session: aiohttp.ClientSession,
    ashly_host: str,
    ashly_port: int,
    ashly_username: str,
    bad_password: str,
) -> None:
    """Both HTTP 401 (bad creds) and HTTP 400 (schema-invalid creds) from
    the real device must surface as `AshlyAuthError` so HA's reauth flow
    fires."""
    bad_client = AshlyClient(
        host=ashly_host,
        port=ashly_port,
        session=live_session,
        username=ashly_username,
        password=bad_password,
    )
    with pytest.raises(AshlyAuthError):
        await bad_client.async_login()


async def test_unreachable_host_raises_connection_error(
    live_session: aiohttp.ClientSession,
    ashly_host: str,
) -> None:
    """A login to a closed port must raise `AshlyConnectionError` quickly.

    Uses port 1 on the same host — guaranteed nothing listens there.
    Verifies the request timeout is honoured (test would hang if not).
    """
    bad_client = AshlyClient(
        host=ashly_host,
        port=1,
        session=live_session,
        username="admin",
        password="anything",
    )
    with pytest.raises(AshlyConnectionError):
        await asyncio.wait_for(bad_client.async_login(), timeout=15.0)


# ── State-mutating round-trips (always restored) ───────────────────────


async def test_chain_mute_round_trip(live_client: AshlyClient) -> None:
    """Toggle `OutputChannel.<NUM_OUTPUTS>` mute and restore it."""
    target_id = output_channel_id(NUM_OUTPUTS)
    chains = await live_client.async_get_chain_state()
    original = chains[target_id].muted
    try:
        await live_client.async_set_chain_mute(target_id, not original)
        observed = (await live_client.async_get_chain_state())[target_id].muted
        assert observed != original, "device did not honour mute toggle"
    finally:
        await live_client.async_set_chain_mute(target_id, original)
    restored = (await live_client.async_get_chain_state())[target_id].muted
    assert restored == original


async def test_dvca_level_round_trip(live_client: AshlyClient) -> None:
    """Set the last DCA's level to a known value and restore it."""
    target = NUM_DVCA_GROUPS
    dvca = await live_client.async_get_dvca_state()
    original = dvca[target].level_db
    test_value = -3.5 if original != -3.5 else -4.0
    try:
        await live_client.async_set_dvca_level(target, test_value)
        observed = (await live_client.async_get_dvca_state())[target].level_db
        assert observed == pytest.approx(test_value, abs=0.05)
    finally:
        await live_client.async_set_dvca_level(target, original)
    restored = (await live_client.async_get_dvca_state())[target].level_db
    assert restored == pytest.approx(original, abs=0.05)


async def test_dvca_mute_round_trip(live_client: AshlyClient) -> None:
    target = NUM_DVCA_GROUPS
    dvca = await live_client.async_get_dvca_state()
    original = dvca[target].muted
    try:
        await live_client.async_set_dvca_mute(target, not original)
        observed = (await live_client.async_get_dvca_state())[target].muted
        assert observed != original
    finally:
        await live_client.async_set_dvca_mute(target, original)


async def test_crosspoint_level_round_trip(live_client: AshlyClient) -> None:
    """Set crosspoint (last mixer, last input) level and restore it."""
    m, i = NUM_MIXERS, NUM_INPUTS
    xp = await live_client.async_get_crosspoints()
    original = xp[(m, i)].level_db
    test_value = -10.5 if original != -10.5 else -11.0
    try:
        await live_client.async_set_crosspoint_level(m, i, test_value)
        observed = (await live_client.async_get_crosspoints())[(m, i)].level_db
        assert observed == pytest.approx(test_value, abs=0.05)
    finally:
        await live_client.async_set_crosspoint_level(m, i, original)


async def test_crosspoint_mute_round_trip(live_client: AshlyClient) -> None:
    m, i = NUM_MIXERS, NUM_INPUTS
    xp = await live_client.async_get_crosspoints()
    original = xp[(m, i)].muted
    try:
        await live_client.async_set_crosspoint_mute(m, i, not original)
        observed = (await live_client.async_get_crosspoints())[(m, i)].muted
        assert observed != original
    finally:
        await live_client.async_set_crosspoint_mute(m, i, original)


async def test_output_mixer_round_trip(live_client: AshlyClient) -> None:
    """Assign `OutputChannel.<NUM_OUTPUTS>` to a different mixer, restore.

    Covers `async_set_output_mixer` (the select entity's setter). When the
    output is already routed (mixer_id != None) we restore to that value
    exactly; when unrouted we toggle to `Mixer.<NUM_MIXERS>` and back to
    the device's literal "None" sentinel.
    """
    target_id = output_channel_id(NUM_OUTPUTS)
    chains = await live_client.async_get_chain_state()
    original_mixer = chains[target_id].mixer_id  # None or "Mixer.N"

    # Pick a different mixer to assign during the test; pick the highest
    # available unless that's already current.
    alternate = f"Mixer.{NUM_MIXERS}"
    if original_mixer == alternate:
        alternate = f"Mixer.{NUM_MIXERS - 1}"

    restore_value = original_mixer or NO_MIXER  # device wants the literal "None"
    try:
        await live_client.async_set_output_mixer(target_id, alternate)
        observed = (await live_client.async_get_chain_state())[target_id].mixer_id
        assert observed == alternate, (
            f"expected {alternate!r}, got {observed!r}"
        )
    finally:
        await live_client.async_set_output_mixer(target_id, restore_value)
    restored = (await live_client.async_get_chain_state())[target_id].mixer_id
    assert restored == original_mixer


async def test_dvca_level_min_boundary(live_client: AshlyClient) -> None:
    """The device must accept the documented minimum level (-50.1 dB)."""
    target = NUM_DVCA_GROUPS
    original = (await live_client.async_get_dvca_state())[target].level_db
    try:
        await live_client.async_set_dvca_level(target, DVCA_LEVEL_MIN_DB)
        observed = (await live_client.async_get_dvca_state())[target].level_db
        assert observed == pytest.approx(DVCA_LEVEL_MIN_DB, abs=0.2)
    finally:
        await live_client.async_set_dvca_level(target, original)


async def test_dvca_level_max_boundary(live_client: AshlyClient) -> None:
    """The device must accept the documented maximum level (+12 dB)."""
    target = NUM_DVCA_GROUPS
    original = (await live_client.async_get_dvca_state())[target].level_db
    try:
        await live_client.async_set_dvca_level(target, DVCA_LEVEL_MAX_DB)
        observed = (await live_client.async_get_dvca_state())[target].level_db
        assert observed == pytest.approx(DVCA_LEVEL_MAX_DB, abs=0.2)
    finally:
        await live_client.async_set_dvca_level(target, original)


# ── Concurrency / stress ───────────────────────────────────────────────


async def test_concurrent_reads_share_session_cookie(live_client: AshlyClient) -> None:
    """8 concurrent reads (exceeds the 4-slot semaphore) must all succeed.

    Confirms that:
      - the cookie jar is correctly shared across in-flight requests
      - the semaphore throttles without deadlocking
      - the dataclass parsers are reentrant
    """
    tasks = [
        live_client.async_get_power(),
        live_client.async_get_chain_state(),
        live_client.async_get_dvca_state(),
        live_client.async_get_crosspoints(),
        live_client.async_get_presets(),
        live_client.async_get_channels(),
        live_client.async_get_system_info(),
        live_client.async_get_power(),
    ]
    results = await asyncio.gather(*tasks)
    assert len(results) == 8
    assert all(r is not None for r in results)


# ── Diagnostics from a live coordinator ────────────────────────────────


async def test_phantom_power_round_trip(live_client: AshlyClient) -> None:
    """Toggle phantom power on the last mic input and restore."""
    target = NUM_INPUTS
    state = await live_client.async_get_phantom_power()
    original = state[target]
    try:
        await live_client.async_set_phantom_power(target, not original)
        observed = (await live_client.async_get_phantom_power())[target]
        assert observed != original
    finally:
        await live_client.async_set_phantom_power(target, original)
    restored = (await live_client.async_get_phantom_power())[target]
    assert restored == original


async def test_mic_preamp_round_trip(live_client: AshlyClient) -> None:
    """Set the last input's preamp gain to 24 dB and restore.

    24 dB is a documented allowed step (0,6,12,...,66).
    """
    target = NUM_INPUTS
    state = await live_client.async_get_mic_preamp()
    original = state[target]
    test_value = 24 if original != 24 else 18
    try:
        await live_client.async_set_mic_preamp(target, test_value)
        observed = (await live_client.async_get_mic_preamp())[target]
        assert observed == test_value
    finally:
        await live_client.async_set_mic_preamp(target, original)
    restored = (await live_client.async_get_mic_preamp())[target]
    assert restored == original


async def test_gpo_round_trip(live_client: AshlyClient) -> None:
    """Drive GPO pin 2 high then restore. Pin 2 is the safer choice — many
    installs wire pin 1 to a paging/mute logic input."""
    state = await live_client.async_get_gpo()
    original = state[2]
    try:
        await live_client.async_set_gpo(2, not original)
        observed = (await live_client.async_get_gpo())[2]
        assert observed != original
    finally:
        await live_client.async_set_gpo(2, original)
    restored = (await live_client.async_get_gpo())[2]
    assert restored == original


async def test_front_panel_led_round_trip_preserves_power(
    live_client: AshlyClient,
) -> None:
    """Toggle front-panel LEDs and restore. The power state must be
    unchanged before, during, and after the test (regression test for
    accidentally clobbering powerState in the LED setter)."""
    info = await live_client.async_get_front_panel()
    original_leds = info.leds_enabled
    original_power = info.power_on
    try:
        await live_client.async_set_front_panel_leds(not original_leds)
        observed = await live_client.async_get_front_panel()
        assert observed.leds_enabled != original_leds
        assert observed.power_on == original_power, (
            "front-panel LED setter must not change powerState"
        )
    finally:
        await live_client.async_set_front_panel_leds(original_leds)
    restored = await live_client.async_get_front_panel()
    assert restored.leds_enabled == original_leds
    assert restored.power_on == original_power


async def test_preset_recall_round_trip(live_client: AshlyClient) -> None:
    """Create a test preset, recall it, then delete it.

    Confirms the cookie-auth recall endpoint works without a SimpleControl
    user. Cleans up the test preset in a `finally` to leave the device
    untouched.
    """
    import aiohttp as _aiohttp

    test_name = "ha_integration_recall_test"
    base = f"http://{live_client.host}:{live_client.port}/v1.0-beta"
    # Both create and delete are unique to this test, so we drive them
    # directly via the client's session rather than expanding the public
    # AshlyClient API just for tests.
    sess = live_client._session
    try:
        async with sess.post(f"{base}/preset/full", json={"name": test_name}) as r:
            assert r.status == 200, await r.text()
        presets = await live_client.async_get_presets()
        assert any(p.name == test_name for p in presets), (
            f"test preset not in list: {[p.name for p in presets]}"
        )

        # Capture lastRecalled before, then recall and verify it changed.
        before = await live_client.async_get_last_recalled_preset()
        await live_client.async_recall_preset(test_name)
        after = await live_client.async_get_last_recalled_preset()
        assert after.name == test_name, (
            f"lastRecalledPreset was {after.name!r}, expected {test_name!r}"
        )
        assert before.name != test_name or after.modified is False

        # Non-existent preset should surface as AshlyApiError.
        from custom_components.ashly.client import AshlyApiError

        with pytest.raises(AshlyApiError):
            await live_client.async_recall_preset("ha_does_not_exist_xyz")
    finally:
        try:
            async with sess.delete(f"{base}/preset/{test_name}") as r:
                # Tolerate 200 (deleted) or 422 (already gone).
                assert r.status in (200, 422), await r.text()
        except _aiohttp.ClientError:
            pass


async def test_last_recalled_preset_readable(live_client: AshlyClient) -> None:
    info = await live_client.async_get_last_recalled_preset()
    assert isinstance(info.name, (str, type(None)))
    assert isinstance(info.modified, bool)


async def test_diagnostics_from_live_coordinator(live_client: AshlyClient) -> None:
    """Generate the diagnostics dump from a real coordinator.

    Verifies the dump contains every expected top-level section, that
    every redaction target is actually scrubbed, and that the real MAC
    address never appears anywhere in the payload.
    """
    from custom_components.ashly.diagnostics import (
        TO_REDACT,
        async_get_config_entry_diagnostics,
    )

    hass = MagicMock()
    hass.loop = asyncio.get_running_loop()
    entry = MagicMock()
    entry.entry_id = "live-diag"
    entry.data = {
        "host": live_client.host,
        "port": live_client.port,
        "username": "admin",
        "password": "secret",
    }
    entry.options = {}

    coord = AshlyCoordinator(hass, live_client, entry)
    await coord._async_setup()
    coord.data = await coord._async_update_data()

    # Build an AshlyData stand-in on the entry so the diagnostics
    # function's `entry.runtime_data.coordinator` path works.
    runtime_data = MagicMock()
    runtime_data.coordinator = coord
    entry.runtime_data = runtime_data

    diag = await async_get_config_entry_diagnostics(hass, entry)

    expected_keys = {
        "config_entry_data",
        "config_entry_options",
        "system_info",
        "front_panel",
        "power_on",
        "channels",
        "chains",
        "dvca",
        "crosspoints",
        "presets",
        "phantom_power",
        "mic_preamp_gain",
        "gpo",
        "last_recalled_preset",
    }
    assert set(diag) == expected_keys

    real_mac = (await live_client.async_get_system_info()).mac_address
    flat = repr(diag)
    assert real_mac not in flat, "MAC leaked into diagnostics dump"
    assert live_client.host not in flat, "Host leaked into diagnostics dump"
    assert "secret" not in flat, "Password leaked into diagnostics dump"

    # Every documented redaction target is replaced with the sentinel.
    for key in TO_REDACT:
        if key in diag["config_entry_data"]:
            assert diag["config_entry_data"][key] == "**REDACTED**"
    if "mac_address" in diag["system_info"]:
        assert diag["system_info"]["mac_address"] == "**REDACTED**"


# ── Coordinator end-to-end ────────────────────────────────────────────


async def test_meter_websocket_streams_channel_meters(
    live_session: aiohttp.ClientSession,
    ashly_host: str,
    ashly_port: int,
    ashly_username: str,
    ashly_password: str,
    ashly_meter_port: int,
) -> None:
    """Confirm the live meter websocket actually streams data.

    We log in to capture the session cookie, then start the meter client
    and wait until either the first snapshot arrives or a timeout expires.
    """
    from custom_components.ashly.meter import AshlyMeterClient

    # Establish the session cookie that the socket.io server requires.
    # Build the auth session with ThreadedResolver too so pycares isn't
    # dragged in (it leaves a daemon thread that trips verify_cleanup).
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    auth_session = aiohttp.ClientSession(
        cookie_jar=cookie_jar,
        connector=aiohttp.TCPConnector(
            resolver=aiohttp.ThreadedResolver(),
            force_close=True,
            enable_cleanup_closed=False,
        ),
    )
    try:
        async with auth_session.post(
            f"http://{ashly_host}:{ashly_port}/v1.0-beta/session/login",
            json={
                "username": ashly_username,
                "password": ashly_password,
                "keepLoggedIn": True,
            },
        ) as resp:
            assert resp.status == 200
    finally:
        await auth_session.close()

    meter_client = AshlyMeterClient(
        host=ashly_host,
        port=ashly_port,
        cookie_jar=cookie_jar,
        socketio_port=ashly_meter_port,
    )
    got_records: list[list[int]] = []
    meter_client.add_listener(lambda r: got_records.append(list(r)))
    await meter_client.async_start()
    try:
        # Allow up to 5 s for the first throttled publish to fire.
        for _ in range(50):
            if got_records:
                break
            await asyncio.sleep(0.1)
    finally:
        await meter_client.async_stop()

    assert got_records, "no meter snapshot received within 5 s"
    snapshot = got_records[0]
    # 24 channel-meter positions are always present (12 inputs + 12 mixer).
    assert len(snapshot) >= 24, f"too few meter positions: {len(snapshot)}"
    # Values are non-negative raw integers per the protocol.
    assert all(isinstance(v, (int, float)) and v >= 0 for v in snapshot[:24])


async def test_coordinator_setup_and_update(live_client: AshlyClient) -> None:
    """Smoke-test the full coordinator path against the device.

    We hand-build a minimal hass/entry stand-in — the coordinator itself
    is the unit under test; we don't need a full HA runtime.
    """
    import asyncio

    hass = MagicMock()
    hass.loop = asyncio.get_running_loop()
    entry = MagicMock()
    entry.options = {}
    entry.entry_id = "live"
    entry.data = {}

    coord = AshlyCoordinator(hass, live_client, entry)
    await coord._async_setup()
    assert coord.system_info is not None
    assert coord.system_info.mac_address

    data = await coord._async_update_data()
    assert data.power_on in (True, False)
    assert input_channel_id(1) in data.chains
    assert output_channel_id(NUM_OUTPUTS) in data.chains
    assert NUM_DVCA_GROUPS in data.dvca
    assert (NUM_MIXERS, NUM_INPUTS) in data.crosspoints
