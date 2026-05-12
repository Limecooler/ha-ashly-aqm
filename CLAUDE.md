# CLAUDE.md

## Project Overview

Home Assistant custom integration for Ashly Audio AQM series zone mixers (AQM1208, AQM408).
Communicates via AquaControl REST API (HTTP on port 8000).

## Development Commands

- **Lint**: `ruff check custom_components/ tests/`
- **Type check**: `mypy custom_components/ashly/`
- **Test**: `pytest tests/ -v`
- **HACS validate**: Run via GitHub Actions

## Architecture

- `client.py` — async aiohttp client wrapping AquaControl REST API
- `coordinator.py` — DataUpdateCoordinator polling device state
- `config_flow.py` — user setup, DHCP discovery, reauth, reconfigure, options
- Entity platforms: media_player (zones), number (gains), switch (mutes/power), button (presets), sensor (metering)
- Uses `entry.runtime_data` pattern (no `hass.data[DOMAIN]`)

## Code Standards

- Python 3.12+ (PEP 695 type aliases)
- All EntityDescriptions: `@dataclass(frozen=True, kw_only=True)`
- All entities: `_attr_has_entity_name = True`
- Imports: `homeassistant.helpers.service_info.dhcp` (NOT `homeassistant.components.dhcp`)
- Options flow: `OptionsFlow` (NOT `OptionsFlowWithConfigEntry`)
- Use `native_*` properties on NumberEntity
- Use `format_mac()` for all MAC address handling
