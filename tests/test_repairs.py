"""Tests for the default-credentials repair fix flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ashly.client import (
    AshlyApiError,
    AshlyAuthError,
    AshlyConnectionError,
)
from custom_components.ashly.const import DOMAIN, SERVICE_ACCOUNT_USERNAME
from custom_components.ashly.repairs import (
    DefaultCredentialsRepairFlow,
    _NoopRepairFlow,
    async_create_fix_flow,
)


@pytest.fixture
async def loaded_entry(hass: HomeAssistant, mock_config_entry, mock_client, patched_session):
    with patch("custom_components.ashly.AshlyClient", return_value=mock_client):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    return mock_config_entry


async def test_async_create_fix_flow_default_credentials_returns_repair(
    hass: HomeAssistant, loaded_entry
):
    flow = await async_create_fix_flow(hass, f"default_credentials_{loaded_entry.entry_id}", None)
    assert isinstance(flow, DefaultCredentialsRepairFlow)


async def test_async_create_fix_flow_unrelated_returns_noop(hass: HomeAssistant):
    """An issue id we don't have a fix flow for falls back to a no-op flow."""
    flow = await async_create_fix_flow(hass, "device_unreachable_xyz", None)
    assert isinstance(flow, _NoopRepairFlow)


async def test_async_create_fix_flow_push_stale_returns_noop(hass: HomeAssistant):
    """push_stale_<entry_id> is informational; dispatched to the noop flow."""
    flow = await async_create_fix_flow(hass, "push_stale_abc123", None)
    assert isinstance(flow, _NoopRepairFlow)


async def test_noop_repair_flow_immediately_creates_entry(hass: HomeAssistant):
    flow = _NoopRepairFlow()
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_init_shows_menu(hass: HomeAssistant, loaded_entry) -> None:
    """The init step presents the provision/manual menu with all placeholders filled."""
    flow = DefaultCredentialsRepairFlow(hass, loaded_entry.entry_id)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"provision", "manual"}
    # All placeholders referenced by strings.json must be present, otherwise
    # HA's renderer raises KeyError → 500 "Config flow could not be loaded".
    placeholders = result.get("description_placeholders") or {}
    assert "name" in placeholders
    assert "host" in placeholders
    assert "service_user" in placeholders


async def test_init_aborts_if_entry_gone(hass: HomeAssistant) -> None:
    """If the entry vanished, init aborts cleanly (no orphan flows)."""
    flow = DefaultCredentialsRepairFlow(hass, "nonexistent-entry-id")
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"


# ── Provision path ────────────────────────────────────────────────────


async def test_provision_path_happy(hass: HomeAssistant, loaded_entry, mock_client):
    """User selects provision; default creds work; new service account stored."""
    flow = DefaultCredentialsRepairFlow(hass, loaded_entry.entry_id)
    flow.hass = hass

    # First, the provision step shows a confirmation form.
    result = await flow.async_step_provision()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "provision"

    # Patch the actual provisioning client interactions.
    with (
        patch("custom_components.ashly.repairs.async_create_clientsession"),
        patch("custom_components.ashly.repairs.AshlyClient") as MockClient,
    ):
        instance = MockClient.return_value
        instance.async_login = AsyncMock()
        instance.async_provision_service_account = AsyncMock()
        result = await flow.async_step_provision({})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded_entry.data[CONF_USERNAME] == SERVICE_ACCOUNT_USERNAME
    # password should be a 16-char hex string
    pw = loaded_entry.data[CONF_PASSWORD]
    assert isinstance(pw, str) and len(pw) == 16
    assert all(c in "0123456789abcdef" for c in pw)


async def test_provision_path_admin_changed(hass: HomeAssistant, loaded_entry, mock_client):
    """If admin password has already changed, surface admin_changed error."""
    flow = DefaultCredentialsRepairFlow(hass, loaded_entry.entry_id)
    flow.hass = hass

    with (
        patch("custom_components.ashly.repairs.async_create_clientsession"),
        patch("custom_components.ashly.repairs.AshlyClient") as MockClient,
    ):
        MockClient.return_value.async_login = AsyncMock(side_effect=AshlyAuthError("x"))
        result = await flow.async_step_provision({})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "admin_changed"}


