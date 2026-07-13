"""Small pinned module with an intentional clamp bug for EXP-006B."""


def clamp(value: int, lower: int, upper: int) -> int:
    """Return value constrained to the inclusive lower/upper interval."""

    return min(lower, max(upper, value))
