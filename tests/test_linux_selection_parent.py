"""Selection ancestry lookup stays bounded by depth when native indices exist."""

import pytest

from a11y_computer_use.drivers import _atspi


class Accessible:
    def __init__(self, role, parent=None, index=None, count=0):
        self.role = role
        self.parent = parent
        self.index = index
        self.count = count
        self.children = {}
        self.reads = []

    def get_role_name(self):
        return self.role

    def get_parent(self):
        return self.parent

    def get_index_in_parent(self):
        return self.index

    def get_child_count(self):
        return self.count

    def get_child_at_index(self, index):
        self.reads.append(index)
        return self.children.get(index)

    def get_selection_iface(self):
        return self if self.role in {"tree table", "list"} else None


@pytest.mark.parametrize("index", [0, 29, 1500, 2999])
@pytest.mark.parametrize("depth", [0, 2])
def test_selection_parent_uses_direct_ancestor_index(index, depth):
    table = Accessible("tree table", count=3000)
    row = Accessible("table cell", table, index)
    table.children[index] = row
    target = row
    for _ in range(depth):
        target = Accessible("table cell", target, 0)
        target.parent.children[0] = target
        target.parent.count = 1

    assert _atspi._selection_parent(target) == (table, index, target)
    assert table.reads == [index]
    assert row.reads == []


@pytest.mark.parametrize("native_index", [None, -1, 3000, True, 0])
def test_selection_parent_falls_back_when_native_index_unusable(native_index):
    table = Accessible("tree table", count=3)
    row = Accessible("table cell", table, native_index)
    table.children[2] = row
    target = Accessible("table cell", row, 0)
    row.children[0] = target
    row.count = 1

    assert _atspi._selection_parent(target) == (table, 2, target)
    assert 2 in table.reads


def test_selection_parent_retains_containment_fallback_for_incomplete_parent_links():
    table = Accessible("tree table", count=1)
    row = Accessible("table cell", table, 0, count=1)
    # Some bridges omit the intervening node in the child's parent link.
    target = Accessible("table cell", table, -1)
    table.children[0] = row
    row.children[0] = target

    assert _atspi._selection_parent(target) == (table, 0, target)


def test_selection_parent_never_selects_menu_items():
    menu = Accessible("menu", count=1)
    target = Accessible("list item", menu, 0)
    menu.children[0] = target
    assert _atspi._selection_parent(target) == (None, None, None)
    assert menu.reads == []
