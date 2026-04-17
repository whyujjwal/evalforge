"""Smoke test so the CI pipeline has at least one green test on an empty tree."""

from __future__ import annotations

import evalforge


def test_version_is_set() -> None:
    assert evalforge.__version__
