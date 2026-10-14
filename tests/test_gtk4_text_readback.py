"""GTK 4 Text rejects -1 as the end offset; synthetic AT-SPI regression."""

import pytest

from a11y_computer_use.drivers import _atspi, _linux_input


class _Text:
    @staticmethod
    def get_character_count(acc):
        return len(acc.text)

    @staticmethod
    def get_text(acc, start, end):
        acc.reads.append((start, end))
        return "" if end == -1 else acc.text[start:end]


class _Api:
    Text = _Text


class _Entry:
    def __init__(self, role, text):
        self.role = role
        self.text = text
        self.reads = []
        self.writes = []

    def get_role_name(self):
        return self.role

    def get_editable_text_iface(self):
        return self

    def set_text_contents(self, text):
        self.writes.append(text)
        self.text = text
        return True


@pytest.mark.parametrize("role", ["entry", "text"])
@pytest.mark.parametrize("original", ["", "old value"])
def test_gtk4_replace_verifies_one_write_without_focus_or_keys(monkeypatch, role, original):
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Api)
    field = _Entry(role, original)

    def unexpected(*args, **kwargs):
        pytest.fail("a verified EditableText replace must not focus or type")

    monkeypatch.setattr(_atspi, "grab_focus", unexpected)
    monkeypatch.setattr(_linux_input, "type_string", unexpected)
    assert _atspi.set_text(field, "SRCH1") is True
    assert field.text == "SRCH1"
    assert field.writes == ["SRCH1"]
    assert _atspi._value_text(field, "AXTextField", role) is None
    assert _atspi._full_text(field) == ""
    assert (0, 5) in field.reads


def test_gtk4_readback_tracks_changed_text_and_empty_field(monkeypatch):
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Api)
    field = _Entry("entry", "TYPE1")
    assert _atspi._full_text(field) == ""
    assert _atspi._verification_matches(field, "TYPE1")
    field.text = "TYPE2"
    assert _atspi._full_text(field) == ""
    assert _atspi._verification_matches(field, "TYPE2")
    assert not _atspi._confirm_text(field, "TYPE1")
    field.text = ""
    field.reads.clear()
    assert _atspi._full_text(field) == ""
    assert field.reads == [(0, -1)]


def test_nonempty_sentinel_read_wins_over_bounded_write_echo(monkeypatch):
    class EchoText(_Text):
        @staticmethod
        def get_text(acc, start, end):
            acc.reads.append((start, end))
            return "actual old text" if end == -1 else "requested"

    class Api:
        Text = EchoText

    monkeypatch.setattr(_atspi, "_atspi", lambda: Api)
    field = _Entry("entry", "requested")
    assert _atspi._full_text(field) == "actual old text"
    assert _atspi._value_text(field, "AXTextField", "entry") == "actual old text"
    assert field.reads == [(0, -1), (0, -1)]


