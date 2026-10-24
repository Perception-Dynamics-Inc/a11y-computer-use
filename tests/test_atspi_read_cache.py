"""Getter cache around one Linux AT-SPI driver call.

The cache is hermetic: stand-ins, no gi and no accessibility bus. It covers
duplicate role reads, the names and failed counts that must stay live, and
the mutators that drop the cache before the tree is read again.
"""

from __future__ import annotations
import pytest

from a11y_computer_use.drivers import _atspi


class _Node:
    def __init__(self, role: str = "push button", name: str = "Save", count: int = 2):
        self.role = role
        self.name = name
        self.count = count
        self.role_calls = 0
        self.name_calls = 0
        self.count_calls = 0
        self.child_calls: list[int] = []
        self.attr_calls = 0
        self.pid_calls = 0
        self.writes = 0
        self.pid = 42

    def get_role_name(self):
        self.role_calls += 1
        return self.role

    def get_name(self):
        self.name_calls += 1
        return self.name

    def get_child_count(self):
        self.count_calls += 1
        return self.count

    def get_child_at_index(self, index):
        self.child_calls.append(index)
        if index < 0:
            return None
        return f"child-{index}"

    def get_attributes(self):
        self.attr_calls += 1
        return {}

    def get_process_id(self):
        self.pid_calls += 1
        return self.pid

    def clear_cache(self):
        self.writes += 1
        return True

    def set_current_value(self, value):
        self.writes += 1
        return True


def test_role_is_read_once_inside_the_cache() -> None:
    node = _Node()
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_role_name",)) == "push button"
        assert _atspi._call_first(node, ("get_role_name",)) == "push button"
    assert node.role_calls == 1


def test_calls_outside_the_cache_always_hit_the_object() -> None:
    node = _Node()
    assert _atspi._call_first(node, ("get_role_name",)) == "push button"
    assert _atspi._call_first(node, ("get_role_name",)) == "push button"
    assert node.role_calls == 2


def test_state_set_is_not_cached() -> None:
    """A click or Escape changes state without an AT-SPI mutator.

    Caching the set inside one driver call reported a combo still expanded
    and a Qt cell still unselected after the input that changed them.
    """

    class Node:
        def __init__(self):
            self.states = {"ENABLED"}
            self.calls = 0

        def get_state_set(self):
            self.calls += 1
            return set(self.states)

    node = Node()
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_state_set",)) == {"ENABLED"}
        node.states.add("SELECTED")
        assert _atspi._call_first(node, ("get_state_set",)) == {"ENABLED", "SELECTED"}
    assert node.calls == 2


def test_name_is_not_cached() -> None:
    node = _Node()
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_name",), default="") == "Save"
        assert _atspi._call_first(node, ("get_name",), default="") == "Save"
    assert node.name_calls == 2


def test_empty_role_is_not_cached() -> None:
    node = _Node(role="")
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_role_name",), default="") == ""
        assert _atspi._call_first(node, ("get_role_name",), default="") == ""
    assert node.role_calls == 2


def test_negative_child_count_is_not_cached() -> None:
    node = _Node(count=-1)
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_child_count",), default=0) == -1
        assert _atspi._call_first(node, ("get_child_count",), default=0) == -1
    assert node.count_calls == 2


def test_zero_child_count_is_cached() -> None:
    node = _Node(count=0)
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_child_count",), default=0) == 0
        assert _atspi._call_first(node, ("get_child_count",), default=0) == 0
    assert node.count_calls == 1


def test_child_index_is_cached_and_a_missing_child_is_not() -> None:
    node = _Node()
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_child_at_index",), 0) == "child-0"
        assert _atspi._call_first(node, ("get_child_at_index",), 0) == "child-0"
        assert _atspi._call_first(node, ("get_child_at_index",), 1) == "child-1"
        assert _atspi._call_first(node, ("get_child_at_index",), -1) is None
        assert _atspi._call_first(node, ("get_child_at_index",), -1) is None
    assert node.child_calls == [0, 1, -1, -1]


def test_valuetext_attributes_are_not_cached() -> None:
    """A date segment's valuetext updates while set_value is still polling.

    Caching that dict kept the first read ('01') and the call reported
    that the segment never became '18'.
    """

    class Node:
        def __init__(self):
            self.attrs = {"tag": "spinbutton", "valuetext": "01"}
            self.calls = 0

        def get_attributes(self):
            self.calls += 1
            return dict(self.attrs)

    node = Node()
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_attributes",))["valuetext"] == "01"
        node.attrs["valuetext"] = "18"
        assert _atspi._call_first(node, ("get_attributes",))["valuetext"] == "18"
    assert node.calls == 2


