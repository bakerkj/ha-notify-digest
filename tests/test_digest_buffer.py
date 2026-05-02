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
        dedupe=False,
        media_policy="flush_then_send",
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
    over many seconds. Instead we assert the cancel-then-rearm behaviour
    directly via the buffer's internal timer handle.
    """
    buf = DigestBuffer(
        hass,
        _config(window_mode="sliding", window_seconds=30.0),
        logging.getLogger("t"),
    )
    await buf.async_add("a")
    first_handle = buf._cancel_window  # noqa: SLF001
    assert first_handle is not None

    await buf.async_add("b")
    second_handle = buf._cancel_window  # noqa: SLF001
    assert second_handle is not None
    # Sliding mode: the prior timer was cancelled and replaced.
    assert first_handle is not second_handle

    await buf.async_flush()  # release armed timers so the test harness is satisfied


async def test_max_buffer_arms_independent_timer(hass, calls) -> None:
    """max_buffer_seconds arms its own timer that survives sliding-mode resets."""
    buf = DigestBuffer(
        hass,
        _config(window_mode="sliding", window_seconds=10.0, max_buffer_seconds=60.0),
        logging.getLogger("t"),
    )
    await buf.async_add("a")
    max_after_first = buf._cancel_max  # noqa: SLF001
    assert max_after_first is not None

    await buf.async_add("b")
    # Sliding mode should re-arm the window timer but leave max alone.
    assert buf._cancel_max is max_after_first  # noqa: SLF001

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


# --- media handling ----------------------------------------------------------


async def test_media_flush_then_send_drains_pending_text_first(hass, calls) -> None:
    """Default policy: pending text gets flushed before the media call goes out."""
    buf = DigestBuffer(
        hass, _config(media_policy="flush_then_send"), logging.getLogger("t")
    )
    await buf.async_add("queued text 1")
    await buf.async_add("queued text 2")
    assert calls == []

    await buf.async_add(
        "video caption", title="Front Door", data={"video_url": "http://x/clip.mp4"}
    )
    await hass.async_block_till_done()

    assert len(calls) == 2
    assert calls[0]["data"]["message"] == "queued text 1 | queued text 2"
    assert "video_url" not in calls[0]["data"]
    assert calls[1]["data"]["video_url"] == "http://x/clip.mp4"
    assert calls[1]["data"]["message"] == "video caption"
    assert calls[1]["data"]["title"] == "Front Door"


async def test_media_passthrough_does_not_flush(hass, calls) -> None:
    """Passthrough policy: media goes out immediately, text remains buffered."""
    buf = DigestBuffer(
        hass, _config(media_policy="passthrough"), logging.getLogger("t")
    )
    await buf.async_add("queued text")
    await buf.async_add("media", data={"image": "http://x/snap.jpg"})
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert calls[0]["data"]["image"] == "http://x/snap.jpg"
    assert buf.pending_count == 1  # text still buffered

    await buf.async_flush()
    assert len(calls) == 2
    assert calls[1]["data"]["message"] == "queued text"


async def test_media_drop_discards_silently(hass, calls) -> None:
    buf = DigestBuffer(hass, _config(media_policy="drop"), logging.getLogger("t"))
    await buf.async_add("queued text")
    await buf.async_add("media", data={"video_url": "http://x/clip.mp4"})
    await hass.async_block_till_done()

    assert calls == []  # media dropped, text still buffered
    assert buf.pending_count == 1
    await buf.async_flush()
    assert len(calls) == 1
    assert calls[0]["data"]["message"] == "queued text"


async def test_media_merges_target_service_data(hass, calls) -> None:
    """Static target_service_data + per-call data should both reach the downstream."""
    buf = DigestBuffer(
        hass,
        _config(target_service_data={"target": "120@g.us"}),
        logging.getLogger("t"),
    )
    await buf.async_add("", data={"video_url": "http://x/clip.mp4"})
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert calls[0]["data"]["target"] == "120@g.us"
    assert calls[0]["data"]["video_url"] == "http://x/clip.mp4"
    # Empty message should not get added as a message field.
    assert "message" not in calls[0]["data"]


async def test_empty_data_dict_is_text_path(hass, calls) -> None:
    """An empty ``data`` dict should NOT count as media — fall through to buffering."""
    buf = DigestBuffer(hass, _config(), logging.getLogger("t"))
    await buf.async_add("hi", data={})
    assert calls == []
    assert buf.pending_count == 1
    await buf.async_flush()
    assert calls[0]["data"]["message"] == "hi"
