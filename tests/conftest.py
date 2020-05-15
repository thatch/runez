import os

import pytest

import runez
from runez.__main__ import main
from runez.base import LOG, stringified
from runez.conftest import cli, isolated_log_setup, IsolatedLogSetup, logged, temp_folder
from runez.context import CaptureOutput
from runez.convert import short
from runez.file import readlines
from runez.logsetup import LogManager


runez.date.DEFAULT_TIMEZONE = runez.date.UTC
runez.serialize.set_default_behavior(strict=False, extras=True)
cli.default_main = main


# This is here only to satisfy flake8, mentioning the imported fixtures so they're not declared "unused"
assert all(s for s in [cli, isolated_log_setup, logged, temp_folder])


class TempLog(object):
    """Extra test-oriented convenience on top of runez.TrackedOutput"""

    def __init__(self, tracked):
        """
        Args:
            tracked (runez.TrackedOutput): Tracked output
        """
        self.folder = os.getcwd()
        self.tracked = tracked
        self.stdout = tracked.stdout
        self.stderr = tracked.stderr

    @property
    def logfile(self):
        if LogManager.file_handler:
            return short(LogManager.file_handler.baseFilename)

    def expect_logged(self, *expected):
        assert self.logfile, "Logging to a file was not setup"
        remaining = set(expected)
        with open(LogManager.file_handler.baseFilename, "rt") as fh:
            for line in fh:
                found = [msg for msg in remaining if msg in line]
                remaining.difference_update(found)

        if remaining:
            LOG.info("File contents:")
            LOG.info("\n".join(readlines(LogManager.file_handler.baseFilename)))

        assert not remaining

    def clear(self):
        self.tracked.clear()

    def __repr__(self):
        return stringified(self.tracked)

    def __str__(self):
        return self.folder

    def __contains__(self, item):
        return item in self.tracked

    def __len__(self):
        return len(self.tracked)


@pytest.fixture
def temp_log():
    with IsolatedLogSetup():
        with CaptureOutput() as tracked:
            yield TempLog(tracked)