@pytest.mark.parametrize("signal", [
    "visibility-method", "visibility-attribute", "password-role",
    "password-state", "protected-state", "password-attribute", "protected-attribute",
])
@pytest.mark.parametrize("sentinel_empty", [True, False])
def test_masking_signals_block_every_readback(monkeypatch, signal, sentinel_empty):
    from types import SimpleNamespace
    from a11y_computer_use import observe
    from a11y_computer_use.schema import Scope
    from tests.test_linux_synthetic import _geometry

    class Text(_Text):
        @staticmethod
        def get_text(acc, start, end):
            acc.reads.append((start, end))
            return "" if sentinel_empty and end == -1 else acc.text[start:None if end == -1 else end]

    Api = type("Api", (), {"Text": Text})

    monkeypatch.setattr(_atspi, "_atspi", lambda: Api)
    field = _Entry("text", "hunter2-578")
    field.get_name = lambda: "PIN"
    field.get_attributes = lambda: {}
    field.get_state_set = lambda: SimpleNamespace(names={"EDITABLE", "VISIBLE", "SHOWING"})
    if signal == "visibility-method":
        field.get_visibility = lambda: False
    elif signal == "visibility-attribute":
        field.get_attributes = lambda: {"visibility": "false"}
    elif signal == "password-role":
        field.role = "password text"
    elif signal.endswith("-state"):
        field.get_state_set = lambda: SimpleNamespace(names={signal.split("-")[0].upper(), "EDITABLE"})
    else:
        field.get_attributes = lambda: {signal.split("-")[0]: "true"}

    assert _atspi.is_secure(field)
    assert _atspi._value_text(field, "AXTextArea", field.role) is None
    assert _atspi._full_text(field) is None
    assert _atspi._readable_text(field) is None
    assert _atspi._direct_text(field) == ""
    assert not _atspi._verification_matches(field, field.text)
    assert not _atspi._confirm_text(field, field.text)
    assert not _atspi.set_text(field, "replacement")
    assert not field.writes
    raw = _atspi.ATSPIAccessor().read(field)
    assert raw.role == "AXSecureTextField"
    assert raw.value is None
    snap = observe.build_snapshot(field, _atspi.ATSPIAccessor(), scope=Scope.WINDOW,
                                  app="test", pid=1, geometry=_geometry())
    assert not observe.find_elements(snap, text="hunter2")
    assert all(el.value is None for el in snap.elements)
    assert field.reads == []


def test_unmarked_gtk4_masked_entry_never_exposes_bounded_text(monkeypatch):
    from a11y_computer_use import observe
    from a11y_computer_use.schema import Scope
    from tests.test_linux_synthetic import _geometry

    monkeypatch.setattr(_atspi, "_atspi", lambda: _Api)
    field = _Entry("text", "hunter2-578")
    field.get_name = lambda: "PIN"
    assert not _atspi.is_secure(field)
    assert _atspi._full_text(field) == ""
    assert _atspi._readable_text(field) == ""
    assert _atspi._value_text(field, "AXTextArea", "text") is None
    snap = observe.build_snapshot(field, _atspi.ATSPIAccessor(), scope=Scope.WINDOW,
                                  app="test", pid=1, geometry=_geometry())
    assert not observe.find_elements(snap, text="hunter2")
    assert all(el.value is None for el in snap.elements)
    assert all(end == -1 for _, end in field.reads)
    assert _atspi._verification_matches(field, field.text)
    assert _atspi._full_text(field) == ""


@pytest.mark.parametrize("signal", ["attribute", "method"])
def test_visible_gtk4_entry_reads_back_in_snapshot_and_find(monkeypatch, signal):
    from a11y_computer_use import observe
    from a11y_computer_use.schema import Scope
    from tests.test_linux_synthetic import _geometry

    monkeypatch.setattr(_atspi, "_atspi", lambda: _Api)
    field = _Entry("entry", "Visible entry 584")
    field.get_name = lambda: "Entry"
    if signal == "attribute":
        field.get_attributes = lambda: {"visibility": "true"}
    else:
        field.get_visibility = lambda: True
    assert _atspi._full_text(field) == field.text
    assert _atspi._value_text(field, "AXTextField", "entry") == field.text
    snap = observe.build_snapshot(field, _atspi.ATSPIAccessor(), scope=Scope.WINDOW,
                                  app="test", pid=1, geometry=_geometry())
    assert observe.find_elements(snap, text=field.text)
    field.text = "Changed entry 584"
    assert _atspi._full_text(field) == field.text


@pytest.mark.parametrize("sentinel_empty", [False, True])
@pytest.mark.parametrize("metadata", [False, True])
@pytest.mark.parametrize("text", ["●●●", "•••", "***", "⬤⬤", "\u200b\u200b"])
def test_bullets_only_entry_never_publishes_value(monkeypatch, metadata, text, sentinel_empty):
    class Text(_Text):
        @staticmethod
        def get_text(acc, start, end):
            acc.reads.append((start, end))
            return "" if sentinel_empty and end == -1 else acc.text[start:None if end == -1 else end]

    monkeypatch.setattr(_atspi, "_atspi", lambda: type("Api", (), {"Text": Text}))
    field = _Entry("entry", text)
    if metadata:
        field.get_attributes = lambda: {"visibility": "true"}
    assert _atspi._full_text(field) == ""
    assert _atspi._value_text(field, "AXTextField", "entry") is None
    if not metadata:
        assert all(end == -1 for _, end in field.reads)


