"""Deprecated compatibility shell for the removed public computer-use tool.

Desktop operations now enter through the session-bound ``vision_desktop``
executable bridge. Keeping this importable for downstream code is useful, but
it must never recreate the old one-call/private-grounding path.
"""

from __future__ import annotations

from .errors import ComputerUseError


class ComputerUseService:
    """Fail-closed compatibility object; use ``vision_desktop`` instead."""

    def __init__(self, *args, **kwargs):
        self._closed = False

    async def execute_async(self, *args, **kwargs):
        raise ComputerUseError(
            "LEGACY_EXECUTABLE_REQUIRED",
            "use the session-bound vision_desktop executable sequence",
        )

    def execute(self, *args, **kwargs):
        raise ComputerUseError(
            "LEGACY_EXECUTABLE_REQUIRED",
            "use the session-bound vision_desktop executable sequence",
        )

    async def dispose_session(self, session_id: str):
        return None

    async def shutdown(self):
        self._closed = True


__all__ = ["ComputerUseService"]
