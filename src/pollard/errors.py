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
