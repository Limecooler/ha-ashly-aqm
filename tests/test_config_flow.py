"""Tests for the Ashly config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

try:
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
except ImportError:
    from homeassistant.components.dhcp import DhcpServiceInfo

from custom_components.ashly.client import (
    AshlyApiError,
    AshlyAuthError,
    AshlyConnectionError,
    SystemInfo,
)
from custom_components.ashly.const import (
    CONF_CREATE_SERVICE_ACCOUNT,
    CONF_PORT,
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    DOMAIN,
    SERVICE_ACCOUNT_USERNAME,
)

VALID_INFO = SystemInfo(
    model="AQM1208",
    name="Living Room",
    firmware_version="1.1.8",
    hardware_revision="1.0.0",
    mac_address="00:14:aa:11:22:33",
    has_auto_mix=True,
)

HOST_INPUT = {CONF_HOST: "192.168.1.100", CONF_PORT: 8000}
USER_INPUT_FULL = {
    CONF_HOST: "192.168.1.100",
    CONF_PORT: 8000,
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "secret",
}


@pytest.fixture(autouse=True)
def _bypass_setup_entry():
    """Skip the heavy async_setup_entry path during config-flow tests."""
    with patch("custom_components.ashly.async_setup_entry", return_value=True):
        yield


# ── User step: default creds work → service_account → declined ──────────


async def test_user_flow_defaults_work_decline_service_account(hass: HomeAssistant) -> None:
    """Happy path: factory creds work, user declines service-account creation."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        return_value=VALID_INFO,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], HOST_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "service_account"

    # User unchecks the box
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CREATE_SERVICE_ACCOUNT: False}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living Room"
    assert result["data"][CONF_USERNAME] == DEFAULT_USERNAME
    assert result["data"][CONF_PASSWORD] == DEFAULT_PASSWORD


async def test_user_flow_defaults_work_provision_service_account(
    hass: HomeAssistant,
) -> None:
    """Factory creds work; user accepts; provisioned creds end up in the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        return_value=VALID_INFO,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], HOST_INPUT)

    assert result["step_id"] == "service_account"

    with patch(
        "custom_components.ashly.config_flow._provision_service_account",
        return_value="generatedpassword42",
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CREATE_SERVICE_ACCOUNT: True}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_USERNAME] == SERVICE_ACCOUNT_USERNAME
    assert result["data"][CONF_PASSWORD] == "generatedpassword42"


# ── User step: defaults fail → credentials path ─────────────────────────


async def test_user_flow_defaults_fail_falls_through_to_credentials(
    hass: HomeAssistant,
) -> None:
    """If factory creds 401, user is asked for real credentials."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        side_effect=AshlyAuthError("hardened"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], HOST_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "credentials"

    # Custom (non-factory) creds → skip service_account step entirely
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        return_value=VALID_INFO,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "admin", CONF_PASSWORD: "operatorPass!"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_USERNAME] == "admin"
    assert result["data"][CONF_PASSWORD] == "operatorPass!"


