"""Scaffold smoke tests: the schema contract holds and every module imports.

Module behavior is tested by each module's own suite; this file only pins the
shared contract (schema shapes, error codes, stub signatures existing).
"""

from __future__ import annotations

import pytest

from computeruse import act, capture, cli, doctor, observe, safety, schema, server
from computeruse.schema import (
    Bounds,
    Click,
    ComputerUseError,
    ErrorCode,
    MouseButton,
    Point,
    action_to_dict,
)
from tests.conftest import HAS_AX, HAS_SCREEN


def test_permission_probes_return_bools() -> None:
    assert isinstance(HAS_AX, bool)
    assert isinstance(HAS_SCREEN, bool)


def test_synthetic_snapshot_shape(synthetic_snapshot) -> None:
    refs = [el.ref for el in synthetic_snapshot.elements]
    assert len(refs) == len(set(refs)), "refs must be unique within a snapshot"
    assert all(el.snapshot_id == synthetic_snapshot.snapshot_id for el in synthetic_snapshot.elements)
    assert all(el.bounds.display_id == 1 for el in synthetic_snapshot.elements)

    save = synthetic_snapshot.element("e2")
    assert save.actionable
    assert not synthetic_snapshot.element("e5").actionable, "disabled => not actionable"
    assert synthetic_snapshot.element("e4").secure
    with pytest.raises(KeyError):
        synthetic_snapshot.element("e999")


def test_bounds_center_is_display_qualified() -> None:
    bounds = Bounds(display_id=7, x=10, y=20, width=100, height=50)
    assert bounds.center == Point(display_id=7, x=60, y=45)


def test_error_codes_are_wire_stable() -> None:
    assert {code.value for code in ErrorCode} == {
        "stale_ref",
        "permission_denied_accessibility",
        "permission_denied_screen",
        "secure_field",
        "focus_changed",
        "app_not_found",
        "timeout",
        "busy",
        "closed",
        "confirmation_declined",
        "unsupported",
    }


def test_computeruse_error_to_dict() -> None:
    err = ComputerUseError(ErrorCode.STALE_REF, "e14 vanished", {"ref": "e14"})
    assert err.to_dict() == {
        "error": "stale_ref",
        "message": "e14 vanished",
        "detail": {"ref": "e14"},
    }


def test_action_to_dict_serializes_enums_and_nested(synthetic_snapshot) -> None:
    action = Click(target=Point(1, 5, 6), button=MouseButton.RIGHT, count=2)
    payload = action_to_dict(action)
    assert payload["kind"] == "click"
    assert payload["button"] == "right"
    assert payload["target"] == {"display_id": 1, "x": 5, "y": 6}


def test_every_module_imports() -> None:
    # Scaffold-era stub checks retired at integration: all modules are now
    # implemented, so the shared contract here is that they import cleanly.
    for module in (act, capture, cli, doctor, observe, safety, schema, server):
        assert module.__name__.startswith("computeruse.")


def test_schema_exports_action_union() -> None:
    # The Action alias is the safety layer's contract; keep it importable.
    assert schema.Action is not None
    assert schema.MODIFIER_KEYS == {"cmd", "ctrl", "alt", "shift", "fn"}
