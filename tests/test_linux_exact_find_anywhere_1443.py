"""find reaches an exact name far past the first partial match (#1443)."""
from types import SimpleNamespace as _NS

import pytest

from a11y_computer_use.drivers import _atspi
from tests.test_linux_synthetic import _Acc, _adopt, _place_row, fake_atspi  # noqa: F401


def _page(filler=4000):
    prose = _Acc("link", name="W3C homepage", width=300, height=20)
    _place_row(prose, 10, 100, 300, 20)
    rows = [_adopt(_Acc("paragraph"), _Acc("static", name=f"filler {i}")) for i in range(filler)]
    decoy = _Acc("link", name="Homework", width=40, height=20)
    home = _Acc("link", name="Home", width=40, height=20)
    _place_row(home, 10, 90000, 40, 20)
    document = _adopt(_Acc("document web"), prose, _adopt(_Acc("section"), *rows), decoy, home)
    return document, prose, decoy, home


def _collection(monkeypatch, document, controls, queries):
    fake = _NS(
        Role=_NS(LINK="link", PUSH_BUTTON="push button", HEADING="heading"),
        StateSet=_NS(new=lambda states: tuple(states)),
        CollectionMatchType=_NS(ALL="all", ANY="any", NONE="none"),
        CollectionSortOrder=_NS(CANONICAL="canonical"),
        MatchRule=_NS(new=lambda *args: args),
    )

    def get_matches(rule, order, limit, traverse):
        queries.append((tuple(rule[4]), limit))
        return list(controls)

    document.get_collection_iface = lambda: _NS(get_matches=get_matches)
    monkeypatch.setattr(_atspi, "_atspi", lambda: fake)


@pytest.mark.parametrize("exact_only", [False, True])
def test_exact_name_far_past_partial_match_is_revealed(fake_atspi, monkeypatch, exact_only):
    document, prose, decoy, home = _page()
    queries = []
    _collection(monkeypatch, document, [prose, decoy, home], queries)
    monkeypatch.setattr(_atspi, "_screen_size", lambda: (1280, 800))
    revealed = []
    monkeypatch.setattr(_atspi, "scroll_to_edge", lambda node, edge: revealed.append(node))
    reads = []
    original = _Acc.get_role_name
    monkeypatch.setattr(_Acc, "get_role_name", lambda self: reads.append(self) or original(self))
    assert _atspi.reveal_text_in_document(document, "Home", exact_only=exact_only)
    assert revealed == [home]
    assert len(queries) == 1 and "link" in queries[0][0]
    # The tree walk itself stays bounded; Collection did the long reach.
    assert len({id(n) for n in reads}) <= _atspi._SEEK_EXACT_AFTER_SUBSTRING + 20


def test_without_collection_the_partial_match_is_still_the_answer(fake_atspi, monkeypatch):
    document, prose, _decoy, _home = _page()
    monkeypatch.setattr(_atspi, "_screen_size", lambda: (1280, 800))
    monkeypatch.setattr(_atspi, "scroll_to_edge", lambda node, edge: pytest.fail("prose is on screen"))
    assert _atspi.reveal_text_in_document(document, "Home")
    assert not _atspi.reveal_text_in_document(document, "Home", exact_only=True)


def test_exact_name_found_by_the_walk_makes_no_collection_query(fake_atspi, monkeypatch):
    document, prose, decoy, home = _page(filler=20)
    queries = []
    _collection(monkeypatch, document, [prose, decoy, home], queries)
    monkeypatch.setattr(_atspi, "_screen_size", lambda: (1280, 800))
    monkeypatch.setattr(_atspi, "scroll_to_edge", lambda node, edge: None)
    assert _atspi.reveal_text_in_document(document, "Home")
    assert queries == []


def test_collection_without_an_exact_name_keeps_the_partial_match(fake_atspi, monkeypatch):
    document, prose, decoy, home = _page()
    queries = []
    _collection(monkeypatch, document, [prose, decoy], queries)
    monkeypatch.setattr(_atspi, "_screen_size", lambda: (1280, 800))
    monkeypatch.setattr(_atspi, "scroll_to_edge", lambda node, edge: pytest.fail("prose is on screen"))
    assert _atspi.reveal_text_in_document(document, "Home")
    assert len(queries) == 1
