"""Entity-contract regression tests.

Locks in the set of entity unique_id keys, translation_key values, and
disabled-by-default flags for each platform. A regression that renames a
key or flips a default shows up here as an explicit diff in CI, not as a
silent change to user-visible entity_ids.

Originally intended to use `syrupy` snapshots, but those require initial
recording outside CI; explicit assertions are easier to review and don't
add a hidden dependency.
"""

from __future__ import annotations


def _key(ent) -> str:
    return ent.entity_description.key


def _translation_key(ent) -> str | None:
    return ent.entity_description.translation_key


def _enabled_default(ent) -> bool:
    return ent.entity_description.entity_registry_enabled_default


async def test_switch_platform_entity_keys(hass, mock_config_entry, mock_coordinator) -> None:
    from custom_components.ashly import switch

    mock_config_entry.runtime_data = type(
        "RT", (), {"coordinator": mock_coordinator, "client": mock_coordinator.client}
    )()
    added: list = []
    await switch.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))

    keys = {_key(e) for e in added}
    # 1 power + 12 in + 8 out chain mutes + 12 DCA + 96 xp + front panel + 12 phantom + 2 gpo.
    expected_count = 1 + 12 + 8 + 12 + 96 + 1 + 12 + 2
    assert len(added) == expected_count
    # Spot-check the static keys.
    assert "power" in keys
    assert "front_panel_leds" in keys
    assert "chain_mute_InputChannel.1" in keys
    assert "chain_mute_OutputChannel.8" in keys
    assert "dvca_mute_1" in keys
    assert "xp_mute_m1_i1" in keys
    assert "phantom_power_1" in keys
    assert "gpo_2" in keys

    # Disabled-by-default invariant: every crosspoint mute is disabled.
    disabled_by_default = {_key(e) for e in added if not _enabled_default(e)}
    assert all(k.startswith("xp_mute_") for k in disabled_by_default)
    assert len(disabled_by_default) == 96


async def test_number_platform_entity_keys(hass, mock_config_entry, mock_coordinator) -> None:
    from custom_components.ashly import number

    mock_config_entry.runtime_data = type(
        "RT", (), {"coordinator": mock_coordinator, "client": mock_coordinator.client}
    )()
    added: list = []
    await number.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))
    keys = {_key(e) for e in added}
    assert len(added) == 12 + 96 + 12  # dvca + crosspoint + mic preamp
    assert "dvca_level_1" in keys
    assert "xp_level_m1_i1" in keys
    assert "mic_preamp_1" in keys
    disabled = {_key(e) for e in added if not _enabled_default(e)}
    assert all(k.startswith("xp_level_") for k in disabled)
    assert len(disabled) == 96


async def test_select_platform_entity_keys(hass, mock_config_entry, mock_coordinator) -> None:
    from custom_components.ashly import select

    mock_config_entry.runtime_data = type(
        "RT", (), {"coordinator": mock_coordinator, "client": mock_coordinator.client}
    )()
    added: list = []
    await select.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))
    assert len(added) == 8
    assert {_key(e) for e in added} == {f"output_mixer_{n}" for n in range(1, 9)}
    assert all(_translation_key(e) == "output_mixer" for e in added)


async def test_button_platform_entity_keys(hass, mock_config_entry, mock_coordinator) -> None:
    from custom_components.ashly import button

    mock_config_entry.runtime_data = type(
        "RT", (), {"coordinator": mock_coordinator, "client": mock_coordinator.client}
    )()
    added: list = []
    await button.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))
    # 1 identify + 2 preset buttons (Preset 1, Preset 2 from mock fixture).
    assert len(added) == 3
    keys = {_key(e) for e in added}
    assert keys == {"identify", "recall_preset_Preset 1", "recall_preset_Preset 2"}
    # Preset buttons disabled by default; identify enabled.
    disabled = {_key(e) for e in added if not _enabled_default(e)}
    assert disabled == {"recall_preset_Preset 1", "recall_preset_Preset 2"}


async def test_sensor_platform_entity_keys(
    hass, mock_config_entry, mock_coordinator, mock_meter_client
) -> None:
    from custom_components.ashly import sensor

    mock_config_entry.runtime_data = type(
        "RT",
        (),
        {
            "coordinator": mock_coordinator,
            "client": mock_coordinator.client,
            "meter_client": mock_meter_client,
        },
    )()
    added: list = []
    await sensor.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))
    # 4 diagnostic (firmware, preset_count, last_recalled, ip_address)
    # + 12 input meters + 12 mixer meters.
    assert len(added) == 4 + 12 + 12
    keys = {_key(e) for e in added}
    assert "firmware_version" in keys
    assert "preset_count" in keys
    assert "last_recalled_preset" in keys
    assert "ip_address" in keys
    assert "meter_input_1" in keys
    assert "meter_mixer_12" in keys
    # IP address and last_recalled_preset are the only sensors enabled by
    # default; the rest (meters + firmware/preset_count) are disabled so a
    # fresh install doesn't get 26 entities cluttering the device card.
    disabled = {_key(e) for e in added if not _enabled_default(e)}
    assert "last_recalled_preset" not in disabled
    assert "ip_address" not in disabled
