"""Shared role normalization for find queries and durable locators."""

#: Snapshot subrole for a Qt single-line editable line edit (#1494).
#: The AT-SPI role stays ``text`` and the snapshot role stays ``textarea``.
LINE_EDIT_SUBROLE = "lineedit"

_ROLE_ALIASES = {
    "push button": "button",
    "pushbutton": "button",
    "text field": "textfield",
    "textbox": "textfield",
    "static text": "statictext",
    # AT-SPI and CDP expose a window splitter as AXSplitter. find(role="separator")
    # is that control. The alias is exact, so it does not match a longer role.
    "separator": "splitter",
    "password": "securetextfield",
    "password field": "securetextfield",
    "passwordfield": "securetextfield",
    "password text": "securetextfield",
}

#: Queries for an ordinary text field. They must not select a secure field,
#: including the ``textfield`` substring of ``securetextfield`` (#1495).
_NONSECURE_TEXT_QUERIES = frozenset({"textfield", "textbox", "text field"})


def _role_name(role: str) -> str:
    return " ".join(str(role or "").split()).casefold().removeprefix("ax")


def normalize_role(role: str) -> str:
    """Strip an optional AX prefix and resolve a complete role alias."""
    name = _role_name(role)
    return _ROLE_ALIASES.get(name, name)


def _element_flag(element: object | None, name: str) -> bool:
    return bool(getattr(element, name, False))


def _single_line_textarea(actual_key: str, element: object | None) -> bool:
    """A Qt line edit exposed as textarea: editable, not secure, marked lineedit."""
    if actual_key != "textarea" or element is None:
        return False
    if _element_flag(element, "secure") or not _element_flag(element, "editable"):
        return False
    return getattr(element, "subrole", None) == LINE_EDIT_SUBROLE


def roles_match(
    actual: str,
    wanted: str,
    *,
    substring: bool = False,
    element: object | None = None,
) -> bool:
    """Match canonical roles; substring queries keep aliases exact.

    For example, ``button`` can find a menu button, but ``textbox`` aliases
    only ``textfield``, never a secure field or a textfield container.
    ``textbox`` and ``textfield`` also match one single-line editable
    ``textarea`` marked ``lineedit`` (Qt 6 ``QLineEdit``). They do not match
    a multi-line text area. ``password`` aliases ``securetextfield``.
    A ``textfield`` query does not substring-match that secure role.
    """
    actual_name = _role_name(actual)
    wanted_name = _role_name(wanted)
    actual_key = _ROLE_ALIASES.get(actual_name, actual_name)
    wanted_key = _ROLE_ALIASES.get(wanted_name, wanted_name)
    if wanted_name in _NONSECURE_TEXT_QUERIES:
        if actual_key == "securetextfield" or _element_flag(element, "secure"):
            return False
        if _single_line_textarea(actual_key, element):
            return True
    if substring and wanted_name not in _ROLE_ALIASES:
        return wanted_key in actual_key
    return actual_key == wanted_key
