# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""DigestBuffer behaviour tests."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.notify_digest.digest import DigestBuffer, DigestConfig
from homeassistant.util import dt as dt_util


def _config(**overrides: Any) -> DigestConfig:
    base: dict[str, Any] = dict(
        name="test",
        target_service="notify.sink",
        target_service_data={},
        window_seconds=10.0,
        max_messages=5,
        max_buffer_seconds=None,
        window_mode="tumbling",
        separator=" | ",
        header="",
        title_mode="first",
        title_separator=" / ",
        dedupe=False,
    )
    base.update(overrides)
    return DigestConfig(**base)


@pytest.fixture
def calls(hass):
    captured: list[dict] = []

    async def _handler(call) -> None:
        captured.append({"data": dict(call.data)})

    hass.services.async_register("notify", "sink", _handler)
    return captured


async def test_single_message_flushes_after_window(hass, calls) -> None:
    buf = DigestBuffer(hass, _config(window_seconds=2.0), logging.getLogger("t"))
    await buf.async_add("hello")
    assert calls == []

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert calls[0]["data"]["message"] == "hello"


async def test_messages_within_window_are_coalesced(hass, calls) -> None:
    buf = DigestBuffer(hass, _config(window_seconds=2.0), logging.getLogger("t"))
    await buf.async_add("first")
    await buf.async_add("second")
    await buf.async_add("third")

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert calls[0]["data"]["message"] == "first | second | third"


async def test_max_messages_forces_early_flush(hass, calls) -> None:
    buf = DigestBuffer(hass, _config(max_messages=3), logging.getLogger("t"))
    await buf.async_add("a")
    await buf.async_add("b")
    assert calls == []
    await buf.async_add("c")  # threshold
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0]["data"]["message"] == "a | b | c"


async def test_max_messages_does_not_block_caller_on_slow_downstream(hass) -> None:
    """async_add at max_messages dispatches the flush as a background task,
    so the caller's automation isn't held up by a slow downstream."""
    import asyncio as _asyncio

    started = _asyncio.Event()
    release = _asyncio.Event()

    async def _slow(_call: Any) -> None:
        started.set()
        await release.wait()

    hass.services.async_register("notify", "sink", _slow)

    buf = DigestBuffer(hass, _config(max_messages=2), logging.getLogger("t"))
    await buf.async_add("first")

    # Second add hits the threshold. With a fire-and-forget flush this returns
    # quickly even though the downstream service is hanging.
    await _asyncio.wait_for(buf.async_add("second"), timeout=0.5)

    # Confirm the slow handler actually got invoked (background task is running).
    await _asyncio.wait_for(started.wait(), timeout=0.5)

    # Cleanup — release the hung handler so test teardown is clean.
    release.set()
    await hass.async_block_till_done()


async def test_dedupe_drops_repeats(hass, calls) -> None:
    buf = DigestBuffer(hass, _config(dedupe=True), logging.getLogger("t"))
    await buf.async_add("same")
    await buf.async_add("same")
    await buf.async_add("other")
    await buf.async_flush()
    assert calls[0]["data"]["message"] == "same | other"


async def test_header_prepended_when_present(hass, calls) -> None:
    buf = DigestBuffer(
        hass,
        _config(header="Updates:", separator="\n- "),
        logging.getLogger("t"),
    )
    await buf.async_add("a")
    await buf.async_add("b")
    await buf.async_flush()
    assert calls[0]["data"]["message"] == "Updates:\n- a\n- b"


async def test_title_mode_first(hass, calls) -> None:
    buf = DigestBuffer(hass, _config(title_mode="first"), logging.getLogger("t"))
    await buf.async_add("a", title="T1")
    await buf.async_add("b", title="T2")
    await buf.async_flush()
    assert calls[0]["data"]["title"] == "T1"


async def test_title_mode_join_dedupes(hass, calls) -> None:
    buf = DigestBuffer(hass, _config(title_mode="join"), logging.getLogger("t"))
    await buf.async_add("a", title="T1")
    await buf.async_add("b", title="T2")
    await buf.async_add("c", title="T1")
    await buf.async_flush()
    assert calls[0]["data"]["title"] == "T1 / T2"


