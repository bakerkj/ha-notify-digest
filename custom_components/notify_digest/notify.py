# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""NotifyEntity that fronts a DigestBuffer.

Each digest in configuration.yaml becomes a ``notify.<digest_name>`` entity.
Callers reach it via the standard ``notify.send_message`` action with
``entity_id``. The NotifyEntity API is text-only (no ``data:`` field), so
media can't enter the digest through this surface — by design. The
DigestBuffer's media-handling path remains as a defensive backstop for any
direct programmatic caller that does pass ``data``.

Entities are registered directly against the notify EntityComponent from
``__init__.py``; this module only defines the entity class.
"""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature

from .digest import DigestBuffer


class DigestNotifyEntity(NotifyEntity):
    """Exposes a DigestBuffer as ``notify.<digest_name>``."""

    _attr_should_poll = False
    _attr_supported_features = NotifyEntityFeature.TITLE

    def __init__(self, buffer: DigestBuffer) -> None:
        self._buffer = buffer
        self._attr_name = buffer.name
        self._attr_unique_id = f"notify_digest_{buffer.name}"

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        await self._buffer.async_add(message=message, title=title)
