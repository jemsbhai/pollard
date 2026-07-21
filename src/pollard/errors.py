"""Exception types raised by pollard."""


class PollardError(Exception):
    """Base class for pollard exceptions."""


class IntegrityError(PollardError):
    """Raised when stored tree data fails an integrity check."""


class BudgetExceeded(PollardError):
    """Raised when a budget refuses a step before execution."""

    def __init__(self, message: str, refusal_id: str) -> None:
        super().__init__(message)
        self.refusal_id = refusal_id


class PolicyViolation(PollardError):
    """Raised when the registry or a policy refuses a tool call."""

    def __init__(self, message: str, refusal_id: str) -> None:
        super().__init__(message)
        self.refusal_id = refusal_id


class ConfirmationRequired(PollardError):
    """Raised when a policy requires human confirmation before execution."""

    def __init__(self, message: str, resume_token: str) -> None:
        super().__init__(message)
        self.resume_token = resume_token


class MissingRecording(PollardError):
    """Raised when replay mode cannot find a stored result for a step."""

    def __init__(self, message: str, node_id: str, payload_summary: str) -> None:
        super().__init__(message)
        self.node_id = node_id
        self.payload_summary = payload_summary


class UnsupportedSchema(PollardError):
    """Raised when a registry schema uses unsupported JSON Schema features."""


class PostDispatchOutcomeUnknown(PollardError):
    """Mark a provider-neutral failure whose external outcome is unknown.

    Call adapters may raise this wrapper after dispatch when they cannot tell
    whether the external operation completed. The runtime records and settles
    the conservative precheck estimates, then re-raises ``error``.
    """

    def __init__(self, error: BaseException) -> None:
        if not isinstance(error, BaseException):
            raise TypeError("post-dispatch error must wrap an exception")
        super().__init__("external call outcome is unknown after dispatch")
        self.error = error


def mark_post_dispatch_outcome_unknown(error: BaseException) -> BaseException:
    """Mark a native exception without replacing its public exception type.

    Provider adapters use this after dispatch so callers still receive the
    original SDK exception. A wrapper is returned only for an exception type
    that refuses instance attributes.
    """

    try:
        error.__dict__["_pollard_post_dispatch_outcome_unknown"] = True
    except (AttributeError, TypeError):
        return PostDispatchOutcomeUnknown(error)
    return error


def is_post_dispatch_outcome_unknown(error: BaseException) -> bool:
    """Return whether an exception represents an unknown dispatched outcome."""

    return isinstance(error, PostDispatchOutcomeUnknown) or (
        getattr(error, "_pollard_post_dispatch_outcome_unknown", False) is True
    )


class CallCleanupError(PollardError):
    """Collect secondary failures while preserving a call's primary error."""

    def __init__(self, errors: list[BaseException]) -> None:
        if not errors:
            raise ValueError("cleanup errors must not be empty")
        self.errors = tuple(errors)
        names = ", ".join(type(error).__name__ for error in self.errors)
        super().__init__(f"call cleanup failed with: {names}")


class ReservationLeaseLost(PollardError):
    """Raised after a completed call whose shared reservation lease was lost."""

    def __init__(self, message: str, reservation_id: str, node_id: str) -> None:
        super().__init__(message)
        self.reservation_id = reservation_id
        self.node_id = node_id


class ReservationUncertain(PollardError):
    """Raised when a shared reservation outcome cannot be confirmed."""

    def __init__(self, message: str, reservation_id: str) -> None:
        super().__init__(message)
        self.reservation_id = reservation_id


class SettlementUncertain(PollardError):
    """Raised when a completed call's shared settlement cannot be confirmed."""

    def __init__(self, message: str, reservation_id: str) -> None:
        super().__init__(message)
        self.reservation_id = reservation_id
