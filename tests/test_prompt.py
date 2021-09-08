import os
from unittest.mock import patch

import pytest

import runez
from runez.prompt import ask_once


def custom_serializer(value):
    if value == "invalid":
        return None

    return {"value": value}


def mocked_input(x):
    return x


def test_no_tty():
    assert ask_once("test", "Please enter value: ") is None
    with pytest.raises(runez.system.AbortException):
        ask_once("test", "Please enter value: ", fatal=True)


def test_with_tty(monkeypatch, logged):
    with patch("runez.prompt.input", side_effect=mocked_input):
        monkeypatch.setattr(runez.SYS_INFO.terminal, "is_stdout_tty", True)
        expected = {"value": "foo"}
        with runez.TempFolder() as tmp:
            assert ask_once("test", "foo", base=tmp, serializer=custom_serializer) == expected
            assert "Saved test.json" in logged.pop()

            assert runez.read_json("test.json", logger=None) == expected
            assert ask_once("test", "bar", base=tmp) == expected  # Ask a 2nd time, same response
            assert not logged

            # Verify that if `serializer` returns None, value is not returned/stored
            with pytest.raises(Exception):
                ask_once("test-invalid", "invalid", base=tmp, serializer=custom_serializer, fatal=True)
            assert "Invalid value provided for test-invalid" in logged.pop()
            assert not os.path.exists("test-invalid.json")

            # Same, but don't raise exception (returns default)
            assert ask_once("test-invalid", "invalid", base=tmp, serializer=custom_serializer) is None
            assert not logged  # Not logged by default (can be turned on via logger=)

            # Simulate no value provided
            with pytest.raises(Exception):
                ask_once("test-invalid", "", base=tmp, serializer=custom_serializer, fatal=True)
            assert "No value provided" in logged.pop()

    with patch("runez.prompt.input", side_effect=KeyboardInterrupt):
        # Simulate CTRL+C
        with pytest.raises(Exception):
            ask_once("test2", "test2", base=tmp, serializer=custom_serializer, fatal=True)
        assert "Cancelled by user" in logged.pop()
