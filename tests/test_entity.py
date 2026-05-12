"""Tests for the AshlyEntity base class."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.ashly.entity import AshlyEntity


def test_entity_construction_without_system_info_raises(mock_coordinator):
    """Constructing an AshlyEntity before _async_setup populated system_info must fail loudly."""
    mock_coordinator.system_info = None
    description = MagicMock()
    description.key = "test"
    with pytest.raises(RuntimeError, match="system_info not initialized"):
        AshlyEntity(mock_coordinator, description)
