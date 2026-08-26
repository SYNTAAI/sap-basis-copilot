"""Risk-level classification helpers."""


def risk_level(count: int, high_threshold: int = 10, medium_threshold: int = 1) -> str:
    """Return 'HIGH', 'MEDIUM', or 'LOW' based on *count* vs thresholds."""
    if count >= high_threshold:
        return "HIGH"
    if count >= medium_threshold:
        return "MEDIUM"
    return "LOW"
