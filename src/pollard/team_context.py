"""Immutable identities and transport records for governed teams."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ._canon import IdentityValue

_NODE_ID = re.compile(r"[0-9a-f]{64}\Z")


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _validate_name(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value or value != value.strip() or len(value) > 256:
        raise ValueError(f"{field} must be a nonempty name of at most 256 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} cannot contain control characters")
    return value


def _validate_tools(value: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, tuple):
        raise TypeError("allowed_tools must be a tuple or None")
    for name in value:
        _validate_name(name, "tool name")
    if len(set(value)) != len(value):
        raise ValueError("allowed_tools cannot contain duplicates")
    return tuple(sorted(value))


def _node_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _NODE_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase 64-character node ID")
    return value


def _fields(value: object, names: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError(f"invalid {label} fields")
    return value


@dataclass(frozen=True)
class AgentIdentity:
    """An actor's declared identity and optional tool capability ceiling."""

    team_id: str
    agent_id: str
    task_id: str
    role: str | None = None
    allowed_tools: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        for name in ("team_id", "agent_id", "task_id"):
            _validate_name(getattr(self, name), name)
        if self.role is not None:
            _validate_name(self.role, "role")
        object.__setattr__(self, "allowed_tools", _validate_tools(self.allowed_tools))

    def to_dict(self) -> dict[str, IdentityValue]:
        return {
            "team_id": self.team_id,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "role": self.role,
            "allowed_tools": None if self.allowed_tools is None else list(self.allowed_tools),
        }

    @classmethod
    def from_dict(cls, value: object) -> AgentIdentity:
        data = _fields(
            value, {"team_id", "agent_id", "task_id", "role", "allowed_tools"}, "agent identity"
        )
        tools = data["allowed_tools"]
        if tools is not None and not isinstance(tools, list):
            raise TypeError("transported allowed_tools must be a list or None")
        return cls(
            team_id=data["team_id"],
            agent_id=data["agent_id"],
            task_id=data["task_id"],
            role=data["role"],
            allowed_tools=None if tools is None else tuple(tools),
        )


@dataclass(frozen=True)
class BudgetContext:
    """A scope anchor and exact decimal limits, without mutable accounting state."""

    anchor_id: str
    limits: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _node_id(self.anchor_id, "budget anchor")
        if not isinstance(self.limits, tuple):
            raise TypeError("budget limits must be a tuple")
        names: set[str] = set()
        normalized: list[tuple[str, str]] = []
        for item in self.limits:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("invalid budget limit")
            name, amount = item
            _validate_name(name, "meter name")
            if name in names or not isinstance(amount, str):
                raise ValueError("invalid or duplicate budget meter")
            try:
                number = Decimal(amount)
            except InvalidOperation as exc:
                raise ValueError("budget limits must be finite nonnegative decimals") from exc
            if not number.is_finite() or number < 0:
                raise ValueError("budget limits must be finite nonnegative decimals")
            names.add(name)
            normalized.append((name, _decimal_text(number)))
        object.__setattr__(self, "limits", tuple(sorted(normalized)))

    def to_dict(self) -> dict[str, IdentityValue]:
        return {"anchor_id": self.anchor_id, "limits": dict(self.limits)}

    @classmethod
    def from_dict(cls, value: object) -> BudgetContext:
        data = _fields(value, {"anchor_id", "limits"}, "budget context")
        limits = data["limits"]
        if not isinstance(limits, dict):
            raise ValueError("budget limits must be an object")
        return cls(data["anchor_id"], tuple(limits.items()))


@dataclass(frozen=True)
class DelegationContext:
    """Transport an exact agent cursor; attachment verifies the recorded authority."""

    root_id: str
    manifest_id: str
    delegation_id: str
    cursor_id: str
    identity: AgentIdentity
    scopes: tuple[BudgetContext, ...]
    config_digest: str

    def __post_init__(self) -> None:
        for name in ("root_id", "manifest_id", "delegation_id", "cursor_id", "config_digest"):
            _node_id(getattr(self, name), name)
        if not isinstance(self.identity, AgentIdentity):
            raise TypeError("identity must be an AgentIdentity")
        if not isinstance(self.scopes, tuple) or not all(
            isinstance(scope, BudgetContext) for scope in self.scopes
        ):
            raise TypeError("scopes must be a tuple of BudgetContext records")
        if len({scope.anchor_id for scope in self.scopes}) != len(self.scopes):
            raise ValueError("duplicate budget scope anchors")

    def to_dict(self) -> dict[str, IdentityValue]:
        return {
            "version": 1,
            "root_id": self.root_id,
            "manifest_id": self.manifest_id,
            "delegation_id": self.delegation_id,
            "cursor_id": self.cursor_id,
            "identity": self.identity.to_dict(),
            "scopes": [scope.to_dict() for scope in self.scopes],
            "config_digest": self.config_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> DelegationContext:
        data = _fields(
            value,
            {
                "version",
                "root_id",
                "manifest_id",
                "delegation_id",
                "cursor_id",
                "identity",
                "scopes",
                "config_digest",
            },
            "delegation context",
        )
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("unsupported delegation context version")
        if not isinstance(data["scopes"], list):
            raise ValueError("transported scopes must be a list")
        return cls(
            root_id=data["root_id"],
            manifest_id=data["manifest_id"],
            delegation_id=data["delegation_id"],
            cursor_id=data["cursor_id"],
            identity=AgentIdentity.from_dict(data["identity"]),
            scopes=tuple(BudgetContext.from_dict(item) for item in data["scopes"]),
            config_digest=data["config_digest"],
        )


@dataclass(frozen=True)
class AgentCheckpoint:
    """A durable checkpoint note plus the exact context committed by that note."""

    checkpoint_id: str
    context: DelegationContext

    def __post_init__(self) -> None:
        _node_id(self.checkpoint_id, "checkpoint_id")
        if not isinstance(self.context, DelegationContext):
            raise TypeError("context must be a DelegationContext")

    def to_dict(self) -> dict[str, IdentityValue]:
        return {
            "version": 1,
            "checkpoint_id": self.checkpoint_id,
            "context": self.context.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> AgentCheckpoint:
        data = _fields(value, {"version", "checkpoint_id", "context"}, "agent checkpoint")
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("unsupported checkpoint version")
        return cls(data["checkpoint_id"], DelegationContext.from_dict(data["context"]))