async def test_sliding_window_resets_timer(hass, calls) -> None:
    """In sliding mode, async_add should cancel the old timer and arm a new one.

    The HA test helper ``async_fire_time_changed`` fires timers within a
    relative window of *real* wall-clock time, so we can't simulate progression
    over many seconds. Instead we assert the cancel-then-rearm behaviour via
    the public arm-counter, which increments on every fresh schedule.
    """
    buf = DigestBuffer(
        hass,
        _config(window_mode="sliding", window_seconds=30.0),
        logging.getLogger("t"),
    )
    await buf.async_add("a")
    assert buf.is_window_armed
    assert buf.window_arms == 1

    await buf.async_add("b")
    assert buf.is_window_armed
    # Sliding mode cancelled and re-scheduled, so the counter advanced.
    assert buf.window_arms == 2

    await buf.async_flush()  # release armed timers so the test harness is satisfied


async def test_max_buffer_arms_independent_timer(hass, calls) -> None:
    """max_buffer_seconds arms its own timer that survives sliding-mode resets."""
    buf = DigestBuffer(
        hass,
        _config(window_mode="sliding", window_seconds=10.0, max_buffer_seconds=60.0),
        logging.getLogger("t"),
    )
    await buf.async_add("a")
    assert buf.is_max_buffer_armed
    assert buf.max_buffer_arms == 1

    await buf.async_add("b")
    # Sliding mode re-arms the window timer but leaves max alone.
    assert buf.max_buffer_arms == 1

    await buf.async_flush()


async def test_target_service_data_merged(hass, calls) -> None:
    buf = DigestBuffer(
        hass,
        _config(target_service_data={"target": "120@g.us"}),
        logging.getLogger("t"),
    )
    await buf.async_add("hi")
    await buf.async_flush()
    assert calls[0]["data"]["target"] == "120@g.us"
    assert calls[0]["data"]["message"] == "hi"


async def test_empty_message_ignored(hass, calls) -> None:
    buf = DigestBuffer(hass, _config(), logging.getLogger("t"))
    await buf.async_add("")
    await buf.async_add("   ")
    await buf.async_flush()
    assert calls == []


async def test_title_separator_configurable(hass, calls) -> None:
    """title_separator is honored when title_mode == join."""
    buf = DigestBuffer(
        hass,
        _config(title_mode="join", title_separator=" • "),
        logging.getLogger("t"),
    )
    await buf.async_add("a", title="T1")
    await buf.async_add("b", title="T2")
    await buf.async_flush()
    assert calls[0]["data"]["title"] == "T1 • T2"


async def test_messages_dispatched_in_arrival_order(hass, calls) -> None:
    """Contract: buffered messages are joined in the order async_add was called.

    No sorting, no priority — strictly FIFO. Pinned as an explicit test so
    future refactors that introduce sorting/grouping fail loudly here, not
    silently in production.
    """
    buf = DigestBuffer(hass, _config(), logging.getLogger("t"))
    for msg in ("alpha", "bravo", "charlie", "delta", "echo"):
        await buf.async_add(msg)
    await buf.async_flush()
    assert calls[0]["data"]["message"] == "alpha | bravo | charlie | delta | echo"


async def test_dedupe_preserves_first_occurrence_position(hass, calls) -> None:
    """Dedupe drops later duplicates; the first occurrence keeps its slot."""
    buf = DigestBuffer(hass, _config(dedupe=True), logging.getLogger("t"))
    await buf.async_add("alpha")
    await buf.async_add("bravo")
    await buf.async_add("alpha")  # dropped — already present
    await buf.async_add("charlie")
    await buf.async_add("bravo")  # dropped
    await buf.async_flush()
    assert calls[0]["data"]["message"] == "alpha | bravo | charlie"


async def test_downstream_failure_logs_messages_and_drains(hass, caplog) -> None:
    """A downstream that raises: the exception propagates, the buffer is drained,
    and the lost message bodies are logged so they're recoverable."""

    async def _failing(_call: Any) -> None:
        raise RuntimeError("downstream broken")

    hass.services.async_register("notify", "sink", _failing)

    test_logger = logging.getLogger("test_digest_failure")
    test_logger.propagate = True
    buf = DigestBuffer(hass, _config(), test_logger)
    await buf.async_add("important1")
    await buf.async_add("important2")

    with caplog.at_level(logging.ERROR, logger="test_digest_failure"):
        with pytest.raises(Exception):
            await buf.async_flush()

    assert "important1" in caplog.text
    assert "important2" in caplog.text
    assert buf.pending_count == 0
