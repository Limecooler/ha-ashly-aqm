"""Async HTTP client for the Ashly AquaControl REST API.

Targets API version `v1.0-beta` exposed by AQM-series devices on port 8000.
All endpoints used here are cookie-authenticated; the SimpleControl tree
is intentionally not used because it requires a separate device user.
"""

from __future__ import annotations

import asyncio
import json as json_mod
import logging
import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import (
    DVCA_LEVEL_ID,
    DVCA_MUTE_ID,
    GPO_PIN_ID,
    INPUT_CHANNEL_ID,
    MIXER_SOURCE_LEVEL_ID,
    MIXER_SOURCE_MUTE_ID,
    NO_MIXER,
    NUM_DVCA_GROUPS,
    NUM_GPO,
    NUM_INPUTS,
    NUM_MIXERS,
    OUTPUT_CHANNEL_ID,
)

# Channel and mixer identifiers we will accept in URL paths.
_CHANNEL_ID_RE = re.compile(r"^(InputChannel|OutputChannel)\.\d{1,3}$")
_MIXER_ID_RE = re.compile(r"^(?:None|Mixer\.\d{1,3})$")


def _to_bool(value: Any, default: bool = False) -> bool:
    """Coerce device-returned boolean-ish values robustly.

    The device occasionally returns `"true"`/`"false"` strings for fields
    typed as boolean in the spec; `bool("false")` is `True`, so we normalise.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "on", "yes")
    if value is None:
        return default
    return bool(value)


def _to_float(value: Any, default: float = 0.0) -> float:
    """Coerce device-returned numeric values to float."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


_LOGGER = logging.getLogger(__name__)

API_PREFIX = "/v1.0-beta"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
MAX_CONCURRENT_REQUESTS = 4


# ── Exceptions ──────────────────────────────────────────────────────────


class AshlyError(Exception):
    """Base exception for Ashly client errors."""


class AshlyConnectionError(AshlyError):
    """Raised when the device cannot be reached."""


class AshlyTimeoutError(AshlyConnectionError):
    """Raised when a single HTTP request times out.

    Subclasses `AshlyConnectionError` so existing handlers that catch
    connection errors continue to work; downstream code that wants to
    distinguish a brief per-request timeout from a full connection
    failure (e.g. for best-effort endpoint reuse) catches this first.
    """


class AshlyAuthError(AshlyError):
    """Raised on 401/403 authentication failures."""


class AshlyApiError(AshlyError):
    """Raised on unexpected API errors."""


# ── Data shapes ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class SystemInfo:
    """Device system information.

    The device API does not expose a serial number; we use the MAC address as
    the unique identifier instead.
    """

    model: str
    name: str
    firmware_version: str
    hardware_revision: str
    mac_address: str
    has_auto_mix: bool


@dataclass(slots=True)
class ChainState:
    """Per-channel chain state (mute, optional output → mixer assignment)."""

    channel_id: str
    muted: bool
    mixer_id: str | None  # only meaningful for output channels


@dataclass(slots=True)
class DSPChannel:
    """A logical I/O channel as exposed by the device."""

    channel_id: str
    name: str
    default_name: str
    base_type: str  # "Input" | "Output"
    channel_number: int


@dataclass(slots=True)
class DVCAState:
    """State of a virtual DCA group."""

    index: int
    name: str
    level_db: float
    muted: bool


@dataclass(slots=True)
class CrosspointState:
    """One mixer x input crosspoint."""

    mixer_index: int
    input_index: int
    level_db: float
    muted: bool


@dataclass(slots=True)
class PresetInfo:
    """A stored preset record.

    On AQM-family devices `id` and `name` are the same string — the
    device keys presets by their user-set name.
    """

    id: str
    name: str


@dataclass(slots=True)
class LastRecalledPreset:
    """Result of `/preset/lastRecalled`."""

    name: str | None  # "None" string from device → Python None
    modified: bool


@dataclass(slots=True)
class FrontPanelInfo:
    """Front-panel configuration shared between power & LED toggles."""

    power_on: bool
    leds_enabled: bool


