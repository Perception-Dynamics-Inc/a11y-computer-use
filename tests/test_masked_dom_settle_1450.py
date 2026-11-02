"""#1450: one unverified/late DOM read must not roll back a formatted value."""
from a11y_computer_use.drivers import _atspi


def test_late_dom_read_is_reread(monkeypatch):
    reads = iter([None, "save10", "SAVE10"])
    monkeypatch.setattr(_atspi, "chromium_dom_text", lambda acc: next(reads))
    monkeypatch.setattr(_atspi.time, "sleep", lambda s: None)
    assert _atspi._dom_text_settles(object(), "SAVE10")


def test_dom_that_never_matches_is_refused(monkeypatch):
    calls = []
    monkeypatch.setattr(_atspi, "chromium_dom_text", lambda acc: calls.append(1) or "zzz")
    monkeypatch.setattr(_atspi.time, "sleep", lambda s: None)
    assert not _atspi._dom_text_settles(object(), "SAVE10")
    assert len(calls) == 3


def test_promo_kept_when_first_dom_read_is_unverified(monkeypatch):
    from tests.test_linux_masked_set_value_1319 import _Field, _wire

    field = _Field("SAVE10")
    _wire(monkeypatch, field)
    first = [True]

    def dom(acc):
        if first[0] and field.text == "SAVE10":
            first[0] = False
            return None  # PRIMARY read not confirmed once (#1450)
        return field.text

    monkeypatch.setattr(_atspi, "chromium_dom_text", dom)
    result = _atspi._replace_with_keys(object(), "save10")
    assert isinstance(result, _atspi._FieldFormattedWrite)
    assert field.text == "SAVE10"
    assert field.dom_writes == []
