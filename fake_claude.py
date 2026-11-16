"""A scripted stand-in for the Anthropic client, so the agent can be run offline for free.

Used by main.py (Step 11) and evaluations/eval.py (offline mode). It returns pre-written replies in
order. When the script runs out it keeps answering with a short question, which is what
the agent sees when it asks for clarification.
"""
from __future__ import annotations

from types import SimpleNamespace


def tool_call(n: int, name: str, **args) -> SimpleNamespace:
    """A fake 'tool_use' block: Claude asking for a tool by name."""
    return SimpleNamespace(type="tool_use", id=f"t{n}", name=name, input=args)


def say(text: str) -> SimpleNamespace:
    """A fake text block."""
    return SimpleNamespace(type="text", text=text)


def reply(stop_reason: str, *blocks) -> SimpleNamespace:
    """A fake API response with a stop_reason and content blocks."""
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks))


class FakeClaude:
    """Looks like anthropic.Anthropic() to the agent: client.beta.messages.create(...)."""

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        if self.replies:
            return self.replies.pop(0)
        return reply("end_turn", say("Which task matters most to you?"))