async def test_user_flow_user_types_factory_creds_offers_service_account(
    hass: HomeAssistant,
) -> None:
    """If the user manually enters admin/secret at credentials step, still offer service account."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        side_effect=AshlyAuthError("x"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], HOST_INPUT)
    assert result["step_id"] == "credentials"

    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        return_value=VALID_INFO,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: DEFAULT_USERNAME, CONF_PASSWORD: DEFAULT_PASSWORD},
        )
    assert result["step_id"] == "service_account"


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (AshlyConnectionError("x"), "cannot_connect"),
        (RuntimeError("x"), "unknown"),
    ],
)
async def test_user_flow_errors(hass: HomeAssistant, exc, error) -> None:
    """Connection errors keep the user step open; auth errors do NOT (handled separately)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        side_effect=exc,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], HOST_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (AshlyConnectionError("x"), "cannot_connect"),
        (AshlyAuthError("x"), "invalid_auth"),
        (RuntimeError("x"), "unknown"),
    ],
)
async def test_credentials_step_errors(hass: HomeAssistant, exc, error) -> None:
    """Errors at the credentials step surface in the form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        side_effect=AshlyAuthError("force credentials step"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], HOST_INPUT)
    assert result["step_id"] == "credentials"

    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        side_effect=exc,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "admin", CONF_PASSWORD: "tryagain"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_credentials_step_no_mac_aborts(hass: HomeAssistant) -> None:
    """If creds entered at step 2 yield a device without a MAC, abort with no_mac.

    Exercises the `return abort` branch in async_step_credentials when
    `_maybe_abort_on_mac` decides the response is unusable — distinct
    from the `_abort_if_unique_id_configured` raise path which doesn't
    flow through that return.
    """
    no_mac = SystemInfo(
        model="AQM1208",
        name="MAC-less",
        firmware_version="1.0",
        hardware_revision="1.0",
        mac_address="",  # device returned no MAC
        has_auto_mix=False,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        side_effect=AshlyAuthError("force credentials step"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], HOST_INPUT)
    assert result["step_id"] == "credentials"

    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        return_value=no_mac,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "admin", CONF_PASSWORD: "tryagain"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_mac"


async def test_user_flow_no_mac_aborts(hass: HomeAssistant) -> None:
    """A device without a MAC aborts the flow (the user can't fix this in-form)."""
    no_mac = SystemInfo(
        model="AQM1208",
        name="",
        firmware_version="1.1.8",
        hardware_revision="1.0.0",
        mac_address="",
        has_auto_mix=False,
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        return_value=no_mac,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], HOST_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_mac"


# ── service_account step error paths ────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (AshlyConnectionError("x"), "cannot_connect"),
        (AshlyAuthError("x"), "invalid_auth"),
        (AshlyApiError("x"), "provision_failed"),
        (RuntimeError("x"), "unknown"),
    ],
)
async def test_service_account_step_errors(hass: HomeAssistant, exc, error) -> None:
    """If provisioning fails, surface the error on the same step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        return_value=VALID_INFO,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], HOST_INPUT)
    assert result["step_id"] == "service_account"

    with patch(
        "custom_components.ashly.config_flow._provision_service_account",
        side_effect=exc,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CREATE_SERVICE_ACCOUNT: True}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "service_account"
    assert result["errors"] == {"base": error}


# ── DHCP discovery ──────────────────────────────────────────────────────


async def test_dhcp_non_ashly_aborts(hass: HomeAssistant) -> None:
    info = DhcpServiceInfo(
        ip="192.168.1.50",
        hostname="other_device",
        macaddress="aabbccddeeff",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_ashly_device"


async def test_dhcp_confirm_defaults_work_offers_service_account(hass: HomeAssistant) -> None:
    """DHCP-discovered device with factory creds → service_account step."""
    info = DhcpServiceInfo(
        ip="192.168.1.114",
        hostname="aqm1208_0014AA112233",
        macaddress="0014aa112233",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=info
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"

    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        return_value=VALID_INFO,
    ):
        # The confirm form now collects port only (no creds).
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "service_account"


async def test_dhcp_confirm_defaults_fail_falls_to_credentials(hass: HomeAssistant) -> None:
    info = DhcpServiceInfo(
        ip="192.168.1.114",
        hostname="aqm1208_0014AA112233",
        macaddress="0014aa112233",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=info
    )
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        side_effect=AshlyAuthError("x"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "credentials"


# ── reauth ─────────────────────────────────────────────────────────────


async def test_reauth_success(hass: HomeAssistant, mock_config_entry) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        return_value=VALID_INFO,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "admin", CONF_PASSWORD: "newpass"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "newpass"


# ── reconfigure ────────────────────────────────────────────────────────


async def test_reconfigure_success(hass: HomeAssistant, mock_config_entry) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM

    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        return_value=VALID_INFO,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**USER_INPUT_FULL, CONF_HOST: "192.168.1.200"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_HOST] == "192.168.1.200"


# ── options ────────────────────────────────────────────────────────────


async def test_options_flow(hass: HomeAssistant, mock_config_entry) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"poll_interval": 60}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"poll_interval": 60}


# ── _login_and_get_info direct ─────────────────────────────────────────


async def test_login_and_get_info_invokes_client(hass: HomeAssistant) -> None:
    """_login_and_get_info logs in and fetches system info from the client."""
    from custom_components.ashly.config_flow import _login_and_get_info

    with (
        patch("custom_components.ashly.config_flow.async_create_clientsession"),
        patch("custom_components.ashly.config_flow.AshlyClient") as MockClient,
    ):
        instance = MockClient.return_value
        instance.async_login = AsyncMock()
        instance.async_get_system_info = AsyncMock(return_value=VALID_INFO)
        result = await _login_and_get_info(hass, "192.0.2.1", 8000, "admin", "secret")
        assert result is VALID_INFO
        instance.async_login.assert_awaited_once()
        instance.async_get_system_info.assert_awaited_once()


async def test_provision_service_account_creates_and_verifies(hass: HomeAssistant) -> None:
    """_provision_service_account calls async_provision_service_account then verifies login."""
    from custom_components.ashly.config_flow import _provision_service_account

    with (
        patch("custom_components.ashly.config_flow.async_create_clientsession"),
        patch("custom_components.ashly.config_flow.AshlyClient") as MockClient,
    ):
        instance = MockClient.return_value
        instance.async_login = AsyncMock()
        instance.async_provision_service_account = AsyncMock()
        pw = await _provision_service_account(hass, "192.0.2.1", 8000, "admin", "secret")
        # Random 16-hex-char password
        assert len(pw) == 16
        assert all(c in "0123456789abcdef" for c in pw)
        # provision called once on the admin client; login called twice
        # (once on admin client, once on the verify client).
        instance.async_provision_service_account.assert_awaited_once()
        assert instance.async_login.await_count == 2


# ── DHCP discovery edge cases ──────────────────────────────────────────


async def test_dhcp_unparseable_mac_aborts(hass: HomeAssistant) -> None:
    """If format_mac raises on the DHCP payload's MAC, we abort cleanly."""
    from custom_components.ashly.config_flow import AshlyConfigFlow

    class _Stub:
        ip = "192.168.1.50"
        hostname = "ashly"
        macaddress = object()  # not a string — format_mac will raise

    flow = AshlyConfigFlow()
    flow.hass = hass
    result = await flow.async_step_dhcp(_Stub())  # type: ignore[arg-type]
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_ashly_device"


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (AshlyConnectionError("x"), "cannot_connect"),
        (RuntimeError("x"), "unknown"),
    ],
)
async def test_dhcp_confirm_errors(hass: HomeAssistant, exc, error) -> None:
    info = DhcpServiceInfo(
        ip="192.168.1.114",
        hostname="aqm1208",
        macaddress="0014aa112233",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=info
    )
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        side_effect=exc,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_dhcp_confirm_mac_mismatch_aborts(hass: HomeAssistant) -> None:
    """If the device under the discovered IP returns a different MAC, abort."""
    discovery = DhcpServiceInfo(
        ip="192.168.1.114",
        hostname="aqm1208",
        macaddress="0014aa112233",
    )
    drifted = SystemInfo(
        model="AQM1208",
        name="Drift",
        firmware_version="1.0",
        hardware_revision="1.0",
        mac_address="00:14:aa:99:99:99",  # different
        has_auto_mix=False,
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=discovery
    )
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        return_value=drifted,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"


