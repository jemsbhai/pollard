"""Small pinned module fixed by the EXP-006B case study."""


def clamp(value: int, lower: int, upper: int) -> int:
    """Return value constrained to the inclusive lower/upper interval."""

    if lower > upper:
        raise ValueError("lower bound must not exceed upper bound")
    return max(lower, min(upper, value))
