# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Constants for Notify Digest."""

DOMAIN = "notify_digest"

CONF_DIGESTS = "digests"
CONF_NAME = "name"
CONF_WINDOW_SECONDS = "window_seconds"
CONF_MAX_MESSAGES = "max_messages"
CONF_MAX_BUFFER_SECONDS = "max_buffer_seconds"
CONF_WINDOW_MODE = "window_mode"
CONF_SEPARATOR = "separator"
CONF_HEADER = "header"
CONF_TITLE_MODE = "title_mode"
CONF_DEDUPE = "dedupe"
CONF_TARGET_SERVICE = "target_service"
CONF_TARGET_SERVICE_DATA = "target_service_data"
CONF_MEDIA_POLICY = "media_policy"

MEDIA_POLICY_FLUSH_THEN_SEND = "flush_then_send"
MEDIA_POLICY_PASSTHROUGH = "passthrough"
MEDIA_POLICY_DROP = "drop"
DEFAULT_MEDIA_POLICY = MEDIA_POLICY_FLUSH_THEN_SEND

WINDOW_MODE_TUMBLING = "tumbling"
WINDOW_MODE_SLIDING = "sliding"
DEFAULT_WINDOW_MODE = WINDOW_MODE_TUMBLING

TITLE_MODE_FIRST = "first"
TITLE_MODE_LAST = "last"
TITLE_MODE_JOIN = "join"
DEFAULT_TITLE_MODE = TITLE_MODE_FIRST

DEFAULT_WINDOW_SECONDS = 30
DEFAULT_MAX_MESSAGES = 20
DEFAULT_SEPARATOR = "\n• "
DEFAULT_DEDUPE = False

SERVICE_FLUSH = "flush"
SERVICE_FLUSH_ALL = "flush_all"

ATTR_DIGEST_NAME = "digest"
