# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Notify Digest — coalesce notifications within a time window."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.notify import DATA_COMPONENT as NOTIFY_DATA_COMPONENT
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_DIGEST_NAME,
    CONF_DEDUPE,
    CONF_DIGESTS,
    CONF_HEADER,
    CONF_MAX_BUFFER_SECONDS,
    CONF_MAX_MESSAGES,
    CONF_MEDIA_POLICY,
    CONF_NAME,
    CONF_SEPARATOR,
    CONF_TARGET_SERVICE,
    CONF_TARGET_SERVICE_DATA,
    CONF_TITLE_MODE,
    CONF_WINDOW_MODE,
    CONF_WINDOW_SECONDS,
    DEFAULT_DEDUPE,
    DEFAULT_MAX_MESSAGES,
    DEFAULT_MEDIA_POLICY,
    DEFAULT_SEPARATOR,
    DEFAULT_TITLE_MODE,
    DEFAULT_WINDOW_MODE,
    DEFAULT_WINDOW_SECONDS,
    DOMAIN,
    MEDIA_POLICY_DROP,
    MEDIA_POLICY_FLUSH_THEN_SEND,
    MEDIA_POLICY_PASSTHROUGH,
    SERVICE_FLUSH,
    SERVICE_FLUSH_ALL,
    TITLE_MODE_FIRST,
    TITLE_MODE_JOIN,
    TITLE_MODE_LAST,
    WINDOW_MODE_SLIDING,
    WINDOW_MODE_TUMBLING,
)
from .digest import DigestBuffer, DigestConfig
from .notify import DigestNotifyEntity

_LOGGER = logging.getLogger(__name__)

# Per-digest timeout when flushing on HA shutdown. Without this, a hung
# downstream service would stall HA's stop sequence indefinitely.
SHUTDOWN_FLUSH_TIMEOUT = 10.0


def _service_id(value: str) -> str:
    """Validate that a service id looks like ``domain.service``."""
    if not isinstance(value, str) or value.count(".") != 1:
        raise vol.Invalid(f"target_service must be 'domain.service', got: {value!r}")
    domain, service = value.split(".", 1)
    if not domain or not service:
        raise vol.Invalid(f"target_service must be 'domain.service', got: {value!r}")
    return value


DIGEST_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.slug,
        vol.Required(CONF_TARGET_SERVICE): _service_id,
        vol.Optional(CONF_TARGET_SERVICE_DATA, default=dict): {cv.string: object},
        vol.Optional(CONF_WINDOW_SECONDS, default=DEFAULT_WINDOW_SECONDS): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=3600)
        ),
        vol.Optional(CONF_MAX_MESSAGES, default=DEFAULT_MAX_MESSAGES): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=1000)
        ),
        vol.Optional(CONF_MAX_BUFFER_SECONDS): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=86400)
        ),
        vol.Optional(CONF_WINDOW_MODE, default=DEFAULT_WINDOW_MODE): vol.In(
            [WINDOW_MODE_TUMBLING, WINDOW_MODE_SLIDING]
        ),
        vol.Optional(CONF_SEPARATOR, default=DEFAULT_SEPARATOR): cv.string,
        vol.Optional(CONF_HEADER, default=""): cv.string,
        vol.Optional(CONF_TITLE_MODE, default=DEFAULT_TITLE_MODE): vol.In(
            [TITLE_MODE_FIRST, TITLE_MODE_LAST, TITLE_MODE_JOIN]
        ),
        vol.Optional(CONF_DEDUPE, default=DEFAULT_DEDUPE): cv.boolean,
        vol.Optional(CONF_MEDIA_POLICY, default=DEFAULT_MEDIA_POLICY): vol.In(
            [
                MEDIA_POLICY_FLUSH_THEN_SEND,
                MEDIA_POLICY_PASSTHROUGH,
                MEDIA_POLICY_DROP,
            ]
        ),
    }
)


def _unique_digest_names(value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    for entry in value:
        name = entry[CONF_NAME]
        if name in seen:
            raise vol.Invalid(f"Duplicate digest name: {name!r}")
        seen.add(name)
    return value


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_DIGESTS): vol.All(
                    cv.ensure_list, [DIGEST_SCHEMA], _unique_digest_names
                ),
            }
        ),
    },
    extra=vol.ALLOW_EXTRA,
)


FLUSH_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DIGEST_NAME): cv.slug,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Notify Digest from configuration.yaml."""
    domain_cfg = config.get(DOMAIN)
    if not domain_cfg:
        return True

    buffers: dict[str, DigestBuffer] = {}
    for raw in domain_cfg[CONF_DIGESTS]:
        cfg = DigestConfig(
            name=raw[CONF_NAME],
            target_service=raw[CONF_TARGET_SERVICE],
            target_service_data=dict(raw.get(CONF_TARGET_SERVICE_DATA) or {}),
            window_seconds=float(raw[CONF_WINDOW_SECONDS]),
            max_messages=int(raw[CONF_MAX_MESSAGES]),
            max_buffer_seconds=(
                float(raw[CONF_MAX_BUFFER_SECONDS])
                if raw.get(CONF_MAX_BUFFER_SECONDS) is not None
                else None
            ),
            window_mode=raw[CONF_WINDOW_MODE],
            separator=raw[CONF_SEPARATOR],
            header=raw[CONF_HEADER],
            title_mode=raw[CONF_TITLE_MODE],
            dedupe=bool(raw[CONF_DEDUPE]),
            media_policy=raw[CONF_MEDIA_POLICY],
        )
        buffers[cfg.name] = DigestBuffer(hass, cfg, _LOGGER.getChild(cfg.name))

    hass.data[DOMAIN] = buffers

    async def _flush(call: ServiceCall) -> None:
        name = call.data[ATTR_DIGEST_NAME]
        buf = buffers.get(name)
        if buf is None:
            _LOGGER.warning("flush: unknown digest %r", name)
            return
        await buf.async_flush(reason="service")

    async def _flush_all(_call: ServiceCall) -> None:
        for buf in buffers.values():
            await buf.async_flush(reason="service_all")

    hass.services.async_register(
        DOMAIN, SERVICE_FLUSH, _flush, schema=FLUSH_SERVICE_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_FLUSH_ALL, _flush_all)

    # Register one NotifyEntity per digest under the notify integration's
    # EntityComponent. Doing this directly (rather than via async_load_platform)
    # is what gets us the modern entity path — discovery routes through the
    # legacy platform setup, which expects async_get_service.
    notify_component = hass.data[NOTIFY_DATA_COMPONENT]
    await notify_component.async_add_entities(
        DigestNotifyEntity(buf) for buf in buffers.values()
    )

    async def _shutdown(_event: Any) -> None:
        for buf in buffers.values():
            try:
                await asyncio.wait_for(
                    buf.async_flush(reason="shutdown"),
                    timeout=SHUTDOWN_FLUSH_TIMEOUT,
                )
            except TimeoutError:
                _LOGGER.warning(
                    "shutdown: digest %r flush exceeded %.1fs timeout",
                    buf.name,
                    SHUTDOWN_FLUSH_TIMEOUT,
                )
            except Exception:
                _LOGGER.exception("shutdown: digest %r flush failed", buf.name)

    hass.bus.async_listen_once("homeassistant_stop", _shutdown)

    _LOGGER.info(
        "Notify Digest set up with %d digest(s): %s",
        len(buffers),
        ", ".join(sorted(buffers)),
    )
    return True