def test_empty_attribute_dict_is_cached() -> None:
    node = _Node()
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_attributes",)) == {}
        assert _atspi._call_first(node, ("get_attributes",)) == {}
    assert node.attr_calls == 1


def test_failed_read_is_not_cached() -> None:
    class Boom:
        def __init__(self):
            self.calls = 0

        def get_role_name(self):
            self.calls += 1
            raise RuntimeError("dbus")

    node = Boom()
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_role_name",), default="") == ""
        assert _atspi._call_first(node, ("get_role_name",), default="") == ""
    assert node.calls == 2


def test_mutator_drops_the_cache_before_it_runs() -> None:
    node = _Node()
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_role_name",)) == "push button"
        assert _atspi._call_first(node, ("clear_cache", "clearCache")) is True
        assert _atspi._call_first(node, ("get_role_name",)) == "push button"
    assert node.role_calls == 2
    assert node.writes == 1


def test_inner_block_reuses_the_outer_cache() -> None:
    node = _Node()
    with _atspi.read_cache():
        _atspi._call_first(node, ("get_role_name",))
        with _atspi.read_cache():
            _atspi._call_first(node, ("get_role_name",))
    assert node.role_calls == 1


def test_inner_mutator_drops_the_outer_cache() -> None:
    node = _Node()
    with _atspi.read_cache():
        _atspi._call_first(node, ("get_role_name",))
        with _atspi.read_cache():
            _atspi._call_first(node, ("clear_cache",))
        _atspi._call_first(node, ("get_role_name",))
    assert node.role_calls == 2


def test_zero_process_id_is_not_cached() -> None:
    node = _Node()
    node.pid = 0
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_process_id",)) == 0
        assert _atspi._call_first(node, ("get_process_id",)) == 0
    assert node.pid_calls == 2


def test_unhashable_arguments_are_not_cached() -> None:
    class Weird:
        def __init__(self):
            self.calls = 0

        def get_attributes(self, extra):
            self.calls += 1
            return {"tag": "button"}

    node = Weird()
    extra = {"k": 1}
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_attributes",), extra) == {"tag": "button"}
        assert _atspi._call_first(node, ("get_attributes",), extra) == {"tag": "button"}
    assert node.calls == 2


def test_cached_attribute_dict_is_a_copy() -> None:
    node = _Node()
    node_attrs = {"id": "save"}

    def get_attributes():
        node.attr_calls += 1
        return node_attrs

    node.get_attributes = get_attributes
    with _atspi.read_cache():
        first = _atspi._call_first(node, ("get_attributes",))
        first["id"] = "changed"
        second = _atspi._call_first(node, ("get_attributes",))
    assert second == {"id": "save"}
    assert node.attr_calls == 1


def test_value_setter_drops_the_cache(monkeypatch) -> None:
    class FakeValue:
        @staticmethod
        def set_current_value(acc, number):
            acc.writes += 1

        @staticmethod
        def get_current_value(acc):
            acc.writes += 10
            return 1.0

    class FakeAtspi:
        Value = FakeValue

    monkeypatch.setattr(_atspi, "_atspi", lambda: FakeAtspi)
    node = _Node()
    with _atspi.read_cache():
        _atspi._call_first(node, ("get_role_name",))
        _atspi._value_call(("get_current_value",), node)
        assert node.role_calls == 1
        _atspi._value_call(("set_current_value",), node, 1.0)
        _atspi._call_first(node, ("get_role_name",))
    assert node.role_calls == 2
    assert node.writes == 11


def test_move_caret_drops_the_cache() -> None:
    moved: list[int] = []

    class CaretText:
        @staticmethod
        def set_caret_offset(acc, offset):
            moved.append(offset)
            return True

    class FakeAtspi:
        Text = CaretText

    node = _Node()
    with _atspi.read_cache():
        _atspi._call_first(node, ("get_role_name",))
        assert _atspi._move_caret(FakeAtspi, node, 3) is True
        _atspi._call_first(node, ("get_role_name",))
    assert node.role_calls == 2
    assert moved == [3]


