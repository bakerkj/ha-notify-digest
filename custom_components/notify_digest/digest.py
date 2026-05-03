# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Digest buffer: collect messages, flush via downstream service after a window."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from logging import Logger
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .const import (
    TITLE_MODE_FIRST,
    TITLE_MODE_LAST,
    WINDOW_MODE_SLIDING,
)


@dataclass(frozen=True)
class DigestConfig:
    """Static configuration for one digest pipeline.

    ``target_service_data`` is typed as a read-only Mapping; setup wraps the
    incoming dict in MappingProxyType so the frozen-dataclass contract holds
    for the embedded payload too.
    """

    name: str
    target_service: str
    target_service_data: Mapping[str, Any]
    window_seconds: float
    max_messages: int
    max_buffer_seconds: float | None
    window_mode: str
    separator: str
    header: str
    title_mode: str
    title_separator: str
    dedupe: bool


@dataclass
class _Pending:
    titles: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


class DigestBuffer:
    """Coalesce messages within a time window, then dispatch to a downstream service.

    The first add() opens a flush window. Behaviour after subsequent adds depends
    on ``window_mode``:
      - tumbling: timer is fixed at first add; flush fires once the window expires
        regardless of further activity.
      - sliding: each new add resets the timer, so a continuous burst stays buffered
        until a quiet gap of ``window_seconds`` elapses.

    A flush is forced early when ``max_messages`` is reached, and (if configured)
    after ``max_buffer_seconds`` from the first add — that prevents a pathological
    sliding window from buffering forever.
    """

    def __init__(
        self, hass: HomeAssistant, config: DigestConfig, logger: Logger
    ) -> None:
        self._hass = hass
        self._config = config
        self._logger = logger
        self._pending = _Pending()
        self._flush_lock = asyncio.Lock()
        self._cancel_window: CALLBACK_TYPE | None = None
        self._cancel_max: CALLBACK_TYPE | None = None
        # Diagnostic arm-counters (read-only via properties). Each time the
        # underlying timer is scheduled, the counter increments — so tests can
        # tell "still armed" from "cancelled and re-armed" without reaching
        # into private state.
        self._window_arms = 0
        self._max_buffer_arms = 0

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def pending_count(self) -> int:
        return len(self._pending.messages)

    @property
    def is_window_armed(self) -> bool:
        return self._cancel_window is not None

    @property
    def is_max_buffer_armed(self) -> bool:
        return self._cancel_max is not None

    @property
    def window_arms(self) -> int:
        """Total times the window timer has been scheduled (re-arms increment)."""
        return self._window_arms

    @property
    def max_buffer_arms(self) -> int:
        """Total times the max-buffer timer has been scheduled."""
        return self._max_buffer_arms

    async def async_add(self, message: str, title: str | None = None) -> None:
        """Queue a message for the next flush. Empty/whitespace messages are ignored.

        When the buffer hits ``max_messages``, the flush is dispatched as a
        background task rather than awaited — so the caller's automation is
        never blocked on a slow downstream. Errors in the dispatched flush are
        already logged inside ``async_flush``; we swallow them here to avoid
        an unhandled-task warning.
        """
        text = message.strip()
        if not text:
            return

        if self._config.dedupe and text in self._pending.messages:
            self._logger.debug("dedupe: dropping duplicate message")
            return

        if title:
            self._pending.titles.append(title)
        self._pending.messages.append(text)

        if len(self._pending.messages) >= self._config.max_messages:
            self._logger.debug(
                "max_messages reached (%d) — scheduling flush",
                self._config.max_messages,
            )
            self._hass.async_create_task(
                self._flush_swallow_errors(reason="max_messages"),
                name=f"notify_digest_{self._config.name}_max_flush",
            )
            return

        self._arm_window_timer()
        self._arm_max_buffer_timer()

    async def _flush_swallow_errors(self, reason: str) -> None:
        """Wrapper for fire-and-forget flushes. Errors are already logged
        inside async_flush; suppressed here to avoid unhandled-task noise."""
        try:
            await self.async_flush(reason=reason)
        except Exception:
            pass

    async def async_flush(self, reason: str = "manual") -> None:
        """Drain the buffer and dispatch a single coalesced message downstream.

        The lock is held for the entire flush, including the downstream call.
        That serializes concurrent flush triggers — without it, two flushes on a
        slow downstream could race and arrive out of order. The cost is that a
        slow downstream backs up subsequent flushes, but ordering is preserved.

        On downstream failure, the buffered message bodies are logged before the
        exception propagates, so the content is recoverable from logs even
        though the buffer was already drained at this point.
        """
        async with self._flush_lock:
            self._cancel_timers()
            if not self._pending.messages:
                return
            pending = self._pending
            self._pending = _Pending()

            title = self._render_title(pending.titles)
            message = self._render_message(pending.messages)

            domain, service = self._config.target_service.split(".", 1)
            service_data: dict[str, Any] = dict(self._config.target_service_data)
            service_data["message"] = message
            if title:
                service_data["title"] = title

            self._logger.debug(
                "flush (%s): %d message(s) → %s.%s",
                reason,
                len(pending.messages),
                domain,
                service,
            )
            try:
                await self._hass.services.async_call(
                    domain, service, service_data, blocking=True
                )
            except Exception:
                self._logger.exception(
                    "flush (%s): downstream %s.%s failed; "
                    "%d message(s) lost from digest %r: %r",
                    reason,
                    domain,
                    service,
                    len(pending.messages),
                    self._config.name,
                    pending.messages,
                )
                raise

    @callback
    def _arm_window_timer(self) -> None:
        if self._config.window_mode == WINDOW_MODE_SLIDING:
            self._cancel_window_timer()
        elif self._cancel_window is not None:
            return
        self._cancel_window = async_call_later(
            self._hass, self._config.window_seconds, self._on_window_elapsed
        )
        self._window_arms += 1

    @callback
    def _arm_max_buffer_timer(self) -> None:
        if self._config.max_buffer_seconds is None or self._cancel_max is not None:
            return
        self._cancel_max = async_call_later(
            self._hass, self._config.max_buffer_seconds, self._on_max_elapsed
        )
        self._max_buffer_arms += 1

    @callback
    def _cancel_window_timer(self) -> None:
        if self._cancel_window is not None:
            self._cancel_window()
            self._cancel_window = None

    @callback
    def _cancel_max_timer(self) -> None:
        if self._cancel_max is not None:
            self._cancel_max()
            self._cancel_max = None

    @callback
    def _cancel_timers(self) -> None:
        self._cancel_window_timer()
        self._cancel_max_timer()

    async def _on_window_elapsed(self, _now: Any) -> None:
        self._cancel_window = None
        await self.async_flush(reason="window")

    async def _on_max_elapsed(self, _now: Any) -> None:
        self._cancel_max = None
        await self.async_flush(reason="max_buffer")

    def _render_title(self, titles: list[str]) -> str:
        if not titles:
            return ""
        mode = self._config.title_mode
        if mode == TITLE_MODE_FIRST:
            return titles[0]
        if mode == TITLE_MODE_LAST:
            return titles[-1]
        # TITLE_MODE_JOIN: order-preserving dedupe, joined by the configured separator.
        seen: set[str] = set()
        ordered: list[str] = []
        for t in titles:
            if t not in seen:
                seen.add(t)
                ordered.append(t)
        return self._config.title_separator.join(ordered)

    def _render_message(self, messages: list[str]) -> str:
        body = self._config.separator.join(messages)
        if self._config.header:
            return f"{self._config.header}{self._config.separator}{body}"
        return body