async def test_provision_path_connection_error(
    hass: HomeAssistant, loaded_entry, mock_client
):
    flow = DefaultCredentialsRepairFlow(hass, loaded_entry.entry_id)
    flow.hass = hass

    with (
        patch("custom_components.ashly.repairs.async_create_clientsession"),
        patch("custom_components.ashly.repairs.AshlyClient") as MockClient,
    ):
        MockClient.return_value.async_login = AsyncMock(side_effect=AshlyConnectionError("x"))
        result = await flow.async_step_provision({})

    assert result["errors"] == {"base": "cannot_connect"}


async def test_provision_path_api_error(hass: HomeAssistant, loaded_entry, mock_client):
    """Provision call fails after login → provision_failed."""
    flow = DefaultCredentialsRepairFlow(hass, loaded_entry.entry_id)
    flow.hass = hass

    with (
        patch("custom_components.ashly.repairs.async_create_clientsession"),
        patch("custom_components.ashly.repairs.AshlyClient") as MockClient,
    ):
        inst = MockClient.return_value
        inst.async_login = AsyncMock()
        inst.async_provision_service_account = AsyncMock(side_effect=AshlyApiError("bad"))
        result = await flow.async_step_provision({})

    assert result["errors"] == {"base": "provision_failed"}


async def test_provision_path_aborts_if_entry_gone(hass: HomeAssistant):
    flow = DefaultCredentialsRepairFlow(hass, "nonexistent-entry-id")
    flow.hass = hass
    result = await flow.async_step_provision()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"


# ── Manual path (legacy) ──────────────────────────────────────────────


async def test_manual_path_happy(hass: HomeAssistant, loaded_entry, mock_client):
    flow = DefaultCredentialsRepairFlow(hass, loaded_entry.entry_id)
    flow.hass = hass
    result = await flow.async_step_manual()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"

    with (
        patch("custom_components.ashly.repairs.async_create_clientsession"),
        patch("custom_components.ashly.repairs.AshlyClient.async_login", return_value=None),
        patch("custom_components.ashly.AshlyClient", return_value=mock_client),
    ):
        result = await flow.async_step_manual({CONF_USERNAME: "alice", CONF_PASSWORD: "hunter2"})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded_entry.data[CONF_USERNAME] == "alice"
    assert loaded_entry.data[CONF_PASSWORD] == "hunter2"


async def test_manual_path_invalid_auth(hass: HomeAssistant, loaded_entry, mock_client):
    flow = DefaultCredentialsRepairFlow(hass, loaded_entry.entry_id)
    flow.hass = hass
    with (
        patch("custom_components.ashly.repairs.async_create_clientsession"),
        patch(
            "custom_components.ashly.repairs.AshlyClient.async_login",
            side_effect=AshlyAuthError("nope"),
        ),
    ):
        result = await flow.async_step_manual(
            {CONF_USERNAME: "alice", CONF_PASSWORD: "wrong"}
        )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_manual_path_connection_error(hass: HomeAssistant, loaded_entry, mock_client):
    flow = DefaultCredentialsRepairFlow(hass, loaded_entry.entry_id)
    flow.hass = hass
    with (
        patch("custom_components.ashly.repairs.async_create_clientsession"),
        patch(
            "custom_components.ashly.repairs.AshlyClient.async_login",
            side_effect=AshlyConnectionError("offline"),
        ),
    ):
        result = await flow.async_step_manual(
            {CONF_USERNAME: "alice", CONF_PASSWORD: "anything"}
        )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_manual_path_aborts_if_entry_gone(hass: HomeAssistant):
    flow = DefaultCredentialsRepairFlow(hass, "nonexistent-entry-id")
    flow.hass = hass
    result = await flow.async_step_manual()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"


async def test_coordinator_creates_fixable_default_credentials_issue(
    hass: HomeAssistant, mock_client, mock_config_entry
):
    """The default-credentials repair issue is created with is_fixable=True."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.ashly.coordinator import AshlyCoordinator

    mock_config_entry.add_to_hass(hass)
    coord = AshlyCoordinator(hass, mock_client, mock_config_entry)
    await coord._async_setup()
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"default_credentials_{mock_config_entry.entry_id}"
    )
    assert issue is not None
    assert issue.is_fixable is True
