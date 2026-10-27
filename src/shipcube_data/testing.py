"""Test doubles. Never used on a production code path; import only from tests or DEV_MODE tooling."""

from __future__ import annotations

from typing import Callable


class MockMuse:
    """Stand-in for Muse Spark in tests and demos: returns canned replies and records prompts."""

    def __init__(self, reply: str | Callable[[str, str], str]):
        self.reply, self.calls = reply, []

    def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.reply(system, user) if callable(self.reply) else self.reply
