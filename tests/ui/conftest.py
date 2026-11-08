"""Shared UI test helpers for platform-dependent filesystem capabilities."""

import os

import pytest


@pytest.fixture
def make_symlink():
    def create_symlink(link, target, **kwargs):
        try:
            return link.symlink_to(target, **kwargs)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                pytest.skip(
                    "Symbolic-link privilege unavailable on Windows; "
                    "run on Linux or enable Developer Mode"
                )
            raise

    return create_symlink
