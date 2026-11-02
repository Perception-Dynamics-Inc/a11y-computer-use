"""A set_value refusal's structuredContent ``next`` matches its detail (#1445)."""
from a11y_computer_use import outcome
from a11y_computer_use.drivers import _atspi


def _refusal(restored):
    return _atspi._text_mismatch(
        "text_mismatch", "set_value could not set 'x'", input_sent=True,
        restored=restored, outcome="refused", next=["type", "ref"],
    )


def test_junk_mask_refusal_structured_next_is_type_then_ref():
    exc = _refusal(False)
    result = outcome.refused_result(text="t", code=str(exc.code), message=str(exc), detail=exc.detail)
    assert exc.detail["next"] == ["type", "ref"]
    assert result.as_dict()["next"] == ["type", "ref"]


def test_emptied_field_refusal_structured_next_is_type_then_ref():
    exc = _refusal(True)
    assert outcome.next_for_error(str(exc.code), exc.detail) == ("type", "ref")


def test_unknown_strategies_are_still_dropped():
    assert outcome.clean_next(["type", "bogus", "ref", "type"]) == ("type", "ref")
