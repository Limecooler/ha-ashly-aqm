# Contributing to ha-ashly-aqm

Thanks for considering a contribution. This repo is open to:

- Bug reports and feature requests via [GitHub issues](https://github.com/limecooler/ha-ashly-aqm/issues).
- Pull requests for bug fixes, new features, additional model support, and documentation.

Security-sensitive issues should go through [SECURITY.md](./SECURITY.md), not the public tracker.

## Development setup

```sh
# Clone, set up a venv (3.12+; CI runs 3.14)
git clone https://github.com/limecooler/ha-ashly-aqm.git
cd ha-ashly-aqm
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip ruff mypy pytest pytest-homeassistant-custom-component aioresponses 'python-socketio[asyncio_client]>=5.10'
```

## Running the test suite

```sh
# Unit tests only (no device required):
pytest tests/ -v

# Include live-device integration tests (requires ASHLY_HOST env var):
ASHLY_HOST=192.168.1.100 ASHLY_USERNAME=admin ASHLY_PASSWORD=secret pytest tests/ -v -m integration

# With coverage (CI enforces 100%):
pytest tests/ -v --cov=custom_components.ashly --cov-report=term-missing --cov-fail-under=100
```

## Code style

- `ruff format` and `ruff check` enforce formatting and lint rules. CI fails on any deviation.
- `mypy --strict` covers `custom_components/ashly/`. The local helper `socketio` and the
  `homeassistant.helpers.service_info.dhcp` fallback are explicitly ignored in `pyproject.toml`.
- Python 3.12+ syntax (PEP 695 type aliases are OK). HA's runtime is Python 3.14.

## Translations

Two files must be kept in sync:

- `custom_components/ashly/strings.json` — source-of-truth (matches HA core convention).
- `custom_components/ashly/translations/en.json` — the compiled English file the integration
  actually loads at runtime.

When you change one, mirror the change in the other. Hassfest validates that all
`translation_key`s used by entities/services/exceptions/issues exist in `strings.json`.

## Architectural conventions

- All entities have `_attr_has_entity_name = True` and use `translation_key` rather than
  ad-hoc `_attr_name` strings.
- `EntityDescription` subclasses are `@dataclass(frozen=True, kw_only=True)`.
- The DHCP `DhcpServiceInfo` import uses a TYPE_CHECKING / runtime fallback to support
  HA versions before/after 2026.2.
- Runtime data hangs off `entry.runtime_data` (an `AshlyData` dataclass) — not `hass.data[DOMAIN]`.
- MAC addresses go through `format_mac()` everywhere.

## Commit message style

Themed commits; one logical change per commit. Prefix lines are not required.
Reference the file or module being touched in the first line when it's a localised change.

## Releases

The repo is set up so that pushing a `v*` tag on `main` produces a GitHub Release.
Update `manifest.json`'s `version` field in the same commit that bumps `CHANGELOG.md`.

## Filing a good bug report

Helpful things to include:

- `Settings → Devices & services → Ashly Audio → ⋮ → Download diagnostics` output
  (already redacts password / host / MAC).
- Device model and firmware (visible in the `firmware_version` sensor).
- HA version (`hass --version`).
- A reproducer — even a "the integration just stopped working" plus diagnostics is useful.