# ── reauth edge cases ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (AshlyConnectionError("x"), "cannot_connect"),
        (AshlyAuthError("x"), "invalid_auth"),
        (RuntimeError("x"), "unknown"),
    ],
)
async def test_reauth_errors(hass: HomeAssistant, mock_config_entry, exc, error) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        side_effect=exc,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "admin", CONF_PASSWORD: "wrong"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_reauth_mac_mismatch_aborts(hass: HomeAssistant, mock_config_entry) -> None:
    """If reauth lands on a different device than the entry's, abort."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    drifted = SystemInfo(
        model="AQM1208",
        name="Drift",
        firmware_version="1.0",
        hardware_revision="1.0",
        mac_address="00:14:aa:99:99:99",
        has_auto_mix=False,
    )
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        return_value=drifted,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "admin", CONF_PASSWORD: "newpass"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"


# ── reconfigure edge cases ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (AshlyConnectionError("x"), "cannot_connect"),
        (AshlyAuthError("x"), "invalid_auth"),
        (RuntimeError("x"), "unknown"),
    ],
)
async def test_reconfigure_errors(hass: HomeAssistant, mock_config_entry, exc, error) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        side_effect=exc,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT_FULL,
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


# ── Zeroconf discovery ─────────────────────────────────────────────────


def _zeroconf_info(hostname: str = "aqm1208_0014aa112233.local.", host: str = "192.168.1.114"):
    try:
        from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
    except ImportError:
        from homeassistant.components.zeroconf import ZeroconfServiceInfo
    return ZeroconfServiceInfo(
        ip_address=host,  # type: ignore[arg-type]
        ip_addresses=[host],  # type: ignore[list-item]
        port=80,
        hostname=hostname,
        type="_http._tcp.local.",
        name=hostname,
        properties={},
    )


async def test_zeroconf_extracts_mac_from_hostname(hass: HomeAssistant) -> None:
    """Hostname `aqm1208_0014aa112233.local.` → MAC `00:14:aa:11:22:33`."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_zeroconf_info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"


