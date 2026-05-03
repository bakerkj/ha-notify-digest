# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""End-to-end setup test: load the integration via async_setup_component."""

from __future__ import annotations

import asyncio
import logging

import pytest
from homeassistant.setup import async_setup_component

import custom_components.notify_digest as integration
from custom_components.notify_digest.const import (
    DOMAIN,
    SERVICE_FLUSH,
    SERVICE_FLUSH_ALL,
)


@pytest.fixture
def downstream_calls(hass):
    captured: list[dict] = []

    async def _handler(call) -> None:
        captured.append(dict(call.data))

    hass.services.async_register("notify", "sink", _handler)
    return captured


async def test_async_setup_registers_flush_services(hass, downstream_calls) -> None:
    cfg = {
        DOMAIN: {
            "digests": [
                {
                    "name": "door",
                    "target_service": "notify.sink",
                    "window_seconds": 1,
                }
            ]
        }
    }
    assert await async_setup_component(hass, DOMAIN, cfg)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, SERVICE_FLUSH)
    assert hass.services.has_service(DOMAIN, SERVICE_FLUSH_ALL)


async def test_flush_service_dispatches_buffered_messages(
    hass, downstream_calls
) -> None:
    cfg = {
        DOMAIN: {
            "digests": [
                {"name": "door", "target_service": "notify.sink", "window_seconds": 60}
            ]
        }
    }
    assert await async_setup_component(hass, DOMAIN, cfg)
    await hass.async_block_till_done()

    buffers = hass.data[DOMAIN]
    await buffers["door"].async_add("ping1")
    await buffers["door"].async_add("ping2")
    assert downstream_calls == []

    await hass.services.async_call(
        DOMAIN, SERVICE_FLUSH, {"digest": "door"}, blocking=True
    )
    await hass.async_block_till_done()
    assert len(downstream_calls) == 1
    assert "ping1" in downstream_calls[0]["message"]
    assert "ping2" in downstream_calls[0]["message"]


async def test_flush_unknown_digest_is_silent(hass, downstream_calls) -> None:
    cfg = {
        DOMAIN: {
            "digests": [
                {"name": "door", "target_service": "notify.sink"},
            ]
        }
    }
    assert await async_setup_component(hass, DOMAIN, cfg)
    await hass.async_block_till_done()

    # Should not raise — just logs a warning.
    await hass.services.async_call(
        DOMAIN, SERVICE_FLUSH, {"digest": "nope"}, blocking=True
    )
    await hass.async_block_till_done()
    assert downstream_calls == []


async def test_shutdown_flush_times_out_on_hang(hass, monkeypatch, caplog) -> None:
    """A downstream that hangs during shutdown must not stall HA stop —
    the per-digest wait_for caps it at SHUTDOWN_FLUSH_TIMEOUT."""
    monkeypatch.setattr(integration, "SHUTDOWN_FLUSH_TIMEOUT", 0.1)

    cancel_event = asyncio.Event()

    async def _hangs(_call) -> None:
        await cancel_event.wait()

    hass.services.async_register("notify", "sink", _hangs)

    cfg = {
        DOMAIN: {
            "digests": [{"name": "x", "target_service": "notify.sink"}],
        }
    }
    assert await async_setup_component(hass, DOMAIN, cfg)
    await hass.async_block_till_done()

    await hass.data[DOMAIN]["x"].async_add("queued")

    with caplog.at_level(logging.WARNING, logger=integration.__name__):
        hass.bus.async_fire("homeassistant_stop")
        # Headroom over our 0.1s timeout. If wait_for is missing, we'd block here.
        await asyncio.sleep(0.3)

    assert any(
        "exceeded" in r.message and "timeout" in r.message for r in caplog.records
    ), f"expected timeout warning; got: {[r.message for r in caplog.records]}"

    # Release the hung service handler so teardown isn't dirty.
    cancel_event.set()
