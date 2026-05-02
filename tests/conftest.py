# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Shared pytest fixtures.

The pytest-homeassistant-custom-component plugin provides the ``hass`` fixture
and other HA-specific helpers. We re-export ``enable_custom_integrations`` here
so individual test modules don't have to import it.
"""

from __future__ import annotations

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):  # type: ignore[no-untyped-def]
    """Make custom_components importable in every test."""
    yield