async def test_zeroconf_non_ashly_hostname_aborts(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf_info(hostname="not_ours.local."),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_ashly_device"


async def test_zeroconf_wrong_oui_aborts(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf_info(hostname="aqm_aabbccddeeff.local."),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_ashly_device"


async def test_zeroconf_aborts_when_no_mac(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf_info(hostname="aqm.local."),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_ashly_device"


async def test_zeroconf_uses_properties_mac(hass: HomeAssistant) -> None:
    try:
        from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
    except ImportError:
        from homeassistant.components.zeroconf import ZeroconfServiceInfo
    info = ZeroconfServiceInfo(
        ip_address="192.168.1.50",  # type: ignore[arg-type]
        ip_addresses=["192.168.1.50"],  # type: ignore[list-item]
        port=80,
        hostname="aqm.local.",
        type="_http._tcp.local.",
        name="aqm.local.",
        properties={"macaddress": "0014aa334455"},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"


async def test_zeroconf_invalid_hostname_mac_aborts(hass: HomeAssistant) -> None:
    info = _zeroconf_info(hostname="aqm_gggggggggggg.local.")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_ashly_device"


async def test_zeroconf_uppercase_hostname_mac_extracted(hass: HomeAssistant) -> None:
    info = _zeroconf_info(hostname="aqm1208_0014AA778899.local.".lower())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"


async def test_zeroconf_format_mac_raises_aborts(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.ashly.config_flow.format_mac",
        side_effect=AttributeError("bogus"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=_zeroconf_info(hostname="aqm_0014aa112233.local."),
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_ashly_device"


async def test_zeroconf_format_mac_raises_on_property_aborts(
    hass: HomeAssistant,
) -> None:
    try:
        from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
    except ImportError:
        from homeassistant.components.zeroconf import ZeroconfServiceInfo
    info = ZeroconfServiceInfo(
        ip_address="192.168.1.50",  # type: ignore[arg-type]
        ip_addresses=["192.168.1.50"],  # type: ignore[list-item]
        port=80,
        hostname="aqm.local.",
        type="_http._tcp.local.",
        name="aqm.local.",
        properties={"mac": "valid-but-format-mac-will-fail"},
    )
    with patch(
        "custom_components.ashly.config_flow.format_mac",
        side_effect=TypeError("nope"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_ashly_device"


async def test_discovery_confirm_accepts_port_override(hass: HomeAssistant) -> None:
    """User can change the port in the discovery confirm dialog."""
    try:
        from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
    except ImportError:
        from homeassistant.components.dhcp import DhcpServiceInfo
    info = DhcpServiceInfo(
        ip="192.168.1.114",
        hostname="aqm1208",
        macaddress="0014aa112233",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=info
    )
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        return_value=VALID_INFO,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PORT: 9000}
        )
    # Defaults work → service_account step
    assert result["step_id"] == "service_account"

    # Decline service account → entry created with port 9000
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CREATE_SERVICE_ACCOUNT: False}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PORT] == 9000


async def test_reconfigure_no_mac_aborts(hass: HomeAssistant, mock_config_entry) -> None:
    """If the device returns no MAC during reconfigure, the flow aborts."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    no_mac = SystemInfo(
        model="AQM1208",
        name="",
        firmware_version="1.0",
        hardware_revision="1.0",
        mac_address="",
        has_auto_mix=False,
    )
    with patch(
        "custom_components.ashly.config_flow._login_and_get_info",
        return_value=no_mac,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            USER_INPUT_FULL,
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_mac"
