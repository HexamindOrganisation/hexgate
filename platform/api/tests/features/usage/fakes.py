"""Test doubles shared across the usage tests."""

from __future__ import annotations


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now
