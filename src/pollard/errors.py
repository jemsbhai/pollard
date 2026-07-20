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
