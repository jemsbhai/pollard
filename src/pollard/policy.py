"""Policy hooks for registered tool calls."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ._canon import IdentityValue
from .registry import ActionSpec


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class PolicyContext:
    spec: ActionSpec
    args: dict[str, IdentityValue]
    cursor_id: str
    run_label: str
    counters: dict[str, float]


class Policy(Protocol):
    def decide(self, ctx: PolicyContext) -> Decision: ...
