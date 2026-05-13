"""Snapshot tests for the entity surface.

Locks in entity unique_id / name / category / disabled-by-default / icon
state-conditional metadata for each platform. A regression that renames a
key or flips a default surfaces here as a diff in
`tests/__snapshots__/test_snapshots.ambr` that the reviewer can audit.
"""

from __future__ import annotations

from typing import Any

import pytest
from syrupy.assertion import SnapshotAssertion


def _entity_descriptor(ent: Any) -> dict[str, Any]:
    """Pull the small set of attributes that define the entity's UX contract."""
    return {
        "unique_id": getattr(ent, "_attr_unique_id", None),
        "translation_key": getattr(ent.entity_description, "translation_key", None),
        "translation_placeholders": getattr(ent, "_attr_translation_placeholders", None),
        "entity_category": (
            ent.entity_description.entity_category.value
            if ent.entity_description.entity_category is not None
            else None
        ),
        "entity_registry_enabled_default": ent.entity_description.entity_registry_enabled_default,
        "device_class": getattr(ent.entity_description, "device_class", None),
    }


@pytest.mark.parametrize(
    "platform_module,entity_count",
    [
        ("switch", 1 + 12 + 8 + 12 + 1 + 12 + 2 + 96),  # see test_switch
        ("number", 12 + 12 + 96),
        ("select", 8),
        ("button", 1 + 2),  # identify + 2 preset buttons
        ("sensor", 3 + 12 + 12),
    ],
)
async def test_entity_descriptor_snapshot(
    hass,
    mock_config_entry,
    mock_coordinator,
    mock_meter_client,
    snapshot: SnapshotAssertion,
    platform_module: str,
    entity_count: int,
) -> None:
    """Capture the entity descriptor set for each platform."""
    from importlib import import_module

    module = import_module(f"custom_components.ashly.{platform_module}")
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
    await module.async_setup_entry(hass, mock_config_entry, lambda x: added.extend(x))
    assert len(added) == entity_count
    descriptors = sorted(
        (_entity_descriptor(e) for e in added),
        key=lambda d: d["unique_id"] or "",
    )
    assert descriptors == snapshot
