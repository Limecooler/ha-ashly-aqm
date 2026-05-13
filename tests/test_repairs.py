"""Tests for the default-credentials repair fix flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ashly.client import AshlyAuthError, AshlyConnectionError
from custom_components.ashly.const import DOMAIN
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


async def test_noop_repair_flow_immediately_creates_entry(hass: HomeAssistant):
    flow = _NoopRepairFlow()
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_default_credentials_repair_flow_happy_path(
    hass: HomeAssistant, loaded_entry, mock_client
):
    """Submitting valid new credentials updates the entry and reloads."""
    flow = DefaultCredentialsRepairFlow(hass, loaded_entry.entry_id)
    flow.hass = hass
    # First call shows the form.
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    # Submit new credentials — mock_client.async_login defaults to a noop success.
    result = await flow.async_step_init({CONF_USERNAME: "alice", CONF_PASSWORD: "hunter2"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded_entry.data[CONF_USERNAME] == "alice"
    assert loaded_entry.data[CONF_PASSWORD] == "hunter2"


async def test_default_credentials_repair_flow_invalid_auth(
    hass: HomeAssistant, loaded_entry, mock_client
):
    """If the new credentials still fail auth, the form re-shows with invalid_auth."""
    flow = DefaultCredentialsRepairFlow(hass, loaded_entry.entry_id)
    flow.hass = hass
    # Make the next login attempt fail.
    with patch(
        "custom_components.ashly.repairs.AshlyClient.async_login",
        side_effect=AshlyAuthError("still wrong"),
    ):
        result = await flow.async_step_init({CONF_USERNAME: "alice", CONF_PASSWORD: "wrong"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_default_credentials_repair_flow_connection_error(
    hass: HomeAssistant, loaded_entry, mock_client
):
    """If the device is unreachable while fixing, the form re-shows with cannot_connect."""
    flow = DefaultCredentialsRepairFlow(hass, loaded_entry.entry_id)
    flow.hass = hass
    with patch(
        "custom_components.ashly.repairs.AshlyClient.async_login",
        side_effect=AshlyConnectionError("offline"),
    ):
        result = await flow.async_step_init({CONF_USERNAME: "alice", CONF_PASSWORD: "anything"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_default_credentials_repair_flow_aborts_if_entry_gone(
    hass: HomeAssistant, loaded_entry
):
    """If the entry was removed between issue surfacing and fix-flow start, abort."""
    flow = DefaultCredentialsRepairFlow(hass, "nonexistent-entry-id")
    flow.hass = hass
    result = await flow.async_step_init()
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
