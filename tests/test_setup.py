# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""End-to-end setup test: load the integration via async_setup_component."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.setup import async_setup_component

import custom_components.notify_digest as integration
from custom_components.notify_digest import CONFIG_SCHEMA
from custom_components.notify_digest.const import (
    DOMAIN,
    SERVICE_FLUSH,
    SERVICE_FLUSH_ALL,
    SERVICE_RELOAD,
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


async def test_flush_all_dispatches_every_buffer(hass) -> None:
    """flush_all should drain every configured digest, not just one."""
    sink_a: list[dict] = []
    sink_b: list[dict] = []

    async def _sink_a(call) -> None:
        sink_a.append(dict(call.data))

    async def _sink_b(call) -> None:
        sink_b.append(dict(call.data))

    hass.services.async_register("notify", "sink_a", _sink_a)
    hass.services.async_register("notify", "sink_b", _sink_b)

    cfg = {
        DOMAIN: {
            "digests": [
                {
                    "name": "a",
                    "target_service": "notify.sink_a",
                    "window_seconds": 60,
                },
                {
                    "name": "b",
                    "target_service": "notify.sink_b",
                    "window_seconds": 60,
                },
            ]
        }
    }
    assert await async_setup_component(hass, DOMAIN, cfg)
    await hass.async_block_till_done()

    await hass.data[DOMAIN]["a"].async_add("hello A")
    await hass.data[DOMAIN]["b"].async_add("hello B")
    assert sink_a == []
    assert sink_b == []

    await hass.services.async_call(DOMAIN, SERVICE_FLUSH_ALL, {}, blocking=True)
    await hass.async_block_till_done()

    assert len(sink_a) == 1
    assert sink_a[0]["message"] == "hello A"
    assert len(sink_b) == 1
    assert sink_b[0]["message"] == "hello B"


async def test_shutdown_event_flushes_pending(hass, downstream_calls) -> None:
    """The homeassistant_stop listener flushes pending buffers (happy path)."""
    cfg = {
        DOMAIN: {
            "digests": [
                {
                    "name": "x",
                    "target_service": "notify.sink",
                    "window_seconds": 60,
                }
            ]
        }
    }
    assert await async_setup_component(hass, DOMAIN, cfg)
    await hass.async_block_till_done()

    await hass.data[DOMAIN]["x"].async_add("queued1")
    await hass.data[DOMAIN]["x"].async_add("queued2")
    assert downstream_calls == []  # window not elapsed

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert len(downstream_calls) == 1
    assert "queued1" in downstream_calls[0]["message"]
    assert "queued2" in downstream_calls[0]["message"]


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
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        # Headroom over our 0.1s timeout. If wait_for is missing, we'd block here.
        await asyncio.sleep(0.3)

    assert any(
        "exceeded" in r.message and "timeout" in r.message for r in caplog.records
    ), f"expected timeout warning; got: {[r.message for r in caplog.records]}"

    # Release the hung service handler so teardown isn't dirty.
    cancel_event.set()


async def test_reload_swaps_in_new_digests(hass) -> None:
    """A reload tears down the existing digest set and installs the new YAML."""
    sink_a: list[dict] = []
    sink_b: list[dict] = []

    async def _a(call) -> None:
        sink_a.append(dict(call.data))

    async def _b(call) -> None:
        sink_b.append(dict(call.data))

    hass.services.async_register("notify", "sink_a", _a)
    hass.services.async_register("notify", "sink_b", _b)

    cfg1 = {
        DOMAIN: {
            "digests": [
                {
                    "name": "first",
                    "target_service": "notify.sink_a",
                    "window_seconds": 60,
                }
            ]
        }
    }
    assert await async_setup_component(hass, DOMAIN, cfg1)
    await hass.async_block_till_done()

    assert hass.states.get("notify.first") is not None

    cfg2_validated = CONFIG_SCHEMA(
        {
            DOMAIN: {
                "digests": [
                    {
                        "name": "second",
                        "target_service": "notify.sink_b",
                        "window_seconds": 60,
                    }
                ]
            }
        }
    )

    with patch(
        "custom_components.notify_digest.async_integration_yaml_config",
        new=AsyncMock(return_value=cfg2_validated),
    ):
        await hass.services.async_call(DOMAIN, SERVICE_RELOAD, {}, blocking=True)
        await hass.async_block_till_done()

    assert hass.states.get("notify.first") is None
    assert hass.states.get("notify.second") is not None
    assert "second" in hass.data[DOMAIN]
    assert "first" not in hass.data[DOMAIN]


async def test_reload_drains_pending_messages_first(hass, downstream_calls) -> None:
    """Reload flushes pending messages on existing digests before tearing down."""
    cfg1 = {
        DOMAIN: {
            "digests": [
                {
                    "name": "x",
                    "target_service": "notify.sink",
                    "window_seconds": 60,
                }
            ]
        }
    }
    assert await async_setup_component(hass, DOMAIN, cfg1)
    await hass.async_block_till_done()

    await hass.data[DOMAIN]["x"].async_add("pending 1")
    await hass.data[DOMAIN]["x"].async_add("pending 2")
    assert downstream_calls == []

    cfg2_validated = CONFIG_SCHEMA(
        {
            DOMAIN: {
                "digests": [
                    {
                        "name": "y",
                        "target_service": "notify.sink",
                        "window_seconds": 60,
                    }
                ]
            }
        }
    )

    with patch(
        "custom_components.notify_digest.async_integration_yaml_config",
        new=AsyncMock(return_value=cfg2_validated),
    ):
        await hass.services.async_call(DOMAIN, SERVICE_RELOAD, {}, blocking=True)
        await hass.async_block_till_done()

    assert len(downstream_calls) == 1
    assert "pending 1" in downstream_calls[0]["message"]
    assert "pending 2" in downstream_calls[0]["message"]
