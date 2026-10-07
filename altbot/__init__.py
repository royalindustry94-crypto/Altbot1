"""altbot — personal, non-custodial spot trading automation."""

from decimal import Decimal

__version__ = "0.1.0"


def dec(value, default: Decimal | None = None) -> Decimal | None:
    """Convert to Decimal via str() so float noise never enters the books."""
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
