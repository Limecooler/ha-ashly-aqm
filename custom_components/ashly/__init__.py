"""The Ashly Audio integration."""

from __future__ import annotations

import aiohttp
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .client import AshlyAuthError, AshlyClient, AshlyConnectionError
from .const import (
    CONF_PORT,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DOMAIN,
    PLATFORMS,
    PUSH_WS_PORT,
)
from .coordinator import AshlyConfigEntry, AshlyCoordinator, AshlyData
from .meter import AshlyMeterClient
from .push import AshlyPushClient
from .services import async_register_services, async_unregister_services


async def async_setup_entry(hass: HomeAssistant, entry: AshlyConfigEntry) -> bool:
    """Set up Ashly Audio from a config entry.

    The device's REST API uses cookie auth, so we ask HA for a session with a
    dedicated cookie jar; HA owns the session lifecycle. A second connection
    (socket.io on port 8001) is opened by `AshlyMeterClient` to stream live
    per-channel signal meters; it reuses the same cookie jar so REST-driven
    re-auth transparently refreshes the websocket on its next reconnect.
    """
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    session = async_create_clientsession(hass, cookie_jar=cookie_jar)

    # HA's NumberSelector returns floats; coerce to int defensively so the
    # URL builder doesn't end up with `:8000.0` (which aiohttp rejects).
    port = int(entry.data.get(CONF_PORT, DEFAULT_PORT))

    client = AshlyClient(
        host=entry.data[CONF_HOST],
        port=port,
        session=session,
        username=entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
        password=entry.data.get(CONF_PASSWORD, DEFAULT_PASSWORD),
    )

    try:
        await client.async_login()
    except AshlyAuthError as err:
        raise ConfigEntryAuthFailed from err
    except AshlyConnectionError as err:
        raise ConfigEntryNotReady from err

    coordinator = AshlyCoordinator(hass, client, entry)
    await coordinator.async_config_entry_first_refresh()

    meter_client = AshlyMeterClient(
        host=entry.data[CONF_HOST],
        port=port,
        cookie_jar=cookie_jar,
    )
    # Start the websocket reconnect loop in the background; it survives
    # transient disconnects without affecting entity availability.
    await meter_client.async_start()

    # Construct (but don't yet start) the push client. Starting after
    # `async_forward_entry_setups` means a slow or unreachable push
    # endpoint cannot delay HA's "entry ready" signal — platforms
    # come up on coordinator state alone, push joins asynchronously.
    push_client = AshlyPushClient(
        hass=hass,
        coordinator=coordinator,
        host=entry.data[CONF_HOST],
        rest_port=port,
        ws_port=PUSH_WS_PORT,
        username=entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
        password=entry.data.get(CONF_PASSWORD, DEFAULT_PASSWORD),
        session=session,
    )

    entry.runtime_data = AshlyData(
        client=client,
        coordinator=coordinator,
        meter_client=meter_client,
        push_client=push_client,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Now safe to bring up push — entry is already 'ready' from HA's POV.
    # Wrap the remaining setup work so a failure here tears down the
    # background WS clients before re-raising. Otherwise HA marks the
    # entry as setup_error but the meter and push tasks keep running,
    # and a retry creates a second pair of clients against the same
    # device.
    try:
        await push_client.async_start()
        async_register_services(hass)
    except Exception:
        await push_client.async_stop()
        await meter_client.async_stop()
        # Tear down the coordinator's poll timer too; if this raise
        # happens after platforms are forwarded but before HA reaches
        # LOADED, the framework won't call async_unload_entry and the
        # interval timer would linger.
        await coordinator.async_shutdown()
        raise

    # Reload the entry whenever options change so a new poll interval takes
    # effect immediately.
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))

    # Stop both websockets *before* HA tears the event loop down. The
    # regular async_unload_entry path still runs during shutdown (HA unloads
    # each entry), but doing this on the bus event means the background
    # asyncio tasks can't race the loop close and log spurious "Task was
    # destroyed but it is pending" warnings. Stop push first so any in-
    # flight handler can't observe a half-closed meter session.
    async def _async_on_ha_stop(_event: Event) -> None:
        await push_client.async_stop()
        await meter_client.async_stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_ha_stop))
    return True


async def _async_reload_on_options(hass: HomeAssistant, entry: AshlyConfigEntry) -> None:
    """Reload the integration when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AshlyConfigEntry) -> bool:
    """Unload an Ashly Audio config entry.

    Stops the push websocket *before* awaiting platform unload so any
    in-flight push event can't fire into half-removed entities. The
    meter websocket is stopped after platform unload (it only writes
    to sensors via its own listener mechanism, which entity teardown
    has already disconnected).

    Integration-wide services are deregistered once the last entry
    unloads.
    """
    # Stop push first — its dispatcher writes to the coordinator, which
    # would race entity removal otherwise. Push uses a `_stopping` flag
    # to short-circuit any handler in flight when stop is called.
    push_client = entry.runtime_data.push_client
    if push_client is not None:
        await push_client.async_stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # Always clear repair issues — even when platform unload partially failed
    # we don't want orphaned issues in the registry pointing at a dead entry.
    ir.async_delete_issue(hass, DOMAIN, f"default_credentials_{entry.entry_id}")
    ir.async_delete_issue(hass, DOMAIN, f"device_unreachable_{entry.entry_id}")
    ir.async_delete_issue(hass, DOMAIN, f"push_stale_{entry.entry_id}")
    if unload_ok:
        meter_client = entry.runtime_data.meter_client
        if meter_client is not None:
            await meter_client.async_stop()
        # Drop services when no other Ashly entries remain.
        remaining = [
            e for e in hass.config_entries.async_entries(DOMAIN) if e.entry_id != entry.entry_id
        ]
        if not remaining:
            async_unregister_services(hass)
    return unload_ok
