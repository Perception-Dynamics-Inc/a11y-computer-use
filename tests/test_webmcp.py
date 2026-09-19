"""WebMCP tools as ``w`` refs on the browser backend.

Hermetic tests drive `BrowserDriver.webmcp_tools` / `webmcp_call` and the
Runtime's ``webmcp`` tool through the scripted CDP transport, so the wire
shape (a `Runtime.evaluate` of the listing script, then of the call script)
is pinned without a browser. One live test launches its own headless Chrome
on a private port against ``tests/fixtures/webmcp_tools.html`` and reports
which registration path ran (native API, driver shim, or the fixture's own
stand-in).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from a11y_computer_use import safety, server
from a11y_computer_use.drivers import _cdp, browser
from a11y_computer_use.schema import ComputerUseError, ErrorCode, WebMcpOp, WebMcpVerb
from tests.test_browser import _driver_on, _fixture_responder

FIXTURE = Path(__file__).parent / "fixtures" / "webmcp_tools.html"

THREE_TOOLS = [
    {"name": "add_to_cart", "description": "Add a product to the cart",
     "inputSchema": {"type": "object", "properties": {"sku": {"type": "string", "enum": ["A1", "B2"]},
                                                      "quantity": {"type": "integer"}}},
     "kind": "script"},
    {"name": "checkout", "description": "Pay for the cart",
     "inputSchema": {"type": "object", "properties": {"method": {"type": "string", "enum": ["card"]}}},
     "kind": "script"},
    {"name": "leave_review", "description": "",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
     "kind": "form"},
]


def _webmcp_responder(listing, call_result=None, *, base=_fixture_responder):
    """A responder that answers the shim install, the listing script, and the
    call script; everything else goes to ``base`` (the a11y fixture page)."""
    def responder(method: str, params: dict):
        if method == "Page.addScriptToEvaluateOnNewDocument":
            return {"identifier": "1"}
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            if expr == browser._WEBMCP_SHIM_JS:
                return {"result": {"type": "undefined"}}
            if expr == browser._WEBMCP_LIST_JS:
                return {"result": {"type": "object", "value": listing}}
            if expr.lstrip().startswith("(async () => {\n  const name ="):
                value = call_result(expr) if callable(call_result) else call_result
                return {"result": {"type": "object", "value": value}}
        return base(method, params)
    return responder


def _runtime(tmp_path, d, tier=safety.Tier.CLICK):
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("TAB1", tier)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "a"), driver=d)


def _audit_rows(tmp_path) -> list[dict]:
    rows = []
    for path in sorted((tmp_path / "a").glob("*.jsonl")):
        rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return rows


# -- driver: listing ----------------------------------------------------------

def test_list_without_api_reports_absent_and_no_tools() -> None:
    d, t = _driver_on(_webmcp_responder({"api": "absent", "tools": []}))
    out = d.webmcp_tools()
    assert out == {"api": "absent", "tools": []}
    # the recorder went in once: new documents + the current one
    assert t.methods().count("Page.addScriptToEvaluateOnNewDocument") == 1
    shim_evals = [p for m, p in t.sent if m == "Runtime.evaluate"
                  and p.get("expression") == browser._WEBMCP_SHIM_JS]
    assert len(shim_evals) == 1
    d.webmcp_tools()
    assert t.methods().count("Page.addScriptToEvaluateOnNewDocument") == 1  # once per session


def test_list_with_api_but_no_tools() -> None:
    d, _ = _driver_on(_webmcp_responder({"api": "native", "tools": []}))
    assert d.webmcp_tools() == {"api": "native", "tools": []}


def test_list_three_tools_keeps_name_description_schema_and_kind() -> None:
    d, t = _driver_on(_webmcp_responder({"api": "shim", "tools": THREE_TOOLS}))
    out = d.webmcp_tools()
    assert out["api"] == "shim"
    assert [x["name"] for x in out["tools"]] == ["add_to_cart", "checkout", "leave_review"]
    assert out["tools"][0]["inputSchema"]["properties"]["sku"]["enum"] == ["A1", "B2"]
    assert out["tools"][2]["kind"] == "form" and out["tools"][0]["kind"] == "script"
    listing_call = next(p for m, p in t.sent if m == "Runtime.evaluate"
                        and p.get("expression") == browser._WEBMCP_LIST_JS)
    assert listing_call["awaitPromise"] is True and listing_call["returnByValue"] is True


def test_list_drops_malformed_entries_and_unknown_api_values() -> None:
    d, _ = _driver_on(_webmcp_responder({"api": "weird", "tools": [
        {"name": "", "description": "x"}, "junk", {"name": "ok", "inputSchema": "not a schema"},
    ]}))
    out = d.webmcp_tools()
    assert out["api"] == "absent"
    assert out["tools"] == [{"name": "ok", "description": "", "inputSchema": None, "kind": "script"}]


def test_shim_can_be_turned_off() -> None:
    d, t = _driver_on(_webmcp_responder({"api": "absent", "tools": []}))
    d._webmcp_shim = False
    d.webmcp_tools()
    assert "Page.addScriptToEvaluateOnNewDocument" not in t.methods()


def test_shim_install_tolerates_unsupported_new_document_hook() -> None:
    from tests.test_browser import _CDPError

    inner = _webmcp_responder({"api": "shim", "tools": []})

    def responder(method, params):
        if method == "Page.addScriptToEvaluateOnNewDocument":
            raise _CDPError("'Page.addScriptToEvaluateOnNewDocument' wasn't found")
        return inner(method, params)

    d, _ = _driver_on(responder)
    assert d.webmcp_tools()["api"] == "shim"  # the current-document install still ran


# -- driver: calling ----------------------------------------------------------

def test_call_success_returns_parsed_result_and_sends_name_and_arguments() -> None:
    result = {"ok": True, "kind": "script",
              "result": json.dumps({"content": [{"type": "text", "text": "added A1 x2"}]})}
    d, t = _driver_on(_webmcp_responder({"api": "shim", "tools": THREE_TOOLS}, result))
    out = d.webmcp_call("add_to_cart", {"sku": "A1", "quantity": 2})
    assert out == {"name": "add_to_cart", "kind": "script",
                   "result": {"content": [{"type": "text", "text": "added A1 x2"}]}}
    call = next(p for m, p in t.sent if m == "Runtime.evaluate"
                and 'const name = "' in p.get("expression", ""))
    assert '"add_to_cart"' in call["expression"] and '"quantity": 2' in call["expression"]
    assert call["awaitPromise"] is True and call["returnByValue"] is True


def test_call_error_is_a_structured_unsupported_error() -> None:
    d, _ = _driver_on(_webmcp_responder({}, {"ok": False, "error": "cart is empty", "kind": "script"}))
    with pytest.raises(ComputerUseError) as ei:
        d.webmcp_call("cart_total")
    assert ei.value.code is ErrorCode.UNSUPPORTED
    assert ei.value.detail == {"tool": "cart_total", "page_error": "cart is empty"}


def test_call_of_unregistered_tool_is_stale_ref() -> None:
    d, _ = _driver_on(_webmcp_responder({}, {"ok": False, "error": "not_found"}))
    with pytest.raises(ComputerUseError) as ei:
        d.webmcp_call("gone")
    assert ei.value.code is ErrorCode.STALE_REF
    assert ei.value.detail["reason"] == "tool_not_registered"


def test_call_page_exception_is_structured() -> None:
    inner = _webmcp_responder({}, {})

    def responder(method, params):
        if method == "Runtime.evaluate" and 'const name = "' in params.get("expression", ""):
            return {"result": {"type": "object"},
                    "exceptionDetails": {"text": "Uncaught", "exception": {"description": "SyntaxError: bad"}}}
        return inner(method, params)

    d, _ = _driver_on(responder)
    with pytest.raises(ComputerUseError) as ei:
        d.webmcp_call("x")
    assert ei.value.code is ErrorCode.UNSUPPORTED and "SyntaxError: bad" in ei.value.message


def test_call_result_is_size_capped() -> None:
    big = json.dumps({"content": [{"type": "text", "text": "x" * (browser._WEBMCP_RESULT_LIMIT + 100)}]})
    d, _ = _driver_on(_webmcp_responder({}, {"ok": True, "result": big}))
    out = d.webmcp_call("dump")
    assert out["truncated"] is True and len(out["result"]) == browser._WEBMCP_RESULT_LIMIT


def test_call_rejects_bad_inputs_before_touching_the_page() -> None:
    d, t = _driver_on(_webmcp_responder({}, {}))
    with pytest.raises(ValueError):
        d.webmcp_call("", {})
    with pytest.raises(ValueError):
        d.webmcp_call("x", ["not", "an", "object"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        d.webmcp_call("x", {"f": object()})
    assert "Runtime.evaluate" not in t.methods()


# -- safety: tiers, confirmation, redaction ------------------------------------

@pytest.mark.parametrize("name,schema,expected", [
    ("add_to_cart", {"type": "object", "properties": {"sku": {"type": "string", "enum": ["A1"]}}}, False),
    ("add_to_cart", {"type": "object", "properties": {"quantity": {"type": "integer"}}}, False),
    ("add_to_cart", {"type": "object", "properties": {}}, False),
    ("leave_review", {"type": "object", "properties": {"text": {"type": "string"}}}, True),  # free text
    ("checkout", {"type": "object", "properties": {}}, True),  # payment word
    ("checkoutNow", {"type": "object"}, True),  # camelCase split
    ("send-message", {"type": "object"}, True),
    ("search", {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}, True),
    ("pick", {"type": "object", "properties": {"n": {"anyOf": [{"type": "integer"}, {"type": "string"}]}}}, True),
    ("mystery", None, True),  # unknown schema: the conservative side
])
def test_webmcp_sensitive_rule(name, schema, expected) -> None:
    assert safety.webmcp_sensitive(name, schema) is expected


def test_required_tier_for_webmcp_ops() -> None:
    assert safety.required_tier(WebMcpOp(verb=WebMcpVerb.LIST, app="TAB1")) is safety.Tier.READ
    assert safety.required_tier(WebMcpOp(verb=WebMcpVerb.CALL, app="TAB1", name="a")) is safety.Tier.CLICK
    assert safety.required_tier(
        WebMcpOp(verb=WebMcpVerb.CALL, app="TAB1", name="a", sensitive=True)) is safety.Tier.FULL


def test_destructive_tool_names_need_confirmation() -> None:
    op = WebMcpOp(verb=WebMcpVerb.CALL, app="TAB1", name="delete_order", arguments="{}")
    prompt = safety.confirmation_prompt(op, "TAB1")
    assert prompt is not None and "delete_order" in prompt
    assert safety.confirmation_prompt(WebMcpOp(verb=WebMcpVerb.CALL, app="TAB1", name="add_to_cart"), "TAB1") is None


def test_audit_row_redacts_arguments(tmp_path) -> None:
    log = safety.AuditLog(tmp_path / "a")
    op = WebMcpOp(verb=WebMcpVerb.CALL, app="TAB1", name="leave_review",
                  arguments=json.dumps({"text": "my card is 4111"}), sensitive=True)
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("TAB1", safety.Tier.FULL)
    decision = safety.check_action(op, "TAB1", store=store)
    log.record_action(op, app="TAB1", decision=decision, result="ok")
    row = _audit_rows(tmp_path)[-1]
    assert row["action"] == "webmcpop" and row["params"]["name"] == "leave_review"
    assert row["params"]["arguments"] == safety.REDACTED
    assert "4111" not in json.dumps(row)


# -- runtime: the webmcp tool, w refs, and the snapshot block ------------------

def test_runtime_list_is_gated_read_and_renders_refs_and_tiers(tmp_path) -> None:
    d, _ = _driver_on(_webmcp_responder({"api": "shim", "tools": THREE_TOOLS}))
    rt = _runtime(tmp_path, d, tier=safety.Tier.READ)
    out = json.loads(rt.webmcp("TAB1", action="list"))
    assert out["api"] == "shim"
    assert [(x["ref"], x["name"], x["tier"]) for x in out["tools"]] == [
        ("w1", "add_to_cart", "click"), ("w2", "checkout", "full"), ("w3", "leave_review", "full"),
    ]
    row = _audit_rows(tmp_path)[-1]
    assert row["action"] == "webmcpop" and row["params"]["verb"] == "list" and row["result"] == "ok"


def test_runtime_call_resolves_w_ref_and_audits_with_redaction(tmp_path) -> None:
    result = {"ok": True, "kind": "script",
              "result": json.dumps({"content": [{"type": "text", "text": "added A1 x1"}]})}
    d, t = _driver_on(_webmcp_responder({"api": "shim", "tools": THREE_TOOLS}, result))
    rt = _runtime(tmp_path, d, tier=safety.Tier.CLICK)
    rt.webmcp("TAB1", action="list")
    out = json.loads(rt.webmcp("TAB1", action="call", name="w1", arguments={"sku": "A1"}))
    assert out["name"] == "add_to_cart" and out["result"]["content"][0]["text"] == "added A1 x1"
    call = next(p for m, p in t.sent if m == "Runtime.evaluate" and 'const name = "' in p.get("expression", ""))
    assert '"add_to_cart"' in call["expression"]
    row = _audit_rows(tmp_path)[-1]
    assert row["params"]["verb"] == "call" and row["params"]["name"] == "add_to_cart"
    assert row["params"]["arguments"] == safety.REDACTED and "A1" not in json.dumps(row["params"])
    assert row["result"] == "ok"


def test_runtime_call_by_name_and_json_string_arguments(tmp_path) -> None:
    result = {"ok": True, "kind": "script", "result": json.dumps({"content": []})}
    d, t = _driver_on(_webmcp_responder({"api": "shim", "tools": THREE_TOOLS}, result))
    rt = _runtime(tmp_path, d, tier=safety.Tier.CLICK)
    rt.webmcp("TAB1", action="list")
    rt.webmcp("TAB1", action="call", name="add_to_cart", arguments='{"sku": "B2"}')
    call = next(p for m, p in t.sent if m == "Runtime.evaluate" and 'const name = "' in p.get("expression", ""))
    assert '"sku": "B2"' in call["expression"]


def test_runtime_sensitive_call_needs_full_tier(tmp_path) -> None:
    d, _ = _driver_on(_webmcp_responder({"api": "shim", "tools": THREE_TOOLS}, {"ok": True, "result": "null"}))
    rt = _runtime(tmp_path, d, tier=safety.Tier.CLICK)
    rt.webmcp("TAB1", action="list")
    with pytest.raises(server.ActionRefused):
        rt.webmcp("TAB1", action="call", name="w2")  # checkout: payment word, FULL
    row = _audit_rows(tmp_path)[-1]
    assert row["params"]["name"] == "checkout" and row["result"] != "ok"
    rt.store.set_tier("TAB1", safety.Tier.FULL)
    rt.webmcp("TAB1", action="call", name="w2")  # now allowed


def test_runtime_call_before_any_listing_is_stale_ref(tmp_path) -> None:
    d, t = _driver_on(_webmcp_responder({"api": "shim", "tools": THREE_TOOLS}))
    rt = _runtime(tmp_path, d)
    with pytest.raises(ComputerUseError) as ei:
        rt.webmcp("TAB1", action="call", name="w1")
    assert ei.value.code is ErrorCode.STALE_REF and ei.value.detail["reason"] == "no_listing"
    rt.webmcp("TAB1", action="list")
    with pytest.raises(ComputerUseError) as ei:
        rt.webmcp("TAB1", action="call", name="w9")
    assert ei.value.detail["reason"] == "unknown_ref"
    with pytest.raises(ComputerUseError) as ei:
        rt.webmcp("TAB1", action="call", name="not_a_tool")
    assert ei.value.detail["reason"] == "unknown_name" and "add_to_cart" in ei.value.detail["candidates"]
    assert not any('const name = "' in p.get("expression", "") for m, p in t.sent)  # nothing ran


def test_runtime_call_needs_a_name_and_a_known_action(tmp_path) -> None:
    d, _ = _driver_on(_webmcp_responder({"api": "shim", "tools": THREE_TOOLS}))
    rt = _runtime(tmp_path, d)
    with pytest.raises(ValueError):
        rt.webmcp("TAB1", action="call")
    with pytest.raises(ValueError):
        rt.webmcp("TAB1", action="poke")


def test_runtime_webmcp_unsupported_off_the_browser(tmp_path) -> None:
    d, _ = _driver_on(_webmcp_responder({"api": "shim", "tools": []}))
    rt = _runtime(tmp_path, d)
    rt.driver = type("NoWebMcp", (), {"name": "macos"})()
    with pytest.raises(ComputerUseError) as ei:
        rt.webmcp("TAB1")
    assert ei.value.code is ErrorCode.UNSUPPORTED


def test_snapshot_appends_webmcp_block_and_makes_w_refs_current(tmp_path) -> None:
    d, _ = _driver_on(_webmcp_responder({"api": "native", "tools": THREE_TOOLS},
                                        {"ok": True, "result": json.dumps({"content": []})}))
    rt = _runtime(tmp_path, d, tier=safety.Tier.CLICK)
    out = rt.desktop_snapshot("TAB1", scope="window")
    assert "Save" in out  # the a11y tree is still there
    block = out[out.index("webmcp tools:"):]
    assert block.splitlines() == [
        "webmcp tools:",
        "  w1 add_to_cart (Add a product to the cart)",
        "  w2 checkout (Pay for the cart)",
        "  w3 leave_review",
    ]
    # the snapshot's refs are current for a call, no separate list needed
    out = json.loads(rt.webmcp("TAB1", action="call", name="w1", arguments={"sku": "A1"}))
    assert out["name"] == "add_to_cart"


def test_snapshot_has_no_block_when_the_page_offers_nothing(tmp_path) -> None:
    d, _ = _driver_on(_webmcp_responder({"api": "absent", "tools": []}))
    rt = _runtime(tmp_path, d, tier=safety.Tier.READ)
    out = rt.desktop_snapshot("TAB1", scope="window")
    assert "webmcp tools:" not in out
    assert rt._webmcp_tools == [] and rt._webmcp_app == "TAB1"


def test_snapshot_survives_a_listing_failure(tmp_path) -> None:
    from tests.test_browser import _CDPError

    def responder(method, params):
        if method == "Runtime.evaluate" and params.get("expression") == browser._WEBMCP_LIST_JS:
            raise _CDPError("Execution context was destroyed")
        return _webmcp_responder({}, {})(method, params)

    d, _ = _driver_on(responder)
    rt = _runtime(tmp_path, d, tier=safety.Tier.READ)
    out = rt.desktop_snapshot("TAB1", scope="window")
    assert "Save" in out and "webmcp tools:" not in out


def test_mcp_surface_registers_webmcp_only_with_the_feed(tmp_path) -> None:
    d, _ = _driver_on(_webmcp_responder({"api": "shim", "tools": []}))
    rt = _runtime(tmp_path, d)
    names = {s["name"] for s in server.tool_specs(rt)}
    assert "webmcp" in names
    spec = next(s for s in server.tool_specs(rt) if s["name"] == "webmcp")
    assert set(spec["input_schema"]["properties"]) == {"app", "action", "name", "arguments"}
    assert "Prefer a matching tool" in spec["description"]
    assert "webmcp" in server._INSTRUCTIONS

    class NoFeed:
        name = "macos"
    rt.driver = NoFeed()
    assert "webmcp" not in {s["name"] for s in server.tool_specs(rt)}


def test_call_tool_dispatches_webmcp(tmp_path) -> None:
    d, _ = _driver_on(_webmcp_responder({"api": "shim", "tools": THREE_TOOLS}))
    rt = _runtime(tmp_path, d)
    out = json.loads(rt.call_tool("webmcp", {"app": "TAB1", "action": "list"}))
    assert out["tools"][0]["ref"] == "w1"


# -- live: own headless Chrome against the fixture -----------------------------

def _chrome_binary() -> str | None:
    env = os.environ.get("A11Y_COMPUTER_USE_CHROME")
    if env and Path(env).exists():
        return env
    for cand in ("google-chrome", "chromium-browser", "chromium", "google-chrome-stable"):
        found = shutil.which(cand)
        if found:
            return found
    for cand in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Chromium.app/Contents/MacOS/Chromium"):
        if Path(cand).exists():
            return cand
    return None


def _free_port() -> int | None:
    for port in range(9951, 10000):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return None


_LIVE_REASON = ("live WebMCP test needs A11Y_COMPUTER_USE_CDP_ENDPOINT set (opt-in) and a Chrome "
                "binary (A11Y_COMPUTER_USE_CHROME or google-chrome/chromium on PATH)")


@pytest.mark.skipif(not os.environ.get("A11Y_COMPUTER_USE_CDP_ENDPOINT") or _chrome_binary() is None,
                    reason=_LIVE_REASON)
def test_live_webmcp_tools_on_own_headless_chrome() -> None:
    port = _free_port()
    assert port is not None, "no free port in 9951-9999"
    profile = tempfile.mkdtemp(prefix="a11y-webmcp-")
    proc = subprocess.Popen(
        [_chrome_binary(), "--headless=new", f"--remote-debugging-port={port}",
         "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", "--disable-gpu",
         "--disable-dev-shm-usage", "--no-sandbox", "--window-size=1280,800", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    endpoint = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if _cdp.page_targets(endpoint):
                    break
            except Exception:
                pass
            time.sleep(0.25)
        else:
            pytest.skip("own headless Chrome did not come up in 30s")

        d = browser.BrowserDriver(endpoint=endpoint)
        first = d.webmcp_tools()  # installs the recorder before the fixture loads
        d.navigate(FIXTURE.resolve().as_uri())
        listing = d.webmcp_tools()
        path = d._connect().call("Runtime.evaluate", {
            "expression": "document.body.dataset.webmcpPath", "returnByValue": True,
        })["result"]["value"]
        if not listing["tools"]:
            pytest.skip(f"the fixture registered no tool the driver can see (path={path!r})")
        names = [t["name"] for t in listing["tools"]]
        assert names == ["add_to_cart", "cart_total"]
        assert listing["tools"][0]["inputSchema"]["properties"]["sku"]["enum"] == ["A1", "B2", "C3"]
        assert path in ("native", "driver-shim")
        assert listing["api"] == ("native" if path == "native" else "shim")

        out = d.webmcp_call("add_to_cart", {"sku": "B2", "quantity": 2})
        assert out["result"]["content"][0]["text"] == "added B2 x2; cart has 1 lines"
        status = d._connect().call("Runtime.evaluate", {
            "expression": "document.getElementById('status').textContent", "returnByValue": True,
        })["result"]["value"]
        assert status == "added B2"  # the page changed: the tool really ran in it
        assert d.webmcp_call("cart_total")["result"]["content"][0]["text"] == "1"

        d.navigate("about:blank")
        d.navigate(FIXTURE.resolve().as_uri())  # fresh document: cart empty again
        with pytest.raises(ComputerUseError) as ei:
            d.webmcp_call("cart_total")
        assert ei.value.code is ErrorCode.UNSUPPORTED and "cart is empty" in ei.value.message
        with pytest.raises(ComputerUseError) as ei:
            d.webmcp_call("nope")
        assert ei.value.code is ErrorCode.STALE_REF
        print(f"\nwebmcp live path: {path} (first listing api={first['api']})")
        d.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)
