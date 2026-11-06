"""Shared role normalization for find queries and durable locators."""

_ROLE_ALIASES = {
    "push button": "button",
    "pushbutton": "button",
    "text field": "textfield",
    "textbox": "textfield",
    "static text": "statictext",
}


def _role_name(role: str) -> str:
    return " ".join(str(role or "").split()).casefold().removeprefix("ax")


def normalize_role(role: str) -> str:
    """Strip an optional AX prefix and resolve a complete role alias."""
    name = _role_name(role)
    return _ROLE_ALIASES.get(name, name)


def roles_match(actual: str, wanted: str, *, substring: bool = False) -> bool:
    """Match canonical roles; substring queries keep aliases exact.

    For example, ``button`` can find a menu button, but ``textbox`` aliases
    only ``textfield``, never a secure field or a textfield container.
    """
    actual_key = normalize_role(actual)
    wanted_key = normalize_role(wanted)
    if substring and _role_name(wanted) not in _ROLE_ALIASES:
        return wanted_key in actual_key
    return actual_key == wanted_key
