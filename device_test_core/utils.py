"""General utilities"""

from typing import Any, Optional
import randomname


def generate_name(prefix: Optional[str] = None, sep="_") -> str:
    """Generate a random name

    Args:
        prefix (str, optional): Prefix the name with a fixed string.
        sep (str, optional): Prefix separator. Defaults to "-".

    Returns:
        str: Random name
    """
    name = randomname.generate("v/", "a/", "n/", sep="_")
    if prefix:
        return sep.join([prefix, name])
    return name


def to_str(value: Any, encoding: str = "utf8") -> str:
    """Convert a value to a string. If the input is bytes, then decode it
    using the given encoding.

    Args:
        value (Any): Input to be decoded to a string
        encoding (str): Encoding if the input is bytes. Defaults to utf8

    Returns:
        str: value decoded as a string
    """
    if hasattr(value, "decode"):
        return value.decode(encoding)

    if not value:
        return ""

    return str(value)