# ── Client ──────────────────────────────────────────────────────────────


class AshlyClient:
    """Async client for the Ashly AquaControl REST API.

    Expects a dedicated `aiohttp.ClientSession` whose `CookieJar` stores the
    session cookie returned by `/session/login`. A semaphore caps concurrent
    in-flight requests so the embedded device is not overwhelmed; an
    `asyncio.Lock` prevents concurrent re-authentication stampedes.
    """

    def __init__(
        self,
        host: str,
        port: int,
        session: aiohttp.ClientSession,
        username: str = "admin",
        password: str = "secret",
    ) -> None:
        self.host = host
        self.port = port
        self._session = session
        self._username = username
        self._password = password
        self._base_url = f"http://{host}:{port}{API_PREFIX}"
        self._auth_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._authenticated = False
        # Bumped on every successful login so concurrent 401-retries skip
        # redundant logins after the first one wins the auth lock.
        self._auth_epoch = 0

    # ── Plumbing ────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def async_login(self, *, expected_epoch: int | None = None) -> None:
        """Authenticate; cookie is stored automatically by the session jar.

        If `expected_epoch` is provided and the current `_auth_epoch` is
        already higher (i.e. another caller logged in while this one was
        waiting on the lock), this call is a no-op. This prevents N
        concurrent 401-retry callers from issuing N redundant logins.

        Validates the response envelope so a 200/`success: false` from the
        device cannot silently pose as a successful auth, which would
        otherwise loop forever on the next 401-retry.
        """
        async with self._auth_lock:
            if expected_epoch is not None and self._auth_epoch > expected_epoch:
                return
            try:
                async with self._session.post(
                    self._url("/session/login"),
                    json={
                        "username": self._username,
                        "password": self._password,
                        "keepLoggedIn": True,
                    },
                    timeout=REQUEST_TIMEOUT,
                ) as resp:
                    # The device returns 401 for wrong-but-valid-format
                    # credentials and 400 ("Invalid request payload
                    # input") for credentials that fail its alphanumeric
                    # schema. Both mean "the user needs new credentials",
                    # so route them through the auth-failure path so HA's
                    # reauth flow fires correctly.
                    if resp.status in (
                        HTTPStatus.BAD_REQUEST,
                        HTTPStatus.UNAUTHORIZED,
                        HTTPStatus.FORBIDDEN,
                    ):
                        # Don't include any response body for auth failures
                        # — some embedded devices echo credentials in
                        # error text.
                        raise AshlyAuthError(f"Authentication failed (HTTP {resp.status})")
                    if resp.status >= HTTPStatus.BAD_REQUEST:
                        raise AshlyApiError(f"Login failed (HTTP {resp.status})")
                    body = await self._parse_json(resp)
                    if isinstance(body, dict) and body.get("success") is False:
                        # Device may return 200 + success:false on locked
                        # accounts or invalid credentials.
                        raise AshlyAuthError(f"Login refused: {body.get('error') or 'unknown'}")
                    self._authenticated = True
                    self._auth_epoch += 1
            except TimeoutError as err:
                raise AshlyTimeoutError(f"Login to {self.host}:{self.port} timed out") from err
            except aiohttp.ClientError as err:
                raise AshlyConnectionError(f"Cannot connect to {self.host}:{self.port}") from err

    async def _parse_json(self, resp: aiohttp.ClientResponse) -> Any:
        try:
            return await resp.json(content_type=None)
        except (json_mod.JSONDecodeError, aiohttp.ContentTypeError) as err:
            try:
                text = await resp.text()
            except aiohttp.ClientError:
                text = "<unable to read body>"
            raise AshlyApiError(f"Invalid JSON response: {text[:200]}") from err

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        retry_auth: bool = True,
    ) -> Any:
        """Make an authenticated request, transparently re-auth on 401.

        On 401 we release the semaphore *before* re-authenticating and
        retrying, so the auth POST and retry don't count against the
        concurrent-request cap (preventing a back-pressure deadlock when
        several callers all hit 401 simultaneously).
        """
        result, need_retry, saw_epoch = await self._request_once(
            method, path, json=json, retry_auth=retry_auth
        )
        if not need_retry:
            return result
        await self.async_login(expected_epoch=saw_epoch)
        # Retry once with retry_auth=False to bound recursion depth.
        result, retry_again, _ = await self._request_once(method, path, json=json, retry_auth=False)
        if retry_again:
            # Defensive: would only happen if retry_auth=False was ignored.
            raise AshlyAuthError("Re-auth did not unblock the request")
        return result

    async def _request_once(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        retry_auth: bool = True,
    ) -> tuple[Any, bool, int]:
        """Single-shot HTTP request.

        Returns `(parsed_body, need_retry, saw_epoch)`. The semaphore is
        held only for the duration of this call; the auth POST and retry
        run outside of it.
        """
        url = self._url(path)
        saw_epoch = self._auth_epoch
        try:
            async with (
                self._semaphore,
                self._session.request(method, url, json=json, timeout=REQUEST_TIMEOUT) as resp,
            ):
                if resp.status == HTTPStatus.UNAUTHORIZED and retry_auth:
                    return None, True, saw_epoch
                if resp.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                    raise AshlyAuthError(f"Authentication failed (HTTP {resp.status})")
                if resp.status == HTTPStatus.NOT_FOUND:
                    raise AshlyApiError(f"Endpoint not found: {url}")
                if resp.status >= HTTPStatus.BAD_REQUEST:
                    try:
                        text = await resp.text()
                    except aiohttp.ClientError:
                        text = "<unable to read body>"
                    raise AshlyApiError(f"API error (HTTP {resp.status}): {text[:200]}")
                return await self._parse_json(resp), False, saw_epoch
        except AshlyError:
            raise
        except TimeoutError as err:
            raise AshlyTimeoutError(f"Request to {self.host}:{self.port} timed out") from err
        except aiohttp.ClientError as err:
            raise AshlyConnectionError(f"Cannot connect to {self.host}:{self.port}") from err

    async def _get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def _post(self, path: str, json: Any | None = None) -> Any:
        return await self._request("POST", path, json=json)

    @staticmethod
    def _unwrap(data: Any) -> Any:
        """Unwrap the device's `{success, data}` envelope.

        Raises `AshlyApiError` if the envelope reports failure or the body
        shape is unexpected. Treats `data: null` as a failure rather than
        silently producing empty objects downstream.
        """
        if not isinstance(data, dict):
            raise AshlyApiError(f"Unexpected response (not an object): {data!r}")
        if data.get("success") is False:
            raise AshlyApiError(f"Device reported failure: {data.get('error') or data}")
        if "data" not in data:
            raise AshlyApiError(f"Response missing 'data' field: {data!r}")
        if data["data"] is None:
            raise AshlyApiError("Device returned 'data: null'")
        return data["data"]

    @staticmethod
    def _validate_channel_id(channel_id: str) -> str:
        """Validate and return a channel id safe for URL path interpolation."""
        if not _CHANNEL_ID_RE.match(channel_id):
            raise ValueError(f"Invalid channel id: {channel_id!r}")
        return channel_id

    @staticmethod
    def _validate_mixer_id(mixer_id: str) -> str:
        """Validate and return a mixer id ('Mixer.N' or 'None')."""
        if not _MIXER_ID_RE.match(mixer_id):
            raise ValueError(f"Invalid mixer id: {mixer_id!r}")
        return mixer_id

    @staticmethod
    def _first_or_empty(data: Any) -> dict[str, Any]:
        """Return the first item if `data` is a list; else the dict itself."""
        if isinstance(data, list):
            return data[0] if data else {}
        if isinstance(data, dict):
            return data
        return {}

    # ── System ──────────────────────────────────────────────────────

    async def async_get_system_info(self) -> SystemInfo:
        """Fetch device system info; combines `/system/info` and `/network`."""
        sys_data, net_data = await asyncio.gather(
            self._get("/system/info"),
            self._get("/network"),
        )
        sys_info = self._first_or_empty(self._unwrap(sys_data))
        net_info = self._first_or_empty(self._unwrap(net_data))
        # Normalise MAC to lower-case colon-separated form so downstream
        # consumers (diagnostics, unique_id) all see the same canonical form.
        mac_raw = str(net_info.get("hwaddress") or "")
        mac = mac_raw.lower().replace("-", ":") if mac_raw else ""
        return SystemInfo(
            model=str(sys_info.get("deviceType") or sys_info.get("DeviceType") or "AQM"),
            name=str(sys_info.get("name") or ""),
            firmware_version=str(sys_info.get("softwareRevision") or ""),
            hardware_revision=str(sys_info.get("hardwareRevision") or ""),
            mac_address=mac,
            has_auto_mix=_to_bool(sys_info.get("hasAutoMix", False)),
        )

    async def async_test_connection(self) -> bool:
        """Return True iff a session is reachable and authenticated."""
        try:
            await self.async_get_system_info()
        except AshlyError:
            return False
        return True

    # ── Front panel (power + LEDs) ──────────────────────────────────

    async def async_get_front_panel(self) -> FrontPanelInfo:
        """Fetch front-panel state (power + LED enable)."""
        data = self._first_or_empty(self._unwrap(await self._get("/system/frontPanel/info")))
        return FrontPanelInfo(
            power_on=str(data.get("powerState", "Off")).lower() == "on",
            leds_enabled=_to_bool(data.get("frontPanelLEDEnable", True), default=True),
        )

    async def async_get_power(self) -> bool:
        """Return True if `powerState` is `On`."""
        return (await self.async_get_front_panel()).power_on

    async def async_set_power(self, on: bool) -> None:
        """Set power state; the device accepts only `On`/`Off`."""
        await self._post(
            "/system/frontPanel/info",
            json={"powerState": "On" if on else "Off"},
        )

    async def async_set_front_panel_leds(self, enabled: bool) -> None:
        """Enable or disable the front-panel LEDs.

        Only the changed field is sent so `powerState` is not clobbered.
        """
        await self._post(
            "/system/frontPanel/info",
            json={"frontPanelLEDEnable": enabled},
        )

    # ── Channel discovery ───────────────────────────────────────────

    async def async_get_channels(self) -> list[DSPChannel]:
        """Return all configured I/O channels (12 inputs + 8 outputs on AQM1208)."""
        raw = self._unwrap(await self._get("/workingsettings/dsp/channel"))
        if not isinstance(raw, list):
            raise AshlyApiError("Unexpected channel list shape")
        channels: list[DSPChannel] = []
        for item in raw:
            if not isinstance(item, dict):
                _LOGGER.debug("[%s] Skipping non-dict channel entry %r", self.host, item)
                continue
            try:
                channels.append(
                    DSPChannel(
                        channel_id=str(item["id"]),
                        name=str(item.get("name") or item.get("defaultName") or ""),
                        default_name=str(item.get("defaultName") or ""),
                        base_type=str(item.get("baseType") or ""),
                        channel_number=int(item.get("channelNumber") or 0),
                    )
                )
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.debug(
                    "[%s] Skipping malformed channel entry %r: %s",
                    self.host,
                    item,
                    err,
                )
        return channels

    # ── Chain state (mutes + output → mixer assignment) ─────────────

    async def async_get_chain_state(self) -> dict[str, ChainState]:
        """Fetch chain mute + mixer assignment for every channel."""
        raw = self._unwrap(await self._get("/workingsettings/dsp/chain"))
        if not isinstance(raw, list):
            raise AshlyApiError("Unexpected chain list shape")
        out: dict[str, ChainState] = {}
        for item in raw:
            if not isinstance(item, dict) or "id" not in item:
                _LOGGER.debug("[%s] Skipping malformed chain entry %r", self.host, item)
                continue
            mixer_id = item.get("mixerId")
            if mixer_id == NO_MIXER:
                mixer_id = None
            out[str(item["id"])] = ChainState(
                channel_id=str(item["id"]),
                muted=_to_bool(item.get("muted", False)),
                mixer_id=mixer_id if isinstance(mixer_id, str) else None,
            )
        return out

    async def async_set_chain_mute(self, channel_id: str, muted: bool) -> None:
        """Mute or unmute a channel chain."""
        cid = self._validate_channel_id(channel_id)
        await self._post(
            f"/workingsettings/dsp/chain/mute/{cid}",
            json={"muted": muted},
        )

    async def async_set_output_mixer(self, output_channel_id: str, mixer_id: str) -> None:
        """Assign a mixer (or `None`) to an output chain.

        `mixer_id` should be `"Mixer.<n>"` or the literal `"None"` to clear.
        """
        cid = self._validate_channel_id(output_channel_id)
        mid = self._validate_mixer_id(mixer_id)
        await self._post(
            f"/workingsettings/dsp/chain/mixer/{cid}",
            json={"mixerId": mid},
        )

    # ── Virtual DCA groups ──────────────────────────────────────────

    async def async_get_dvca_state(self) -> dict[int, DVCAState]:
        """Fetch level + mute + name for every DVCA group."""
        raw = self._unwrap(await self._get("/workingsettings/virtualDVCA/parameters"))
        if not isinstance(raw, list):
            raise AshlyApiError("Unexpected DVCA parameter shape")

        levels: dict[int, float] = {}
        mutes: dict[int, bool] = {}
        names: dict[int, str] = {}
        for item in raw:
            if not isinstance(item, dict):
                _LOGGER.debug("[%s] Skipping non-dict DVCA entry %r", self.host, item)
                continue
            type_id = item.get("DSPParameterTypeId")
            raw_index = item.get("index")
            if raw_index is None:
                _LOGGER.debug("[%s] Skipping DVCA entry with no index", self.host)
                continue
            try:
                idx = int(raw_index)
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "[%s] Skipping DVCA entry with non-int index %r",
                    self.host,
                    item.get("index"),
                )
                continue
            if not 1 <= idx <= NUM_DVCA_GROUPS:
                _LOGGER.debug("[%s] DVCA index %s out of range; ignoring", self.host, idx)
                continue
            value = item.get("value")
            if type_id == "Virtual DCA.Level":
                levels[idx] = _to_float(value)
            elif type_id == "Virtual DCA.Mute":
                mutes[idx] = _to_bool(value)
            elif type_id == "Virtual DCA.Name":
                names[idx] = str(value) if value is not None and value != "" else f"DCA {idx}"

        return {
            i: DVCAState(
                index=i,
                name=names.get(i, f"DCA {i}"),
                level_db=levels.get(i, 0.0),
                muted=mutes.get(i, False),
            )
            for i in range(1, NUM_DVCA_GROUPS + 1)
        }

    async def async_set_dvca_level(self, index: int, level_db: float) -> None:
        """Set DVCA level in dB."""
        self._check_index(index, 1, NUM_DVCA_GROUPS, "DCA")
        await self._post(
            f"/workingsettings/virtualDVCA/parameters/{DVCA_LEVEL_ID.format(n=index)}",
            json={"value": level_db},
        )

    async def async_set_dvca_mute(self, index: int, muted: bool) -> None:
        """Mute or unmute a DVCA group."""
        self._check_index(index, 1, NUM_DVCA_GROUPS, "DCA")
        await self._post(
            f"/workingsettings/virtualDVCA/parameters/{DVCA_MUTE_ID.format(n=index)}",
            json={"value": muted},
        )

    # ── Mixer crosspoints ───────────────────────────────────────────

    async def async_get_crosspoints(self) -> dict[tuple[int, int], CrosspointState]:
        """Fetch every mixer x input source level + mute as a flat dict."""
        raw = self._unwrap(await self._get("/workingsettings/dsp/mixer/config/parameter"))
        if not isinstance(raw, list):
            raise AshlyApiError("Unexpected mixer parameter shape")

        levels: dict[tuple[int, int], float] = {}
        mutes: dict[tuple[int, int], bool] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            type_id = item.get("DSPMixerConfigParameterTypeId")
            if type_id not in ("Mixer.Source Level", "Mixer.Source Mute"):
                continue
            mixer_id = str(item.get("DSPMixerConfigId") or "")
            channel_id = str(item.get("channelId") or "")
            try:
                mixer_idx = int(mixer_id.split(".")[-1])
                input_idx = int(channel_id.split(".")[-1])
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "[%s] Skipping crosspoint with unparseable ids %r/%r",
                    self.host,
                    mixer_id,
                    channel_id,
                )
                continue
            if not (1 <= mixer_idx <= NUM_MIXERS and 1 <= input_idx <= NUM_INPUTS):
                _LOGGER.debug(
                    "[%s] Crosspoint indices out of range: m=%s i=%s",
                    self.host,
                    mixer_idx,
                    input_idx,
                )
                continue
            key = (mixer_idx, input_idx)
            value = item.get("value")
            if type_id == "Mixer.Source Level":
                levels[key] = _to_float(value)
            else:
                mutes[key] = _to_bool(value)

        return {
            (m, i): CrosspointState(
                mixer_index=m,
                input_index=i,
                level_db=levels.get((m, i), 0.0),
                muted=mutes.get((m, i), True),
            )
            for m in range(1, NUM_MIXERS + 1)
            for i in range(1, NUM_INPUTS + 1)
        }

    @staticmethod
    def _check_index(idx: int, lo: int, hi: int, label: str) -> None:
        if not lo <= idx <= hi:
            raise ValueError(f"{label} index {idx} out of range [{lo}..{hi}]")

    async def async_set_crosspoint_level(
        self, mixer_index: int, input_index: int, level_db: float
    ) -> None:
        """Set a single crosspoint source level."""
        self._check_index(mixer_index, 1, NUM_MIXERS, "mixer")
        self._check_index(input_index, 1, NUM_INPUTS, "input")
        param_id = MIXER_SOURCE_LEVEL_ID.format(m=mixer_index, i=input_index)
        # Param ids contain spaces; quote the path segment defensively even
        # though aiohttp will percent-encode for us.
        await self._post(
            f"/workingsettings/dsp/mixer/config/parameter/{quote(param_id, safe='.')}",
            json={"value": level_db},
        )

    async def async_set_crosspoint_mute(
        self, mixer_index: int, input_index: int, muted: bool
    ) -> None:
        """Set a single crosspoint source mute."""
        self._check_index(mixer_index, 1, NUM_MIXERS, "mixer")
        self._check_index(input_index, 1, NUM_INPUTS, "input")
        param_id = MIXER_SOURCE_MUTE_ID.format(m=mixer_index, i=input_index)
        await self._post(
            f"/workingsettings/dsp/mixer/config/parameter/{quote(param_id, safe='.')}",
            json={"value": muted},
        )

    # ── Presets (read-only) ─────────────────────────────────────────

    async def async_get_presets(self) -> list[PresetInfo]:
        """List stored presets keyed by name (device uses `name == id`)."""
        raw = self._unwrap(await self._get("/preset"))
        if not isinstance(raw, list):
            raise AshlyApiError("Unexpected preset list shape")
        out: list[PresetInfo] = []
        for item in raw:
            if not isinstance(item, dict):
                _LOGGER.debug("[%s] Skipping non-dict preset entry %r", self.host, item)
                continue
            name = str(item.get("name") or item.get("id") or "")
            if not name:
                _LOGGER.debug(
                    "[%s] Skipping preset entry without a name: %r",
                    self.host,
                    item,
                )
                continue
            out.append(PresetInfo(id=str(item.get("id") or name), name=name))
        return out

    async def async_recall_preset(self, name: str) -> None:
        """Recall a stored preset by name.

        Working settings are overwritten with the preset's stored state.
        The device updates `lastRecalledPreset` within milliseconds; the
        REST call returns once the recall has completed.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("Preset name must be a non-empty string")
        await self._post(f"/preset/recall/{quote(name, safe='')}", json={})

    # ── Identify ────────────────────────────────────────────────────

    async def async_identify(self) -> None:
        """Trigger the device's identify (front-panel LED blink)."""
        await self._get("/system/identify")

    # ── Phantom power (per input) ───────────────────────────────────

    async def async_get_phantom_power(self) -> dict[int, bool]:
        """Return per-input phantom-power state, keyed by input number."""
        raw = self._unwrap(await self._get("/phantomPower"))
        if not isinstance(raw, list):
            raise AshlyApiError("Unexpected phantom-power list shape")
        out: dict[int, bool] = {}
        for item in raw:
            if not isinstance(item, dict):
                _LOGGER.debug(
                    "[%s] Skipping non-dict phantom entry %r",
                    self.host,
                    item,
                )
                continue
            try:
                idx = int(item["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if 1 <= idx <= NUM_INPUTS:
                out[idx] = _to_bool(item.get("phantomPowerEnabled", False))
        return out

    async def async_set_phantom_power(self, input_index: int, enabled: bool) -> None:
        """Enable or disable +48V phantom power on a mic input."""
        self._check_index(input_index, 1, NUM_INPUTS, "input")
        await self._post(
            f"/phantomPower/{input_index}",
            json={"phantomPowerEnabled": enabled},
        )

    # ── Mic preamp gain (per input) ─────────────────────────────────

    async def async_get_mic_preamp(self) -> dict[int, int]:
        """Return per-input mic-preamp gain (dB), keyed by input number."""
        raw = self._unwrap(await self._get("/micPreamp"))
        if not isinstance(raw, list):
            raise AshlyApiError("Unexpected mic-preamp list shape")
        out: dict[int, int] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if 1 <= idx <= NUM_INPUTS:
                try:
                    out[idx] = int(item.get("gain", 0))
                except (TypeError, ValueError):
                    out[idx] = 0
        return out

    async def async_set_mic_preamp(self, input_index: int, gain_db: int) -> None:
        """Set the mic-preamp gain for a mic input (allowed: 0..66 in 6 dB steps)."""
        self._check_index(input_index, 1, NUM_INPUTS, "input")
        await self._post(
            f"/micPreamp/{input_index}",
            json={"gain": int(gain_db)},
        )

    # ── General-purpose outputs (GPO pins) ──────────────────────────

    async def async_get_gpo(self) -> dict[int, bool]:
        """Return per-pin GPO state (True = high), keyed by pin number."""
        raw = self._unwrap(await self._get("/workingsettings/generalPurposeOutputConfiguration"))
        if not isinstance(raw, list):
            raise AshlyApiError("Unexpected GPO list shape")
        out: dict[int, bool] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("generalPurposeOutputId") or 0)
            except (TypeError, ValueError):
                continue
            if 1 <= idx <= NUM_GPO:
                out[idx] = str(item.get("value", "low")).lower() == "high"
        return out

    async def async_set_gpo(self, pin_index: int, high: bool) -> None:
        """Drive a GPO pin high or low."""
        self._check_index(pin_index, 1, NUM_GPO, "GPO")
        pin_id = GPO_PIN_ID.format(n=pin_index)
        await self._post(
            f"/workingsettings/generalPurposeOutputConfiguration/{quote(pin_id, safe='.')}",
            json={"value": "high" if high else "low"},
        )

    # ── Last recalled preset (read-only) ────────────────────────────

    async def async_get_last_recalled_preset(self) -> LastRecalledPreset:
        """Fetch the name of the most-recently-recalled preset (if any)."""
        data = self._first_or_empty(self._unwrap(await self._get("/preset/lastRecalled")))
        name = data.get("lastRecalledPreset")
        return LastRecalledPreset(
            name=None if name in (None, "", "None") else str(name),
            modified=_to_bool(data.get("modified", False)),
        )


# Helpers used by other modules
def input_channel_id(n: int) -> str:
    """Return the canonical InputChannel.<n> id."""
    return INPUT_CHANNEL_ID.format(n=n)


def output_channel_id(n: int) -> str:
    """Return the canonical OutputChannel.<n> id."""
    return OUTPUT_CHANNEL_ID.format(n=n)