@pytest.mark.parametrize("signal", ["invisible", "false-attribute", "false-method"])
def test_visibility_conflicts_fail_closed(monkeypatch, signal):
    from types import SimpleNamespace

    monkeypatch.setattr(_atspi, "_atspi", lambda: _Api)
    field = _Entry("entry", "secret 584")
    field.get_attributes = lambda: {"visibility": "true"}
    field.get_visibility = lambda: True
    if signal == "invisible":
        field.get_state_set = lambda: SimpleNamespace(names={"INVISIBLE"})
    elif signal == "false-attribute":
        field.get_attributes = lambda: {"visibility": "false"}
    else:
        field.get_visibility = lambda: False
    assert _atspi._full_text(field) is None
    assert field.reads == []


@pytest.mark.parametrize("char", ["x", "120"])
def test_toolkit_invisible_character_is_not_published(monkeypatch, char):
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Api)
    field = _Entry("entry", "xxxx")
    field.get_attributes = lambda: {"visibility": "true", "invisible-char": char}
    assert _atspi._full_text(field) == ""
    assert _atspi._value_text(field, "AXTextField", "entry") is None


@pytest.mark.parametrize("role", ["entry", "text", "table cell"])
@pytest.mark.parametrize("sentinel_empty", [True, False])
def test_text_read_fetches_security_metadata_once(monkeypatch, role, sentinel_empty):
    calls = {"attributes": 0, "visibility": 0, "count": 0}

    class Text(_Text):
        @staticmethod
        def get_character_count(acc):
            calls["count"] += 1
            return len(acc.text)

        @staticmethod
        def get_text(acc, start, end):
            acc.reads.append((start, end))
            return "" if sentinel_empty and end == -1 else acc.text

    def attributes():
        calls["attributes"] += 1
        return {"visibility": "true"}

    def visibility():
        calls["visibility"] += 1
        return True

    monkeypatch.setattr(_atspi, "_atspi", lambda: type("Api", (), {"Text": Text}))
    field = _Entry(role, "visible")
    field.get_attributes = attributes
    field.get_visibility = visibility
    entry = role in {"entry", "text"}
    assert _atspi._read_full_text(field) == ("visible" if entry or not sentinel_empty else "")
    assert calls == {"attributes": 1, "visibility": int(entry),
                     "count": int(entry and sentinel_empty)}
    assert field.reads == ([(0, -1), (0, 7)] if entry and sentinel_empty else [(0, -1)])


def test_known_empty_text_does_not_retry_bounded_read(monkeypatch):
    class Text(_Text):
        @staticmethod
        def get_character_count(acc):
            pytest.fail("the caller already supplied the character count")

    monkeypatch.setattr(_atspi, "_atspi", lambda: type("Api", (), {"Text": Text}))
    field = _Entry("entry", "")
    field.get_attributes = lambda: {"visibility": "true"}
    assert _atspi._read_full_text(field, count=0) == ""
    assert field.reads == [(0, -1)]


@pytest.mark.parametrize("protected", [None, "PASSWORD", "PROTECTED", "INVISIBLE"])
@pytest.mark.parametrize("native", [False, True])
def test_text_security_reads_one_fresh_state_set_per_probe(monkeypatch, protected, native):
    """All masking flags share one read, but a later probe sees changed state."""
    from types import SimpleNamespace

    field = _Entry("entry", "private")
    calls = []
    state = {"flag": protected}

    members = SimpleNamespace(PASSWORD=1, PROTECTED=2, INVISIBLE=3)

    def get_states():
        calls.append(state["flag"])
        if native:
            member = getattr(members, state["flag"], None) if state["flag"] else None
            return SimpleNamespace(contains=lambda candidate: member is not None and candidate == member)
        return SimpleNamespace(names={state["flag"]} if state["flag"] else set())

    field.get_state_set = get_states
    monkeypatch.setattr(_atspi, "_atspi", lambda: SimpleNamespace(StateType=members))
    with _atspi.read_cache():
        assert _atspi.is_secure(field) is (protected is not None)
        assert len(calls) == 1
        state["flag"] = "PROTECTED" if protected is None else None
        assert _atspi.is_secure(field) is (protected is None)
        assert len(calls) == 2


