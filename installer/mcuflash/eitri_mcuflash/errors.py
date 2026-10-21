# SPDX-License-Identifier: Apache-2.0
"""Error types.

Every failure the user can see has to say *which board*, *which port*, *which
bootloader*, *which step* and *what to do next* (installer design, section 4), so
all errors carry those fields and render them the same way.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class FlashError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        step: Optional[str] = None,
        board: Optional[str] = None,
        port: Optional[str] = None,
        bootloader: Optional[str] = None,
        recovery: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.step = step
        self.board = board
        self.port = port
        self.bootloader = bootloader
        self.recovery = recovery

    def with_context(self, **ctx: Any) -> "FlashError":
        """Fill in context fields that the raiser did not know (never overwrite)."""
        for key, value in ctx.items():
            if value is not None and getattr(self, key, None) is None:
                setattr(self, key, value)
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "step": self.step,
            "board": self.board,
            "port": self.port,
            "bootloader": self.bootloader,
            "recovery": self.recovery,
        }

    def __str__(self) -> str:
        where = ", ".join(
            "%s=%s" % (k, v)
            for k, v in (
                ("board", self.board),
                ("port", self.port),
                ("bootloader", self.bootloader),
                ("step", self.step),
            )
            if v
        )
        text = self.message + ((" [" + where + "]") if where else "")
        if self.recovery:
            text += "\n  recovery: " + self.recovery
        return text


class TransportError(FlashError):
    """Serial port could not be opened, configured or used."""

    def __init__(self, message: str, **kw: Any) -> None:
        kw.setdefault("code", "transport")
        super().__init__(message, **kw)


class ProtocolError(FlashError):
    """The peer answered, but not with something we can parse or trust."""

    def __init__(self, message: str, **kw: Any) -> None:
        kw.setdefault("code", "protocol")
        super().__init__(message, **kw)

