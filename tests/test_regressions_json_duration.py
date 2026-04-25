import pytest
from datetime import datetime
from unittest.mock import patch

import spindle
from spindle import _extract_last_json_object, _parse_duration


def test_extract_last_json_object_ignores_braces_in_strings():
    # Last JSON object is valid; it contains a '}' inside a string value.
    text = (
        "noise\n"
        '{"ok": true, "msg": "this has a brace } in a string"}\n'
        "trailer"
    )
    obj = _extract_last_json_object(text)
    assert obj == {"ok": True, "msg": "this has a brace } in a string"}


def test_parse_duration_absolute_time_never_exceeds_24h():
    # Freeze time just after 06:00; waiting until 06:00 tomorrow is almost 24h.
    frozen = datetime(2026, 1, 1, 6, 0, 1)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    # spindle._parse_duration uses spindle.datetime (imported from datetime)
    with patch.object(spindle, "datetime", FrozenDateTime):
        secs = _parse_duration("06:00")
        assert secs is not None
        assert 1 <= secs <= 86400
