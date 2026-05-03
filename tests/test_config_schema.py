# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Schema validation tests — exercised against CONFIG_SCHEMA without HA."""

from __future__ import annotations

import pytest
import voluptuous as vol

from custom_components.notify_digest import CONFIG_SCHEMA, DOMAIN


def _validate(raw):
    return CONFIG_SCHEMA({DOMAIN: raw})[DOMAIN]


class TestDigestSchema:
    def test_minimal_valid(self) -> None:
        cfg = _validate(
            {
                "digests": [
                    {"name": "door", "target_service": "notify.whatsapp_group"},
                ]
            }
        )
        digest = cfg["digests"][0]
        assert digest["name"] == "door"
        assert digest["target_service"] == "notify.whatsapp_group"
        # Defaults should be applied
        assert digest["window_seconds"] == 30
        assert digest["max_messages"] == 20
        assert digest["window_mode"] == "tumbling"
        assert digest["title_mode"] == "first"
        assert digest["dedupe"] is False

    def test_target_service_must_be_dotted(self) -> None:
        with pytest.raises(vol.Invalid):
            _validate({"digests": [{"name": "x", "target_service": "notwhitelisted"}]})

    def test_duplicate_names_rejected(self) -> None:
        with pytest.raises(vol.Invalid):
            _validate(
                {
                    "digests": [
                        {"name": "dup", "target_service": "notify.a"},
                        {"name": "dup", "target_service": "notify.b"},
                    ]
                }
            )

    def test_window_seconds_range_enforced(self) -> None:
        with pytest.raises(vol.Invalid):
            _validate(
                {
                    "digests": [
                        {
                            "name": "x",
                            "target_service": "notify.a",
                            "window_seconds": 0,
                        }
                    ]
                }
            )
        with pytest.raises(vol.Invalid):
            _validate(
                {
                    "digests": [
                        {
                            "name": "x",
                            "target_service": "notify.a",
                            "window_seconds": 99999,
                        }
                    ]
                }
            )

    def test_window_mode_choices(self) -> None:
        with pytest.raises(vol.Invalid):
            _validate(
                {
                    "digests": [
                        {
                            "name": "x",
                            "target_service": "notify.a",
                            "window_mode": "rolling",
                        }
                    ]
                }
            )

    def test_max_messages_floor(self) -> None:
        """A max_messages of 1 makes the digest a no-op — the floor is 2."""
        with pytest.raises(vol.Invalid):
            _validate(
                {
                    "digests": [
                        {
                            "name": "x",
                            "target_service": "notify.a",
                            "max_messages": 1,
                        }
                    ]
                }
            )

    def test_full_options_pass_through(self) -> None:
        cfg = _validate(
            {
                "digests": [
                    {
                        "name": "door_whatsapp",
                        "target_service": "script.whatsapp_send",
                        "target_service_data": {"target": "120@g.us"},
                        "window_seconds": 45,
                        "max_messages": 5,
                        "max_buffer_seconds": 120,
                        "window_mode": "sliding",
                        "separator": " | ",
                        "header": "Updates:",
                        "title_mode": "join",
                        "title_separator": " • ",
                        "dedupe": True,
                    }
                ]
            }
        )
        digest = cfg["digests"][0]
        assert digest["target_service_data"] == {"target": "120@g.us"}
        assert digest["window_mode"] == "sliding"
        assert digest["title_mode"] == "join"
        assert digest["title_separator"] == " • "
        assert digest["dedupe"] is True
        assert digest["max_buffer_seconds"] == 120
