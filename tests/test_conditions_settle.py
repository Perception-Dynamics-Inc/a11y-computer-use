"""``{"settle": seconds}``: the one wait_until condition with nothing to
observe. The Krita trial on the Box (a Qt app with no accessibility tree)
had the planner waiting on ``file_stable ~/.bashrc`` as a sleep substitute;
a named condition keeps that intent honest and capped."""

import pytest

from a11y_computer_use import conditions
from a11y_computer_use.schema import ComputerUseError, ErrorCode


def test_settle_holds_once_the_seconds_have_passed(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(conditions.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(conditions.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    checker = conditions.Checker()
    result = checker.wait({"settle": 3}, timeout_s=10, poll_s=1)
    assert result["matched"] == "settled for 3s"
    assert result["waited_s"] == pytest.approx(3.0)
    assert result["polls"] == 4  # t=0, 1, 2 miss; t=3 holds


def test_settle_longer_than_the_timeout_is_a_timeout(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(conditions.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(conditions.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    with pytest.raises(ComputerUseError) as info:
        conditions.Checker().wait({"settle": 30}, timeout_s=5, poll_s=1)
    assert info.value.code is ErrorCode.TIMEOUT


def test_settle_rejects_negative_or_absurd_seconds() -> None:
    with pytest.raises(ValueError):
        conditions.Checker().probe({"settle": -1}, {})
    with pytest.raises(ValueError):
        conditions.Checker().probe({"settle": conditions.MAX_WAIT_UNTIL_S + 1}, {})


def test_settle_is_a_known_kind() -> None:
    assert conditions.kind_of({"settle": 2}) == "settle"
    with pytest.raises(ValueError):
        conditions.kind_of({"settle": 2, "file_exists": "~/x"})