def test_text_security_does_not_retry_a_missing_state_set(monkeypatch):
    """A missing reply is shared within this probe and retried by the next."""
    field = _Entry("entry", "visible")
    calls = []
    field.get_state_set = lambda: calls.append("states")
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Api)
    assert _atspi.is_secure(field) is False
    assert calls == ["states"]
    assert _atspi.is_secure(field) is False
    assert calls == ["states", "states"]


@pytest.mark.parametrize("kind", ["plain", "masked", "password", "unreadable", "custom-mask"])
def test_real_gtk4_layout_signal(monkeypatch, kind):
    """Mirror GTK 4.18 probe: GetText leaks; layout strings show bullets."""
    from types import SimpleNamespace
    from a11y_computer_use import observe
    from a11y_computer_use.schema import Scope
    from tests.test_linux_synthetic import _geometry

    calls = {"states": 0, "attrs": 0, "layout": 0}
    field = _Entry("password text" if kind == "password" else "text", "ProbeSecret584")
    field.get_name = lambda: "Field"

    def states():
        calls["states"] += 1
        return SimpleNamespace(names={"EDITABLE", "FOCUSABLE", "SENSITIVE", "SHOWING", "VISIBLE"})

    def attrs():
        calls["attrs"] += 1
        return {"toolkit": "GTK"}

    class Text(_Text):
        @staticmethod
        def get_string_at_offset(acc, offset, granularity):
            calls["layout"] += 1
            assert offset == 0 and granularity == "LINE"
            return SimpleNamespace(content=(field.text if kind == "plain" else
                                           "x" * len(field.text) if kind == "custom-mask" else
                                           "•" * len(field.text) if kind != "unreadable" else None))

    field.get_state_set = states
    field.get_attributes = attrs
    monkeypatch.setattr(_atspi, "_atspi", lambda: SimpleNamespace(
        Text=Text, TextGranularity=SimpleNamespace(LINE="LINE")))
    assert _atspi.is_secure(field) is (kind != "plain")
    assert calls == ({"states": 0, "attrs": 0, "layout": 0} if kind == "password"
                     else {"states": 1, "attrs": 1, "layout": 1})
    raw = _atspi.ATSPIAccessor().read(field)
    assert (raw.role == "AXSecureTextField") is (kind != "plain")
    snap = observe.build_snapshot(field, _atspi.ATSPIAccessor(), scope=Scope.WINDOW,
                                  app="test", pid=1, geometry=_geometry())
    assert bool(observe.find_elements(snap, text=field.text)) is (kind == "plain")
    if kind != "plain":
        assert field.text not in repr(snap)
        assert not _atspi.set_text(field, "ReplacementSecret584")
        assert not _atspi._verification_matches(field, field.text)
        assert not field.writes
        if kind == "custom-mask":
            assert field.reads and all(end > 0 for _, end in field.reads)
        else:
            assert not field.reads


