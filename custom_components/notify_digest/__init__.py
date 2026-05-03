# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Notify Digest — coalesce notifications within a time window."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.notify import DATA_COMPONENT as NOTIFY_DATA_COMPONENT
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.reload import async_integration_yaml_config
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_DIGEST_NAME,
    CONF_DEDUPE,
    CONF_DIGESTS,
    CONF_HEADER,
    CONF_MAX_BUFFER_SECONDS,
    CONF_MAX_MESSAGES,
    CONF_NAME,
    CONF_SEPARATOR,
    CONF_TARGET_SERVICE,
    CONF_TARGET_SERVICE_DATA,
    CONF_TITLE_MODE,
    CONF_TITLE_SEPARATOR,
    CONF_WINDOW_MODE,
    CONF_WINDOW_SECONDS,
    DEFAULT_DEDUPE,
    DEFAULT_MAX_MESSAGES,
    DEFAULT_SEPARATOR,
    DEFAULT_TITLE_MODE,
    DEFAULT_TITLE_SEPARATOR,
    DEFAULT_WINDOW_MODE,
    DEFAULT_WINDOW_SECONDS,
    DOMAIN,
    SERVICE_FLUSH,
    SERVICE_FLUSH_ALL,
    SERVICE_RELOAD,
    TITLE_MODE_FIRST,
    TITLE_MODE_JOIN,
    TITLE_MODE_LAST,
    WINDOW_MODE_SLIDING,
    WINDOW_MODE_TUMBLING,
)
from .digest import DigestBuffer, DigestConfig
from .notify import DigestNotifyEntity

_LOGGER = logging.getLogger(__name__)

# Per-digest timeout for in-flight flushes during shutdown or reload. Without
# this, a hung downstream service would stall HA's stop sequence (or block a
# reload) indefinitely.
SHUTDOWN_FLUSH_TIMEOUT = 10.0

# hass.data key for the list of currently-registered DigestNotifyEntity
# instances. Reload uses it to remove old entities before installing new ones.
DATA_ENTITIES = f"{DOMAIN}_entities"


def _service_id(value: str) -> str:
    """Validate that a service id looks like ``domain.service``."""
    if not isinstance(value, str) or value.count(".") != 1:
        raise vol.Invalid(f"target_service must be 'domain.service', got: {value!r}")
    domain, service = value.split(".", 1)
    if not domain or not service:
        raise vol.Invalid(f"target_service must be 'domain.service', got: {value!r}")
    return value


def _reject_max_buffer_in_tumbling(value: dict[str, Any]) -> dict[str, Any]:
    """``max_buffer_seconds`` only makes sense in sliding mode.

    In tumbling mode the window timer arms once at first add and never resets,
    so a separate "max age" ceiling is either redundant (>= window_seconds) or
    just a shorter window (< window_seconds). Reject the combination so the
    misconfiguration surfaces at setup, not silently as confusing flushes.
    """
    if (
        value.get(CONF_WINDOW_MODE) == WINDOW_MODE_TUMBLING
        and value.get(CONF_MAX_BUFFER_SECONDS) is not None
    ):
        raise vol.Invalid(
            "max_buffer_seconds is only meaningful with window_mode: sliding"
        )
    return value


