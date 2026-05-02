# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Notify-entity end-to-end tests: notify.send_message → DigestBuffer."""

from __future__ import annotations

import pytest
from homeassistant.setup import async_setup_component

from custom_components.notify_digest.const import DOMAIN


@pytest.fixture
def downstream_calls(hass):
    captured: list[dict] = []

    async def _handler(call) -> None:
        captured.append(dict(call.data))

    hass.services.async_register("notify", "sink", _handler)
    return captured


async def _setup(hass, downstream_calls) -> None:
    cfg = {
        DOMAIN: {
            "digests": [
                {
                    "name": "door_whatsapp",
                    "target_service": "notify.sink",
                    "window_seconds": 60,
                }
            ]
        }
    }
    assert await async_setup_component(hass, "notify", {})
    assert await async_setup_component(hass, DOMAIN, cfg)
    await hass.async_block_till_done()


async def test_entity_registered_under_digest_name(hass, downstream_calls) -> None:
    await _setup(hass, downstream_calls)
    state = hass.states.get("notify.door_whatsapp")
    assert state is not None, (
        "expected notify.door_whatsapp; have: "
        f"{[s.entity_id for s in hass.states.async_all('notify')]}"
    )


async def test_send_message_routes_to_buffer(hass, downstream_calls) -> None:
    await _setup(hass, downstream_calls)
    await hass.services.async_call(
        "notify",
        "send_message",
        {"entity_id": "notify.door_whatsapp", "message": "hello"},
        blocking=True,
    )
    await hass.async_block_till_done()

    buf = hass.data[DOMAIN]["door_whatsapp"]
    assert buf.pending_count == 1
    # Force-flush so we can verify the downstream call shape.
    await buf.async_flush()
    assert len(downstream_calls) == 1
    assert downstream_calls[0]["message"] == "hello"


async def test_send_message_with_title(hass, downstream_calls) -> None:
    await _setup(hass, downstream_calls)
    await hass.services.async_call(
        "notify",
        "send_message",
        {
            "entity_id": "notify.door_whatsapp",
            "message": "ding",
            "title": "Front Door",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    buf = hass.data[DOMAIN]["door_whatsapp"]
    await buf.async_flush()
    assert downstream_calls[0]["title"] == "Front Door"
    assert downstream_calls[0]["message"] == "ding"


async def test_multiple_sends_coalesce(hass, downstream_calls) -> None:
    await _setup(hass, downstream_calls)
    for msg in ("one", "two", "three"):
        await hass.services.async_call(
            "notify",
            "send_message",
            {"entity_id": "notify.door_whatsapp", "message": msg},
            blocking=True,
        )
    await hass.async_block_till_done()
    assert downstream_calls == []  # still buffered

    buf = hass.data[DOMAIN]["door_whatsapp"]
    await buf.async_flush()
    assert len(downstream_calls) == 1
    assert "one" in downstream_calls[0]["message"]
    assert "two" in downstream_calls[0]["message"]
    assert "three" in downstream_calls[0]["message"]
