"""Config flow for the Ashly Audio integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

if TYPE_CHECKING:
    # mypy only sees the new HA 2026.2+ location; the runtime fallback below
    # is invisible to typing.
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
else:
    try:
        from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
    except ImportError:  # pragma: no cover  # HA < 2026.2 fallback; CI is on 2026.5+
        from homeassistant.components.dhcp import DhcpServiceInfo
    try:
        from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
    except ImportError:  # pragma: no cover  # HA < 2026.2 fallback
        from homeassistant.components.zeroconf import ZeroconfServiceInfo

from .client import AshlyAuthError, AshlyClient, AshlyConnectionError, SystemInfo
from .const import (
    ASHLY_MAC_PREFIX,
    CONF_PORT,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): NumberSelector(
            NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
        ),
        # Username defaults to "admin" (the factory username on every AQM device).
        # Password is intentionally NOT defaulted: prefilling the factory password
        # would one-click users into the state our `default_credentials` repair
        # issue then scolds them for.
        vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


async def _validate_connection(
    hass: HomeAssistant,
    host: str,
    port: int,
    username: str,
    password: str,
) -> SystemInfo:
    """Probe the device once and return its system info.

    Uses an HA-managed session with its own cookie jar so the device's
    session cookie does not leak into HA's shared session.
    """
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar(unsafe=True))
    client = AshlyClient(
        host=host,
        port=port,
        session=session,
        username=username,
        password=password,
    )
    await client.async_login()
    return await client.async_get_system_info()


def _entry_title(info: SystemInfo) -> str:
    """Render a config entry title from device info."""
    return info.name or f"Ashly {info.model}"


class AshlyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Ashly Audio integration."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._discovered_host: str | None = None
        self._discovered_port: int = DEFAULT_PORT
        self._discovered_mac: str | None = None
        self._discovered_model: str | None = None

    # ── Manual entry ────────────────────────────────────────────────

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        last_host: str | None = None
        last_port: int = DEFAULT_PORT
        if user_input is not None:
            last_host = user_input.get(CONF_HOST)
            last_port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            try:
                info = await _validate_connection(
                    self.hass,
                    host=user_input[CONF_HOST],
                    port=last_port,
                    username=user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                    password=user_input[CONF_PASSWORD],
                )
            except AshlyConnectionError:
                errors["base"] = "cannot_connect"
            except AshlyAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during setup")
                errors["base"] = "unknown"
            else:
                if not info.mac_address:
                    # `no_mac` is permanent for this device until firmware is
                    # updated; abort rather than re-show the same form.
                    return self.async_abort(reason="no_mac")
                mac = format_mac(info.mac_address)
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=_entry_title(info), data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=USER_SCHEMA,
            errors=errors,
            description_placeholders={"host": last_host or "", "port": str(last_port)},
        )

    # ── DHCP discovery ──────────────────────────────────────────────

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        # Normalise the MAC (HA may pass colon, hyphen, or compact forms)
        # before the OUI prefix check.
        try:
            mac = format_mac(discovery_info.macaddress)
        except (AttributeError, TypeError, ValueError):
            return self.async_abort(reason="not_ashly_device")
        if not mac.replace(":", "").upper().startswith(ASHLY_MAC_PREFIX):
            return self.async_abort(reason="not_ashly_device")

        self._discovered_host = discovery_info.ip
        self._discovered_mac = mac

        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured(updates={CONF_HOST: discovery_info.ip})

        hostname = discovery_info.hostname or ""
        if "_" in hostname:
            self._discovered_model = hostname.split("_")[0].upper()

        self.context["title_placeholders"] = {"name": f"Ashly {self._discovered_model or 'Audio'}"}
        return await self.async_step_discovery_confirm()

    # ── Zeroconf / mDNS discovery ───────────────────────────────────

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """Handle mDNS-advertised AQM devices.

        Static-IP commercial AV installs rarely emit a DHCP lease packet HA
        can see, so DHCP discovery often misses them. The AquaControl Portal
        advertises itself via mDNS as `_http._tcp.local.` with a hostname
        like `aqm1208_0014AA112233.local.`. Match by hostname prefix (the
        manifest also filters on `aqm*` / `ashly*` so we only get called
        for likely-Ashly devices).

        MAC may be in the properties dict; if absent, fall back to probing
        the device for its system info.
        """
        host = discovery_info.host
        hostname = (discovery_info.hostname or "").lower()
        # The manifest already prefilters by hostname prefix, but defence in
        # depth — refuse anything that doesn't look like an AQM device.
        if not hostname.startswith(("aqm", "ashly")):
            return self.async_abort(reason="not_ashly_device")

        # Extract MAC from the hostname suffix (`aqm1208_0014AA112233.local.`).
        mac: str | None = None
        bare = hostname.removesuffix(".local.").removesuffix(".local")
        if "_" in bare:
            tail = bare.rsplit("_", 1)[-1]
            if len(tail) == 12 and all(c in "0123456789abcdef" for c in tail):
                try:
                    mac = format_mac(tail)
                except (AttributeError, TypeError, ValueError):
                    mac = None

        # If hostname didn't carry the MAC, probe properties.
        if mac is None:
            mac_prop = discovery_info.properties.get("macaddress") or discovery_info.properties.get(
                "mac"
            )
            if isinstance(mac_prop, str):
                try:
                    mac = format_mac(mac_prop)
                except (AttributeError, TypeError, ValueError):
                    mac = None

        if mac is None:
            # No MAC available; skip the OUI prefix check but defer unique-id
            # assignment until discovery_confirm probes the device.
            self._discovered_host = host
            self._discovered_port = DEFAULT_PORT
            if "_" in bare:
                self._discovered_model = bare.split("_")[0].upper()
            self.context["title_placeholders"] = {
                "name": f"Ashly {self._discovered_model or 'Audio'}"
            }
            return await self.async_step_discovery_confirm()

        if not mac.replace(":", "").upper().startswith(ASHLY_MAC_PREFIX):
            return self.async_abort(reason="not_ashly_device")

        self._discovered_host = host
        self._discovered_mac = mac
        if "_" in bare:
            self._discovered_model = bare.split("_")[0].upper()
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        self.context["title_placeholders"] = {"name": f"Ashly {self._discovered_model or 'Audio'}"}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._discovered_host is not None
            # Allow the user to override the port from the confirm dialog
            # for deployments where AquaControl is on a remapped port.
            self._discovered_port = int(user_input.get(CONF_PORT, self._discovered_port))
            try:
                info = await _validate_connection(
                    self.hass,
                    host=self._discovered_host,
                    port=self._discovered_port,
                    username=user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                    password=user_input[CONF_PASSWORD],
                )
            except AshlyConnectionError:
                errors["base"] = "cannot_connect"
            except AshlyAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during discovery setup")
                errors["base"] = "unknown"
            else:
                # If the actual MAC drifts from what DHCP advertised, abort.
                if (
                    info.mac_address
                    and self._discovered_mac
                    and format_mac(info.mac_address) != self._discovered_mac
                ):
                    return self.async_abort(reason="unique_id_mismatch")
                return self.async_create_entry(
                    title=_entry_title(info),
                    data={
                        CONF_HOST: self._discovered_host,
                        CONF_PORT: self._discovered_port,
                        CONF_USERNAME: user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        # The discovery form gets the port field as well so the user can
        # override 8000 for a remapped deployment without falling back to
        # the manual step.
        discovery_schema_with_port = vol.Schema(
            {
                vol.Optional(CONF_PORT, default=self._discovered_port): NumberSelector(
                    NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=discovery_schema_with_port,
            description_placeholders={
                "model": self._discovered_model or "Audio Processor",
                "host": self._discovered_host or "",
                "port": str(self._discovered_port),
            },
            errors=errors,
        )

    # ── Re-authentication ───────────────────────────────────────────

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        # Stash the host for use in the confirm dialog's description so the
        # user knows *which* device is asking for credentials (matters when
        # multiple AQMs are configured).
        self._reauth_host: str = entry_data.get(CONF_HOST, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        host = reauth_entry.data[CONF_HOST]
        port = int(reauth_entry.data.get(CONF_PORT, DEFAULT_PORT))

        if user_input is not None:
            try:
                info = await _validate_connection(
                    self.hass,
                    host=host,
                    port=port,
                    username=user_input[CONF_USERNAME],
                    password=user_input[CONF_PASSWORD],
                )
            except AshlyConnectionError:
                errors["base"] = "cannot_connect"
            except AshlyAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during re-authentication")
                errors["base"] = "unknown"
            else:
                # Make sure the device on the other end is still the same
                # one this entry was set up against; if the IP now points at
                # a different Ashly device, abort rather than silently bind.
                if (
                    info.mac_address
                    and reauth_entry.unique_id
                    and format_mac(info.mac_address) != reauth_entry.unique_id
                ):
                    return self.async_abort(reason="unique_id_mismatch")
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        # Pre-fill the username field from the existing entry.
        prefilled = self.add_suggested_values_to_schema(
            REAUTH_SCHEMA,
            {CONF_USERNAME: reauth_entry.data.get(CONF_USERNAME, DEFAULT_USERNAME)},
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=prefilled,
            description_placeholders={
                "name": reauth_entry.title,
                "host": host,
                "port": str(port),
            },
            errors=errors,
        )

    # ── Reconfiguration ────────────────────────────────────────────

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()
        current_host = reconfigure_entry.data.get(CONF_HOST, "")
        current_port = int(reconfigure_entry.data.get(CONF_PORT, DEFAULT_PORT))

        if user_input is not None:
            current_host = user_input.get(CONF_HOST, current_host)
            current_port = int(user_input.get(CONF_PORT, current_port))
            try:
                info = await _validate_connection(
                    self.hass,
                    host=user_input[CONF_HOST],
                    port=current_port,
                    username=user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                    password=user_input[CONF_PASSWORD],
                )
            except AshlyConnectionError:
                errors["base"] = "cannot_connect"
            except AshlyAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during reconfiguration")
                errors["base"] = "unknown"
            else:
                if not info.mac_address:
                    # As with the user step, no_mac is a hardware/firmware-level
                    # condition the user can't fix in this dialog; abort.
                    return self.async_abort(reason="no_mac")
                mac = format_mac(info.mac_address)
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    reconfigure_entry, data_updates=user_input
                )

        suggested = {
            CONF_HOST: reconfigure_entry.data.get(CONF_HOST, ""),
            CONF_PORT: reconfigure_entry.data.get(CONF_PORT, DEFAULT_PORT),
            CONF_USERNAME: reconfigure_entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
        }
        schema = self.add_suggested_values_to_schema(USER_SCHEMA, suggested)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "name": reconfigure_entry.title,
                "current_host": current_host or "",
                "current_port": str(current_port),
                "host": current_host or "",
                "port": str(current_port),
            },
        )

    # ── Options ─────────────────────────────────────────────────────

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> AshlyOptionsFlow:
        return AshlyOptionsFlow()


class AshlyOptionsFlow(OptionsFlow):
    """Options flow for the Ashly integration."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current_interval = self.config_entry.options.get("poll_interval", DEFAULT_SCAN_INTERVAL)
        # Floor raised from 5 to 10 seconds: a busy embedded device handles a
        # 9-endpoint gather every 5s poorly; 10s+ is conflict-free in practice.
        schema = vol.Schema(
            {
                vol.Optional("poll_interval", default=current_interval): NumberSelector(
                    NumberSelectorConfig(min=10, max=300, step=5, mode=NumberSelectorMode.SLIDER)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