DIGEST_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_NAME): cv.slug,
            vol.Required(CONF_TARGET_SERVICE): _service_id,
            vol.Optional(CONF_TARGET_SERVICE_DATA, default=dict): {cv.string: object},
            vol.Optional(CONF_WINDOW_SECONDS, default=DEFAULT_WINDOW_SECONDS): vol.All(
                vol.Coerce(float), vol.Range(min=1, max=3600)
            ),
            vol.Optional(CONF_MAX_MESSAGES, default=DEFAULT_MAX_MESSAGES): vol.All(
                vol.Coerce(int), vol.Range(min=2, max=1000)
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
            vol.Optional(
                CONF_TITLE_SEPARATOR, default=DEFAULT_TITLE_SEPARATOR
            ): cv.string,
            vol.Optional(CONF_DEDUPE, default=DEFAULT_DEDUPE): cv.boolean,
        }
    ),
    _reject_max_buffer_in_tumbling,
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
    if domain_cfg:
        await _async_install_digests(hass, domain_cfg)
    else:
        # Still install empty state and the reload service so a user who adds
        # config later can pick it up via reload without restarting HA.
        hass.data[DOMAIN] = {}
        hass.data[DATA_ENTITIES] = []

    async def _flush(call: ServiceCall) -> None:
        name = call.data[ATTR_DIGEST_NAME]
        buf = hass.data.get(DOMAIN, {}).get(name)
        if buf is None:
            _LOGGER.warning("flush: unknown digest %r", name)
            return
        await buf.async_flush(reason="service")

    async def _flush_all(_call: ServiceCall) -> None:
        for buf in hass.data.get(DOMAIN, {}).values():
            await buf.async_flush(reason="service_all")

    async def _reload(_call: ServiceCall) -> None:
        """Reload digest configuration from configuration.yaml.

        Drains pending messages on existing buffers (best-effort, with the
        same per-digest timeout used at shutdown), removes their entities,
        re-reads the YAML, and installs the new set.
        """
        try:
            new_config = await async_integration_yaml_config(hass, DOMAIN)
        except HomeAssistantError:
            _LOGGER.exception("reload: failed to read configuration.yaml")
            return

        await _async_drain_and_remove(hass)

        new_domain_cfg = (new_config or {}).get(DOMAIN)
        if new_domain_cfg:
            await _async_install_digests(hass, new_domain_cfg)
        else:
            hass.data[DOMAIN] = {}
            hass.data[DATA_ENTITIES] = []

        _LOGGER.info("Notify Digest reloaded: %d digest(s)", len(hass.data[DOMAIN]))

    hass.services.async_register(
        DOMAIN, SERVICE_FLUSH, _flush, schema=FLUSH_SERVICE_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_FLUSH_ALL, _flush_all)
    async_register_admin_service(hass, DOMAIN, SERVICE_RELOAD, _reload)

    async def _shutdown(_event: Any) -> None:
        for buf in hass.data.get(DOMAIN, {}).values():
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

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown)

    return True


async def _async_install_digests(
    hass: HomeAssistant, domain_cfg: dict[str, Any]
) -> None:
    """Build buffers + entities from a validated domain config block, register
    them, and store handles in hass.data for later reload."""
    buffers: dict[str, DigestBuffer] = {}
    entities: list[DigestNotifyEntity] = []
    for raw in domain_cfg[CONF_DIGESTS]:
        cfg = DigestConfig.from_raw(raw)
        buf = DigestBuffer(hass, cfg, _LOGGER.getChild(cfg.name))
        buffers[cfg.name] = buf
        entities.append(DigestNotifyEntity(buf))

    hass.data[DOMAIN] = buffers
    hass.data[DATA_ENTITIES] = entities

    if entities:
        notify_component = hass.data[NOTIFY_DATA_COMPONENT]
        await notify_component.async_add_entities(entities)

    _LOGGER.info(
        "Notify Digest installed %d digest(s): %s",
        len(buffers),
        ", ".join(sorted(buffers)),
    )


async def _async_drain_and_remove(hass: HomeAssistant) -> None:
    """Drain pending messages on existing buffers (best-effort) and remove
    their entities. Used by reload before installing the new set."""
    for buf in hass.data.get(DOMAIN, {}).values():
        try:
            await asyncio.wait_for(
                buf.async_flush(reason="reload"),
                timeout=SHUTDOWN_FLUSH_TIMEOUT,
            )
        except TimeoutError:
            _LOGGER.warning(
                "reload: digest %r flush exceeded %.1fs timeout",
                buf.name,
                SHUTDOWN_FLUSH_TIMEOUT,
            )
        except Exception:
            _LOGGER.exception("reload: digest %r flush failed", buf.name)

    # force_remove drops the registry entry too; without it, the entity_id
    # lingers as "unavailable, restored" after reload — confusing for users
    # who renamed or removed a digest.
    for entity in hass.data.get(DATA_ENTITIES, []):
        await entity.async_remove(force_remove=True)
