"""Shared pytest fixtures for the Ashly Audio integration tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ashly.client import (
    AshlyClient,
    ChainState,
    CrosspointState,
    DSPChannel,
    DVCAState,
    FrontPanelInfo,
    LastRecalledPreset,
    PresetInfo,
    SystemInfo,
)
from custom_components.ashly.const import (
    CONF_PORT,
    DOMAIN,
    NUM_DVCA_GROUPS,
    NUM_GPO,
    NUM_INPUTS,
    NUM_MIXERS,
    NUM_OUTPUTS,
)
from custom_components.ashly.coordinator import AshlyCoordinator, AshlyDeviceData


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations in all tests."""


@pytest.fixture
def mock_system_info() -> SystemInfo:
    """Return deterministic SystemInfo for tests."""
    return SystemInfo(
        model="AQM1208",
        name="Test AQM1208",
        firmware_version="1.1.8",
        hardware_revision="1.0.0",
        mac_address="00:14:aa:11:22:33",
        has_auto_mix=True,
    )


@pytest.fixture
def mock_channels() -> dict[str, DSPChannel]:
    out: dict[str, DSPChannel] = {}
    for n in range(1, NUM_INPUTS + 1):
        cid = f"InputChannel.{n}"
        out[cid] = DSPChannel(
            channel_id=cid,
            name=f"Mic/Line {n}",
            default_name=f"Mic/Line {n}",
            base_type="Input",
            channel_number=n,
        )
    for n in range(1, NUM_OUTPUTS + 1):
        cid = f"OutputChannel.{n}"
        out[cid] = DSPChannel(
            channel_id=cid,
            name=f"Line Out {n}",
            default_name=f"Line Out {n}",
            base_type="Output",
            channel_number=n,
        )
    return out


@pytest.fixture
def mock_chains() -> dict[str, ChainState]:
    chains: dict[str, ChainState] = {}
    for n in range(1, NUM_INPUTS + 1):
        cid = f"InputChannel.{n}"
        chains[cid] = ChainState(channel_id=cid, muted=False, mixer_id=None)
    for n in range(1, NUM_OUTPUTS + 1):
        cid = f"OutputChannel.{n}"
        chains[cid] = ChainState(channel_id=cid, muted=False, mixer_id=None)
    return chains


@pytest.fixture
def mock_dvca() -> dict[int, DVCAState]:
    return {
        i: DVCAState(index=i, name=f"DCA {i}", level_db=0.0, muted=False)
        for i in range(1, NUM_DVCA_GROUPS + 1)
    }


@pytest.fixture
def mock_crosspoints() -> dict[tuple[int, int], CrosspointState]:
    return {
        (m, i): CrosspointState(mixer_index=m, input_index=i, level_db=0.0, muted=True)
        for m in range(1, NUM_MIXERS + 1)
        for i in range(1, NUM_INPUTS + 1)
    }


@pytest.fixture
def mock_front_panel() -> FrontPanelInfo:
    return FrontPanelInfo(power_on=True, leds_enabled=True)


@pytest.fixture
def mock_phantom_power() -> dict[int, bool]:
    return {i: False for i in range(1, NUM_INPUTS + 1)}


@pytest.fixture
def mock_mic_preamp() -> dict[int, int]:
    return {i: 0 for i in range(1, NUM_INPUTS + 1)}


@pytest.fixture
def mock_gpo() -> dict[int, bool]:
    return {i: False for i in range(1, NUM_GPO + 1)}


@pytest.fixture
def mock_last_recalled() -> LastRecalledPreset:
    return LastRecalledPreset(name=None, modified=False)


@pytest.fixture
def mock_device_data(
    mock_system_info: SystemInfo,
    mock_channels: dict[str, DSPChannel],
    mock_chains: dict[str, ChainState],
    mock_dvca: dict[int, DVCAState],
    mock_crosspoints: dict[tuple[int, int], CrosspointState],
    mock_front_panel: FrontPanelInfo,
    mock_phantom_power: dict[int, bool],
    mock_mic_preamp: dict[int, int],
    mock_gpo: dict[int, bool],
    mock_last_recalled: LastRecalledPreset,
) -> AshlyDeviceData:
    return AshlyDeviceData(
        system_info=mock_system_info,
        front_panel=mock_front_panel,
        channels=mock_channels,
        chains=mock_chains,
        dvca=mock_dvca,
        crosspoints=mock_crosspoints,
        presets=[
            PresetInfo(id="Preset 1", name="Preset 1"),
            PresetInfo(id="Preset 2", name="Preset 2"),
        ],
        phantom_power=mock_phantom_power,
        mic_preamp_gain=mock_mic_preamp,
        gpo=mock_gpo,
        last_recalled_preset=mock_last_recalled,
    )


