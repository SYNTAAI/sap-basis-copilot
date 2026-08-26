"""SAP numeric string parsing helper.

SAP ABAP formats negative numbers with a trailing minus sign (e.g. '792.00-').
This module provides a robust parser that handles all SAP numeric formats.
"""


def parse_sap_number(value) -> float:
    """Parse a SAP numeric string into a Python float.

    Handles:
      - Normal numbers: '1234.56' → 1234.56
      - Trailing minus: '792.00-' → -792.0
      - Leading minus: '-792.00' → -792.0
      - Empty/None: '' / None → 0.0
      - Already numeric: 123.4 → 123.4
    """
    if value is None:
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip()
    if not s:
        return 0.0

    # SAP trailing minus: '792.00-' → '-792.00'
    if s.endswith("-"):
        s = "-" + s[:-1]

    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0