@pytest.mark.parametrize("operation", ["set", "type"])
def test_layout_masked_field_runtime_refuses_without_echo(monkeypatch, operation):
    from types import SimpleNamespace
    from a11y_computer_use import observe, server
    from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope
    from tests.test_linux_synthetic import _geometry

    field = _Entry("text", "OriginalSecret584")
    field.get_attributes = lambda: {"toolkit": "GTK"}
    field.get_state_set = lambda: SimpleNamespace(names={"EDITABLE", "SENSITIVE", "VISIBLE", "SHOWING"})

    class Text(_Text):
        @staticmethod
        def get_string_at_offset(acc, offset, granularity):
            return SimpleNamespace(content="•" * len(acc.text))

    monkeypatch.setattr(_atspi, "_atspi", lambda: SimpleNamespace(
        Text=Text, TextGranularity=SimpleNamespace(LINE="LINE")))
    snap = observe.build_snapshot(field, _atspi.ATSPIAccessor(), scope=Scope.WINDOW,
                                  app="test", pid=1, geometry=_geometry())
    el = snap.elements[0]
    runtime = server.Runtime.__new__(server.Runtime)
    runtime._resolve = lambda *args: (snap, el)
    runtime._capture = lambda *args: None
    runtime._cover_owner = lambda *args: None
    runtime._type_target_error = lambda *args: None
    audit = []
    runtime._record_failure = lambda *args, **kwargs: audit.append(kwargs)
    with pytest.raises(ComputerUseError) as caught:
        if operation == "set":
            runtime.set_value(el.ref, "ReplacementSecret584")
        else:
            runtime.type_text("ReplacementSecret584", ref=el.ref)
    assert caught.value.code is ErrorCode.SECURE_FIELD
    for secret in (field.text, "ReplacementSecret584"):
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value.detail)
        assert secret not in repr(audit)
    assert audit[0]["result"] == "secure_field"
    assert not field.reads and not field.writes


def test_gtk_layout_masking_rechecked_and_multiline_not_probed(monkeypatch):
    from types import SimpleNamespace

    field = _Entry("text", "Visible584")
    field.get_attributes = lambda: {"toolkit": "GTK"}
    state = {"multiline": False, "masked": False}
    field.get_state_set = lambda: SimpleNamespace(names={"MULTI_LINE"} if state["multiline"] else set())
    calls = []

    class Text(_Text):
        @staticmethod
        def get_string_at_offset(acc, offset, granularity):
            calls.append(state["masked"])
            return SimpleNamespace(content="•" * len(acc.text) if state["masked"] else acc.text)

    monkeypatch.setattr(_atspi, "_atspi", lambda: SimpleNamespace(
        Text=Text, TextGranularity=SimpleNamespace(LINE="LINE")))
    with _atspi.read_cache():
        assert not _atspi.is_secure(field)
        state["masked"] = True
        assert _atspi.is_secure(field)
        state["multiline"] = True
        assert not _atspi.is_secure(field)
    assert calls == [False, True]


def test_gtk3_empty_entry_keeps_existing_write_path(monkeypatch):
    field = _Entry("text", "")
    field.get_attributes = lambda: {"toolkit": "GTK"}
    field.get_toolkit_version = lambda: "3.24.49"
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Api)
    assert not _atspi.is_secure(field)
    assert _atspi.set_text(field, "Visible584")
    assert field.writes == ["Visible584"]