def test_linux_driver_run_caches_duplicate_getters(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi_events
    from a11y_computer_use.drivers.linux import LinuxDriver

    # This test runs the inline path. macOS and Windows CI have no gi.
    monkeypatch.setattr(_atspi_events, "enabled", lambda: False)
    node = _Node()

    def work():
        _atspi._call_first(node, ("get_role_name",))
        _atspi._call_first(node, ("get_role_name",))
        _atspi._call_first(node, ("get_name",), default="")
        _atspi._call_first(node, ("get_name",), default="")
        return "ok"

    assert LinuxDriver()._run(work) == "ok"
    assert node.role_calls == 1
    assert node.name_calls == 2


def test_toolkit_probes_reuse_application_until_mutation() -> None:
    """Calc row probes must not pay for each repeated owner lookup."""
    class Application:
        def get_toolkit_name(self):
            return "gtk"

        def get_name(self):
            return "soffice"

    class Node:
        def __init__(self):
            self.calls = 0

        def get_application(self):
            self.calls += 1
            # Model GI wrappers that can disappear between getter calls.
            return Application()

        def clear_cache(self):
            return True

    node = Node()
    with _atspi.read_cache():
        assert not _atspi._qt_app(node)
        assert not _atspi._chromium_app(node)
        assert not _atspi._gecko_app(node)
        assert node.calls == 1
        _atspi._call_first(node, ("clear_cache",))
        assert not _atspi._qt_app(node)
        assert node.calls == 2
    with _atspi.read_cache():
        assert not _atspi._qt_app(node)
    assert node.calls == 3


def test_application_lookup_retries_missing_owner_and_caches_binding_alias() -> None:
    class Node:
        def __init__(self):
            self.calls = 0
            self.owner = object()

        def getApplication(self):
            self.calls += 1
            return None if self.calls == 1 else self.owner

    node = Node()
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_application", "getApplication")) is None
        assert _atspi._call_first(node, ("get_application", "getApplication")) is node.owner
        assert _atspi._call_first(node, ("get_application", "getApplication")) is node.owner
    assert node.calls == 2


def test_child_refresh_invalidates_nested_structure_but_retains_metadata() -> None:
    node = _Node()
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_role_name",)) == "push button"
        assert _atspi._call_first(node, ("get_child_count",)) == 2
        assert _atspi._call_first(node, ("get_child_at_index",), 0) == "child-0"
        with _atspi.read_cache():
            node.count = 3
            _atspi._drop_child_read_cache()
            assert _atspi._call_first(node, ("get_role_name",)) == "push button"
            assert _atspi._call_first(node, ("get_child_count",)) == 3
            assert _atspi._call_first(node, ("get_child_at_index",), 0) == "child-0"
        assert _atspi._call_first(node, ("get_child_count",)) == 3
    assert node.role_calls == 1
    assert node.count_calls == 3
    assert node.child_calls == [0, 0]


def test_firefox_visible_refresh_does_not_invalidate_child_cache(monkeypatch) -> None:
    """Chromium effect discovery must not run while observing Gecko fields."""
    node = _Node()
    node.get_application = lambda: node
    node.get_toolkit_name = lambda: "Gecko"
    node.name = "Firefox"

    def unexpected_discovery(_root):
        raise AssertionError("Firefox must not enter Chromium list discovery")

    monkeypatch.setattr(_atspi, "_list_containers", unexpected_discovery)
    with _atspi.read_cache():
        assert _atspi._call_first(node, ("get_child_count",)) == 2
        _atspi.ATSPIAccessor().refresh_visible(node)
        assert _atspi._call_first(node, ("get_child_count",)) == 2
    assert node.count_calls == 1


@pytest.mark.parametrize("relation", ["same-native", "other-parent", "other-bus", "unknown"])
def test_parent_application_sharing_requires_native_parent_and_bus(monkeypatch, relation):
    """Fresh GI wrappers must share reads without crossing application owners."""
    parent = _Node(role="section")
    wrapper = _Node(role="section")
    child = _Node()
    owner = object()
    parent.get_application = lambda: owner
    child.get_parent = lambda: wrapper
    calls = []
    child.get_application = lambda: calls.append(1) or owner
    keys = {
        id(parent): (":1.42", "/parent"),
        id(wrapper): (":1.42", "/parent" if relation != "other-parent" else "/elsewhere"),
        id(child): ((":1.99" if relation == "other-bus" else ":1.42"), "/child"),
    }
    if relation == "unknown":
        keys[id(wrapper)] = None
    monkeypatch.setattr(_atspi, "_atspi_wrapper_key", lambda node: keys.get(id(node)))
    with _atspi.read_cache():
        _atspi._share_parent_application(parent, [child])
        assert _atspi._call_first(child, ("get_application",)) is owner
        assert calls == ([] if relation == "same-native" else [1])
        _atspi._drop_read_cache()
        assert _atspi._call_first(child, ("get_application",)) is owner
    assert len(calls) == (1 if relation == "same-native" else 2)
