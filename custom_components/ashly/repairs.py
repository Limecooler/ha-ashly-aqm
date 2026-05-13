"""Repair flows for actionable Ashly Audio integration issues.

Currently the `default_credentials` repair gets a one-click fix flow that
prompts the user for their new (non-factory) password and writes it to the
config entry. The coordinator's next poll re-evaluates the repair and
clears the issue.

`device_unreachable` is intentionally not given a fix flow — the
underlying cause (device powered off, IP changed, cable disconnected)
isn't something this UI can do anything about; the description copy
points the user at the reconfigure flow instead.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import AshlyAuthError, AshlyClient, AshlyConnectionError
from .const import CONF_PORT, DEFAULT_PORT, DEFAULT_USERNAME

NEW_CREDS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class DefaultCredentialsRepairFlow(RepairsFlow):
    """Walk the user through updating the device's credentials in HA.

    The repair issue is per-entry; the entry id is encoded in the issue id
    as `default_credentials_<entry_id>`. We look up the entry, validate the
    new credentials against the actual device, then update the entry data.
    The repair issue auto-clears on the next successful coordinator poll
    via `_evaluate_repair_issues` (so we don't need to call
    `ir.async_delete_issue` here).
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self._hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            # Entry was removed between repair surfacing and this flow opening.
            return self.async_abort(reason="entry_not_found")

        host = entry.data.get("host", "")
        port = int(entry.data.get(CONF_PORT, DEFAULT_PORT))

        if user_input is not None:
            session = async_create_clientsession(
                self._hass, cookie_jar=aiohttp.CookieJar(unsafe=True)
            )
            client = AshlyClient(
                host=host,
                port=port,
                session=session,
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
            )
            try:
                await client.async_login()
            except AshlyAuthError:
                errors["base"] = "invalid_auth"
            except AshlyConnectionError:
                errors["base"] = "cannot_connect"
            else:
                self._hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
                # Reload the entry so the running client picks up the new
                # credentials immediately (and so the next poll fires).
                await self._hass.config_entries.async_reload(entry.entry_id)
                return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="init",
            data_schema=NEW_CREDS_SCHEMA,
            description_placeholders={
                "name": entry.title,
                "host": host,
            },
            errors=errors,
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Dispatch the right repair flow for an issue id.

    Issue ids are formatted `<rule>_<entry_id>` so we strip the rule prefix
    to find the entry. Only `default_credentials` is fixable today; other
    issues fall back to a no-op confirm flow.
    """
    if issue_id.startswith("default_credentials_"):
        entry_id = issue_id.removeprefix("default_credentials_")
        return DefaultCredentialsRepairFlow(hass, entry_id)
    # Future repair flows go here. The signature must always return a
    # RepairsFlow even for issues we don't customise.
    return _NoopRepairFlow()


class _NoopRepairFlow(RepairsFlow):
    """Fallback for unfixable / informational issues."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_create_entry(data={})