@pytest.fixture
def mock_client(
    mock_system_info: SystemInfo,
    mock_channels: dict[str, DSPChannel],
    mock_chains: dict[str, ChainState],
    mock_dvca: dict[int, DVCAState],
    mock_crosspoints: dict[tuple[int, int], CrosspointState],
    mock_front_panel: FrontPanelInfo,
    mock_phantom_power: dict[int, bool],
    mock_mic_preamp: dict[int, int],
    mock_gpo: dict[int, bool],
    mock_last_recalled: LastRecalledPreset,
) -> AsyncMock:
    """A mocked AshlyClient with all methods pre-stubbed."""
    client = AsyncMock(spec=AshlyClient)
    client.host = "192.168.1.100"
    client.port = 8000

    client.async_login = AsyncMock(return_value=None)
    client.async_get_system_info = AsyncMock(return_value=mock_system_info)
    client.async_test_connection = AsyncMock(return_value=True)

    client.async_get_channels = AsyncMock(return_value=list(mock_channels.values()))
    client.async_get_power = AsyncMock(return_value=True)
    client.async_get_front_panel = AsyncMock(return_value=mock_front_panel)
    client.async_get_chain_state = AsyncMock(return_value=mock_chains)
    client.async_get_dvca_state = AsyncMock(return_value=mock_dvca)
    client.async_get_crosspoints = AsyncMock(return_value=mock_crosspoints)
    client.async_get_presets = AsyncMock(
        return_value=[
            PresetInfo(id="Preset 1", name="Preset 1"),
            PresetInfo(id="Preset 2", name="Preset 2"),
        ]
    )
    client.async_get_phantom_power = AsyncMock(return_value=mock_phantom_power)
    client.async_get_mic_preamp = AsyncMock(return_value=mock_mic_preamp)
    client.async_get_gpo = AsyncMock(return_value=mock_gpo)
    client.async_get_last_recalled_preset = AsyncMock(return_value=mock_last_recalled)

    # Setters
    client.async_set_power = AsyncMock(return_value=None)
    client.async_set_front_panel_leds = AsyncMock(return_value=None)
    client.async_set_chain_mute = AsyncMock(return_value=None)
    client.async_set_output_mixer = AsyncMock(return_value=None)
    client.async_set_dvca_level = AsyncMock(return_value=None)
    client.async_set_dvca_mute = AsyncMock(return_value=None)
    client.async_set_crosspoint_level = AsyncMock(return_value=None)
    client.async_set_crosspoint_mute = AsyncMock(return_value=None)
    client.async_set_phantom_power = AsyncMock(return_value=None)
    client.async_set_mic_preamp = AsyncMock(return_value=None)
    client.async_set_gpo = AsyncMock(return_value=None)
    client.async_identify = AsyncMock(return_value=None)
    return client


@pytest.fixture
def mock_meter_client() -> MagicMock:
    """A meter-client stand-in for entity tests."""
    mc = MagicMock()
    mc.connected = True
    mc.latest_records = [0] * 96
    mc.async_start = AsyncMock(return_value=None)
    mc.async_stop = AsyncMock(return_value=None)
    mc.add_listener = MagicMock(return_value=lambda: None)
    return mc


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.168.1.100",
            CONF_PORT: 8000,
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "secret",
        },
        unique_id="00:14:aa:11:22:33",
        title="Test AQM1208",
    )


@pytest.fixture
def patched_session():
    """Replace HA's session factory + meter websocket used during setup.

    `async_create_clientsession` ultimately constructs aiohttp objects which
    initialise pycares' AsyncResolver — that pycares thread trips HA's
    verify_cleanup fixture. The meter websocket would also start a real
    socket.io connection. Returning fakes keeps tests self-contained.
    """
    fake_session = MagicMock(spec=aiohttp.ClientSession)
    fake_session.closed = False
    fake_session.close = AsyncMock(return_value=None)

    fake_meter = MagicMock()
    fake_meter.connected = False
    fake_meter.latest_records = []
    fake_meter.async_start = AsyncMock(return_value=None)
    fake_meter.async_stop = AsyncMock(return_value=None)
    fake_meter.add_listener = MagicMock(return_value=lambda: None)

    with (
        patch(
            "custom_components.ashly.async_create_clientsession",
            return_value=fake_session,
        ),
        patch(
            "custom_components.ashly.config_flow.async_create_clientsession",
            return_value=fake_session,
        ),
        patch(
            "custom_components.ashly.AshlyMeterClient",
            return_value=fake_meter,
        ),
    ):
        yield fake_session


@pytest.fixture
def mock_coordinator(
    mock_client: AsyncMock,
    mock_system_info: SystemInfo,
    mock_channels: dict[str, DSPChannel],
    mock_device_data: AshlyDeviceData,
) -> MagicMock:
    """A MagicMock that walks like an AshlyCoordinator for entity tests."""
    coordinator = MagicMock(spec=AshlyCoordinator)
    coordinator.data = mock_device_data
    coordinator.system_info = mock_system_info
    coordinator.client = mock_client
    coordinator.channels = mock_channels
    coordinator.async_request_refresh = AsyncMock()
    coordinator.async_set_updated_data = MagicMock()
    coordinator.async_add_listener = MagicMock(return_value=lambda: None)
    coordinator.last_update_success = True
    return coordinator