@pytest.mark.parametrize("masked", [False, True])
@pytest.mark.parametrize("operation", ["set", "type"])
@pytest.mark.parametrize("sticks", [False, True])
def test_empty_gtk4_unknown_write_is_private(monkeypatch, masked, operation, sticks):
    from types import SimpleNamespace
    from a11y_computer_use import observe, server
    from a11y_computer_use.schema import ComputerUseError, Scope
    from tests.test_linux_synthetic import _geometry

    field = _Entry("text", "")
    field.get_attributes = lambda: {"toolkit": "GTK"}
    field.get_name = lambda: "Field584"
    field.get_state_set = lambda: SimpleNamespace(names={"EDITABLE", "SENSITIVE"})
    writes = []

    def write(text):
        writes.append(text)
        if sticks:
            field.text = text
        return True

    field.set_text_contents = write
    field.insert_text = lambda offset, text, length: write(text)

    class Text(_Text):
        @staticmethod
        def get_string_at_offset(acc, offset, granularity):
            return SimpleNamespace(content="•" * len(acc.text) if masked else acc.text)

    monkeypatch.setattr(_atspi, "_atspi", lambda: SimpleNamespace(
        Text=Text, TextGranularity=SimpleNamespace(LINE="LINE")))
    assert not _atspi.is_secure(field)
    assert _atspi.text_visibility_unknown(field)
    snap = observe.build_snapshot(field, _atspi.ATSPIAccessor(), scope=Scope.WINDOW,
                                  app="test", pid=1, geometry=_geometry())
    el = snap.elements[0]
    assert not el.secure and el.value is None
    runtime = server.Runtime.__new__(server.Runtime)
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver.__new__(LinuxDriver)
    driver._run = lambda call: call()
    driver.set_value = lambda el, text: _atspi.set_text(field, text)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *args: field)
    runtime.driver = driver
    runtime._resolve = lambda *args: (snap, el)
    runtime._capture = lambda *args: None
    runtime._cover_owner = lambda *args: None
    runtime._type_target_error = lambda *args: None
    runtime._reject_domain = lambda *args, **kwargs: None
    runtime._refuse_disabled = lambda *args, **kwargs: None
    runtime._guard_user = lambda *args: None
    runtime._dismiss_open_menu = lambda *args: ""
    runtime._refuse_vanished_target = lambda *args: None
    runtime._deliver_type_at_ref = lambda el, text: _atspi.insert_text(field, text)
    audit = []

    def gate(action, app, execute, **kwargs):
        audit.append(kwargs)
        return execute()

    runtime._run_gated = gate
    secret = "Private584"
    call = (lambda: runtime.set_value(el.ref, secret)) if operation == "set" else (
        lambda: runtime.type_text(secret, ref=el.ref))
    if sticks:
        result = call()
        assert result.outcome == "confirmed"
        assert secret not in str(result) + repr(result.as_dict())
        assert _atspi.is_secure(field) is masked
        assert _atspi._full_text(field) == (None if masked else secret)
    else:
        with pytest.raises(ComputerUseError) as caught:
            call()
        assert secret not in str(caught.value) + repr(caught.value.detail)
    assert len(writes) == 1
    assert audit[0]["secure"] is True


@pytest.mark.parametrize("count", [None, False, 1])
def test_empty_layout_requires_real_zero_count_to_allow_writes(monkeypatch, count):
    from types import SimpleNamespace

    class Text(_Text):
        @staticmethod
        def get_character_count(acc):
            return count

        @staticmethod
        def get_string_at_offset(acc, offset, granularity):
            return SimpleNamespace(content="")

    field = _Entry("text", "")
    field.get_attributes = lambda: {"toolkit": "GTK"}
    monkeypatch.setattr(_atspi, "_atspi", lambda: SimpleNamespace(
        Text=Text, TextGranularity=SimpleNamespace(LINE="LINE")))
    assert _atspi.is_secure(field)
    assert not _atspi.text_visibility_unknown(field)
    assert not _atspi.set_text(field, "Private584")
    assert not field.writes and not field.reads


@pytest.mark.parametrize("success", [False, True])
def test_implicit_unknown_type_redacts_audit_and_receipt(monkeypatch, success):
    from types import SimpleNamespace
    from a11y_computer_use import server
    from a11y_computer_use.schema import ComputerUseError, TypeText

    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = SimpleNamespace(name="linux")
    runtime._domain_applies = lambda action: False
    runtime._reject_terminal = lambda *args, **kwargs: None
    runtime._require_permission = lambda *args, **kwargs: None
    recorded = []
    runtime.audit = SimpleNamespace(record_action=lambda *args, **kwargs: recorded.append(kwargs))
    monkeypatch.setattr(server, "CONFIRMATION_GATE", False)

    def execute():
        if success:
            return _atspi._PrivateTextCount(10)
        raise _atspi._text_mismatch("text_mismatch", "private write not verified", private_text=True)

    if success:
        runtime._run_gated(TypeText(text="Private584"), "test", execute)
        result = runtime._conclude("must be redacted Private584", tool="type")
        assert result.outcome == "confirmed"
        assert "Private584" not in str(result) + repr(result.as_dict())
    else:
        with pytest.raises(ComputerUseError):
            runtime._run_gated(TypeText(text="Private584"), "test", execute)
    assert recorded[0]["secure"] is True
