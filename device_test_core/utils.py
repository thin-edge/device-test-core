import randomname


def generate_name(prefix: str = None, sep="-") -> str:
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
