"""Timestamp parsing"""

from typing import Union
from datetime import datetime
import dateparser

Timestamp = Union[datetime, int, float, str]


def parse_timestamp(value: Timestamp) -> datetime:
    """Convert to a datetime from various inputs

    Args:
        value (Timestamp): Value to be converted to a datetime object

    Raises:
        ValueError: Invalid input

    Returns:
        datetime: datetime
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    output = dateparser.parse(value)
    if output:
        return output
    raise ValueError(f"Invalid date format. value={value}")
