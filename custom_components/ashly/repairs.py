"""Repair flows for actionable Ashly Audio integration issues.

The `default_credentials` repair now offers two paths via a menu:

1. **Provision a dedicated service account** (recommended) — the integration
   creates a `haassistant` user on the device with the minimum permissions
   it needs, generates a random password, and stores those new credentials.
   The user's `admin` account stays untouched. Only works on devices where
   `admin/secret` still works (i.e. the operator has not yet changed admin).

2. **I've already changed the admin password** — the legacy flow that asks
   for the new admin credentials.

`device_unreachable` is intentionally not given a fix flow — the
underlying cause (device powered off, IP changed, cable disconnected)
isn't something this UI can do anything about.
"""

from __future__ import annotations

import logging
import secrets
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

from .client import (
    AshlyApiError,
    AshlyAuthError,
    AshlyClient,
    AshlyConnectionError,
)
from .const import (
    CONF_PORT,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    SERVICE_ACCOUNT_PASSWORD_HEX_BYTES,
    SERVICE_ACCOUNT_PERMISSIONS,
    SERVICE_ACCOUNT_ROLE,
    SERVICE_ACCOUNT_USERNAME,
)

_LOGGER = logging.getLogger(__name__)

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
    """Repair flow for an entry running on factory `admin/secret` credentials.

    Presents a menu of two fix options: provision a service account (best),
    or accept new admin credentials the user has set themselves.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        # Resolve the entry's name/host so the title/description placeholders
        # render. A missing `{name}` placeholder raises KeyError during HA's
        # form-render path and surfaces as "Config flow could not be loaded".
        entry = self._hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        return self.async_show_menu(
            step_id="init",
            menu_options=["provision", "manual"],
            description_placeholders={
                "name": entry.title,
                "host": entry.data.get("host", ""),
                "service_user": SERVICE_ACCOUNT_USERNAME,
            },
        )

    # ── Option 1: provision service account ─────────────────────────

    async def async_step_provision(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Create the dedicated service account using the still-default admin creds."""
        errors: dict[str, str] = {}
        entry = self._hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        host = entry.data.get("host", "")
        port = int(entry.data.get(CONF_PORT, DEFAULT_PORT))

        if user_input is not None:
            # Always provision against the factory creds. If the user has
            # since changed admin, the login below will 401 and we route
            # them to the manual path.
            new_pw = secrets.token_hex(SERVICE_ACCOUNT_PASSWORD_HEX_BYTES)
            session = async_create_clientsession(
                self._hass, cookie_jar=aiohttp.CookieJar(unsafe=True)
            )
            client = AshlyClient(
                host=host,
                port=port,
                session=session,
                username=DEFAULT_USERNAME,
                password=DEFAULT_PASSWORD,
            )
            try:
                await client.async_login()
                await client.async_provision_service_account(
                    username=SERVICE_ACCOUNT_USERNAME,
                    password=new_pw,
                    role_type_id=SERVICE_ACCOUNT_ROLE,
                    permissions=SERVICE_ACCOUNT_PERMISSIONS,
                )
                # Confirm the new user can sign in before we commit creds.
                verify_session = async_create_clientsession(
                    self._hass, cookie_jar=aiohttp.CookieJar(unsafe=True)
                )
                verify = AshlyClient(
                    host=host,
                    port=port,
                    session=verify_session,
                    username=SERVICE_ACCOUNT_USERNAME,
                    password=new_pw,
                )
                await verify.async_login()
            except AshlyAuthError:
                # Admin password already changed; the user must use the manual
                # fix path. Surface as an error rather than silently failing.
                errors["base"] = "admin_changed"
            except AshlyConnectionError:
                errors["base"] = "cannot_connect"
            except AshlyApiError as err:
                _LOGGER.error("Service-account repair failed: %s", err)
                errors["base"] = "provision_failed"
            else:
                self._hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_USERNAME: SERVICE_ACCOUNT_USERNAME,
                        CONF_PASSWORD: new_pw,
                    },
                )
                await self._hass.config_entries.async_reload(entry.entry_id)
                return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="provision",
            # Empty schema — this step is purely confirmation. Submitting the
            # form runs the provisioning.
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": entry.title,
                "host": host,
                "service_user": SERVICE_ACCOUNT_USERNAME,
            },
            errors=errors,
        )

    # ── Option 2: manual new credentials (legacy path) ──────────────

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self._hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
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
                await self._hass.config_entries.async_reload(entry.entry_id)
                return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="manual",
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
    """Dispatch the right repair flow for an issue id."""
    if issue_id.startswith("default_credentials_"):
        entry_id = issue_id.removeprefix("default_credentials_")
        return DefaultCredentialsRepairFlow(hass, entry_id)
    return _NoopRepairFlow()


class _NoopRepairFlow(RepairsFlow):
    """Fallback for unfixable / informational issues."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_create_entry(data={})
