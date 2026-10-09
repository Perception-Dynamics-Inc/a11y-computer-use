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
    assert info.value.detail["settle_s"] == 30
    # The probe at t=4 records elapsed 4, then the sleep reaches the deadline
    # and no further probe starts.
    assert info.value.detail["elapsed_s"] == pytest.approx(4.0)
    assert info.value.detail["waited_s"] == pytest.approx(5.0)
    assert info.value.detail["polls"] == 5 and "condition" in info.value.detail


def test_settle_retries_when_sleep_is_interrupted(monkeypatch) -> None:
    """macOS can raise InterruptedError from time.sleep on SIGTERM."""
    clock = [100.0]
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise InterruptedError("interrupted system call")
        clock[0] += seconds

    monkeypatch.setattr(conditions.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(conditions.time, "sleep", sleep)
    result = conditions.Checker().wait({"settle": 1}, timeout_s=10, poll_s=1)
    assert result["matched"] == "settled for 1s"
    assert calls["n"] >= 2


def test_interrupted_sleep_stops_the_wait_when_cancel_is_set(monkeypatch) -> None:
    """A signal during the slice must not keep the settle running."""
    clock = [0.0]

    def sleep(_seconds: float) -> None:
        raise InterruptedError("interrupted system call")

    monkeypatch.setattr(conditions.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(conditions.time, "sleep", sleep)
    flag = {"stop": False}

    def on_interrupt() -> None:
        flag["stop"] = True

    result = conditions.Checker().wait(
        {"settle": 30},
        timeout_s=60,
        poll_s=1,
        stop=lambda: flag["stop"],
        on_interrupt=on_interrupt,
    )
    assert result["matched"] == "cancelled"
    assert flag["stop"] is True


def test_settle_rejects_negative_or_absurd_seconds() -> None:
    with pytest.raises(ValueError):
        conditions.Checker().probe({"settle": -1}, {})
    with pytest.raises(ValueError):
        conditions.Checker().probe({"settle": conditions.MAX_WAIT_UNTIL_S + 1}, {})


def test_settle_is_a_known_kind() -> None:
    assert conditions.kind_of({"settle": 2}) == "settle"
    with pytest.raises(ValueError):
        conditions.kind_of({"settle": 2, "file_exists": "~/x"})
