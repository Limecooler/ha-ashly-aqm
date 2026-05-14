"""Config flow for the Ashly Audio integration."""

from __future__ import annotations

import logging
import secrets
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
    BooleanSelector,
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

from .client import (
    AshlyApiError,
    AshlyAuthError,
    AshlyClient,
    AshlyConnectionError,
    SystemInfo,
)
from .const import (
    ASHLY_MAC_PREFIX,
    CONF_CREATE_SERVICE_ACCOUNT,
    CONF_PORT,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USERNAME,
    DOMAIN,
    SERVICE_ACCOUNT_PASSWORD_HEX_BYTES,
    SERVICE_ACCOUNT_PERMISSIONS,
    SERVICE_ACCOUNT_ROLE,
    SERVICE_ACCOUNT_USERNAME,
)

_LOGGER = logging.getLogger(__name__)

# Step 1 schema: host (+ optional port) only. We auto-try the factory creds
# on submit; if those fail we route the user to async_step_credentials which
# asks for a password explicitly.
HOST_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): NumberSelector(
            NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
        ),
    }
)

# Fallback step when the factory creds don't work: ask the user explicitly.
CREDENTIALS_SCHEMA = vol.Schema(
    {
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


async def _login_and_get_info(
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


async def _provision_service_account(
    hass: HomeAssistant,
    host: str,
    port: int,
    admin_username: str,
    admin_password: str,
) -> str:
    """Create the dedicated service-account user on the device.

    Authenticates with the supplied admin credentials, idempotently creates
    `haassistant` with the role + permissions documented in
    docs/SECURITY-API.md, returns the generated password. Caller is
    responsible for storing the new credentials in the config entry.
    """
    new_password = secrets.token_hex(SERVICE_ACCOUNT_PASSWORD_HEX_BYTES)
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar(unsafe=True))
    client = AshlyClient(
        host=host,
        port=port,
        session=session,
        username=admin_username,
        password=admin_password,
    )
    await client.async_login()
    await client.async_provision_service_account(
        username=SERVICE_ACCOUNT_USERNAME,
        password=new_password,
        role_type_id=SERVICE_ACCOUNT_ROLE,
        permissions=SERVICE_ACCOUNT_PERMISSIONS,
    )
    # Sanity-check: the new user can log in. If this fails, the provisioning
    # silently failed and we should refuse to store creds we can't use.
    verify_session = async_create_clientsession(
        hass, cookie_jar=aiohttp.CookieJar(unsafe=True)
    )
    verify = AshlyClient(
        host=host,
        port=port,
        session=verify_session,
        username=SERVICE_ACCOUNT_USERNAME,
        password=new_password,
    )
    await verify.async_login()
    return new_password


def _entry_title(info: SystemInfo) -> str:
    """Render a config entry title from device info."""
    return info.name or f"Ashly {info.model}"


def _is_default_creds(username: str, password: str) -> bool:
    return username == DEFAULT_USERNAME and password == DEFAULT_PASSWORD


class AshlyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Ashly Audio integration."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._discovered_host: str | None = None
        self._discovered_port: int = DEFAULT_PORT
        self._discovered_mac: str | None = None
        self._discovered_model: str | None = None
        # State carried across user → credentials → service_account.
        self._pending_host: str | None = None
        self._pending_port: int = DEFAULT_PORT
        self._pending_username: str | None = None
        self._pending_password: str | None = None
        self._pending_info: SystemInfo | None = None

    # ── Manual entry: step 1 (host) ─────────────────────────────────

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect host (+ port). Auto-try factory creds; route accordingly."""
        errors: dict[str, str] = {}
        last_host: str | None = None
        last_port: int = DEFAULT_PORT
        if user_input is not None:
            last_host = user_input.get(CONF_HOST)
            last_port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            self._pending_host = user_input[CONF_HOST]
            self._pending_port = last_port

            # Try the factory credentials silently. If they work, we can
            # offer the service-account flow without ever prompting the user
            # for a password.
            try:
                info = await _login_and_get_info(
                    self.hass,
                    host=self._pending_host,
                    port=self._pending_port,
                    username=DEFAULT_USERNAME,
                    password=DEFAULT_PASSWORD,
                )
            except AshlyAuthError:
                # Defaults rejected → device has been hardened, ask for creds.
                return await self.async_step_credentials()
            except AshlyConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during setup")
                errors["base"] = "unknown"
            else:
                # Defaults worked. Validate MAC + uniqueness, then advance.
                abort = await self._maybe_abort_on_mac(info)
                if abort:
                    return abort
                self._pending_username = DEFAULT_USERNAME
                self._pending_password = DEFAULT_PASSWORD
                self._pending_info = info
                return await self.async_step_service_account()

        return self.async_show_form(
            step_id="user",
            data_schema=HOST_SCHEMA,
            errors=errors,
            description_placeholders={"host": last_host or "", "port": str(last_port)},
        )

    # ── Manual entry: step 2 (credentials, only when defaults fail) ─

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user for credentials. Reached when factory defaults fail."""
        errors: dict[str, str] = {}
        host = self._pending_host or ""
        port = self._pending_port

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, DEFAULT_USERNAME)
            password = user_input[CONF_PASSWORD]
            try:
                info = await _login_and_get_info(
                    self.hass, host=host, port=port, username=username, password=password
                )
            except AshlyConnectionError:
                errors["base"] = "cannot_connect"
            except AshlyAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during credentials step")
                errors["base"] = "unknown"
            else:
                abort = await self._maybe_abort_on_mac(info)
                if abort:
                    return abort
                self._pending_username = username
                self._pending_password = password
                self._pending_info = info
                # If the operator typed the factory creds anyway, still offer
                # the service-account flow.
                if _is_default_creds(username, password):
                    return await self.async_step_service_account()
                return self._finalise(username, password)

        return self.async_show_form(
            step_id="credentials",
            data_schema=CREDENTIALS_SCHEMA,
            errors=errors,
            description_placeholders={"host": host, "port": str(port)},
        )

    # ── Manual entry: step 3 (service account offer) ────────────────

    async def async_step_service_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer to create a dedicated service-account user.

        Only reached when the admin password is the factory default — that's
        the situation where the integration should help the user provision a
        less-privileged account rather than running on `admin/secret`.
        """
        errors: dict[str, str] = {}
        host = self._pending_host or ""
        port = self._pending_port
        admin_user = self._pending_username or DEFAULT_USERNAME
        admin_pass = self._pending_password or DEFAULT_PASSWORD

        if user_input is not None:
            if user_input.get(CONF_CREATE_SERVICE_ACCOUNT, True):
                try:
                    new_pw = await _provision_service_account(
                        self.hass,
                        host=host,
                        port=port,
                        admin_username=admin_user,
                        admin_password=admin_pass,
                    )
                except AshlyConnectionError:
                    errors["base"] = "cannot_connect"
                except AshlyAuthError:
                    errors["base"] = "invalid_auth"
                except AshlyApiError as err:
                    _LOGGER.error("Service-account provisioning failed: %s", err)
                    errors["base"] = "provision_failed"
                except Exception:
                    _LOGGER.exception("Unexpected error during service-account provisioning")
                    errors["base"] = "unknown"
                else:
                    return self._finalise(SERVICE_ACCOUNT_USERNAME, new_pw)
            else:
                # User declined; keep the admin credentials.
                return self._finalise(admin_user, admin_pass)

        schema = vol.Schema(
            {
                vol.Optional(CONF_CREATE_SERVICE_ACCOUNT, default=True): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="service_account",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "host": host,
                "port": str(port),
                "service_user": SERVICE_ACCOUNT_USERNAME,
            },
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
        """Handle mDNS-advertised AQM devices."""
        host = discovery_info.host
        hostname = (discovery_info.hostname or "").lower()
        if not hostname.startswith(("aqm", "ashly")):
            return self.async_abort(reason="not_ashly_device")

        # Extract MAC from the hostname suffix (`aqm1208_0014AA112233.local.`).
        mac: str | None = None
        bare = hostname.removesuffix(".local.").removesuffix(".local")
        if "_" in bare:
            tail = bare.rsplit("_", 1)[-1].lower()
            if len(tail) == 12 and all(c in "0123456789abcdef" for c in tail):
                try:
                    mac = format_mac(tail)
                except (AttributeError, TypeError, ValueError):
                    mac = None

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
            return self.async_abort(reason="not_ashly_device")

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
        """Auto-try factory credentials; fall through to credentials prompt."""
        errors: dict[str, str] = {}
        assert self._discovered_host is not None
        host = self._discovered_host

        if user_input is not None:
            # The form just collects an optional port override.
            self._discovered_port = int(user_input.get(CONF_PORT, self._discovered_port))
            self._pending_host = host
            self._pending_port = self._discovered_port
            try:
                info = await _login_and_get_info(
                    self.hass,
                    host=host,
                    port=self._discovered_port,
                    username=DEFAULT_USERNAME,
                    password=DEFAULT_PASSWORD,
                )
            except AshlyAuthError:
                return await self.async_step_credentials()
            except AshlyConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during discovery setup")
                errors["base"] = "unknown"
            else:
                # Same MAC-drift safety check that the previous flow did.
                if (
                    info.mac_address
                    and self._discovered_mac
                    and format_mac(info.mac_address) != self._discovered_mac
                ):
                    return self.async_abort(reason="unique_id_mismatch")
                self._pending_username = DEFAULT_USERNAME
                self._pending_password = DEFAULT_PASSWORD
                self._pending_info = info
                return await self.async_step_service_account()

        discovery_schema = vol.Schema(
            {
                vol.Optional(CONF_PORT, default=self._discovered_port): NumberSelector(
                    NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
                ),
            }
        )
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=discovery_schema,
            description_placeholders={
                "model": self._discovered_model or "Audio Processor",
                "host": host,
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
                info = await _login_and_get_info(
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
                info = await _login_and_get_info(
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
                    return self.async_abort(reason="no_mac")
                mac = format_mac(info.mac_address)
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_mismatch()
                stored = {**user_input, CONF_PORT: current_port}
                return self.async_update_reload_and_abort(reconfigure_entry, data_updates=stored)

        # Reconfigure preserves the original 4-field form — the user knows
        # what they want to change here, so we don't auto-try defaults.
        full_reconfigure_schema = vol.Schema(
            {
                vol.Required(CONF_HOST): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): NumberSelector(
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
        suggested = {
            CONF_HOST: reconfigure_entry.data.get(CONF_HOST, ""),
            CONF_PORT: reconfigure_entry.data.get(CONF_PORT, DEFAULT_PORT),
            CONF_USERNAME: reconfigure_entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
        }
        schema = self.add_suggested_values_to_schema(full_reconfigure_schema, suggested)
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

    # ── Helpers ────────────────────────────────────────────────────

    async def _maybe_abort_on_mac(self, info: SystemInfo) -> ConfigFlowResult | None:
        """Run the standard MAC-uniqueness checks. Returns an abort result or None."""
        if not info.mac_address:
            return self.async_abort(reason="no_mac")
        mac = format_mac(info.mac_address)
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()
        return None

    def _finalise(self, username: str, password: str) -> ConfigFlowResult:
        """Create the entry from the resolved credentials + cached info."""
        assert self._pending_info is not None
        assert self._pending_host is not None
        return self.async_create_entry(
            title=_entry_title(self._pending_info),
            data={
                CONF_HOST: self._pending_host,
                CONF_PORT: self._pending_port,
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
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
