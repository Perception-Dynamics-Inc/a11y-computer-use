# WebMCP tools as w refs

WebMCP is a proposal that lets a web page hand an agent a set of typed
tools instead of a pile of buttons. A page calls
`navigator.modelContext.registerTool({name, description, inputSchema, execute})`
and an agent that knows about the tool can call it with a JSON object; the
page validates the input and runs its own code. The same page can declare a
tool without script: a `<form toolname="..." tooldescription="...">` whose
inputs carry `toolparamdescription` attributes.

On the browser backend, a11y-computer-use reads those tools and exposes them
next to the accessibility tree. A snapshot of a page that registered tools
ends with a block like this:

```text
webmcp tools:
  w1 add_to_cart (Add a product to the cart by SKU)
  w2 cart_total (Count the lines in the cart)
```

The agent then calls `webmcp(app="TAB1", action="call", name="w1",
arguments={"sku": "B2", "quantity": 2})` and gets the tool's result back,
instead of finding the SKU field, typing into it, finding the quantity
field, and clicking the button. The `e` refs stay valid: `w` refs and `e`
refs live side by side in the same snapshot, and an agent picks whichever
fits the step (a tool for a well-defined operation, an element for
everything the page did not wrap in a tool). Refs from a snapshot or a
`webmcp(action="list")` call are the current epoch; a call with a `w` ref
from an older listing, or with a name the current listing does not hold,
fails with `stale_ref`, the same contract as `e` refs.

## Standard and browser status

As of September 2026 WebMCP is a draft of the W3C Web Machine Learning
Community Group (https://webmachinelearning.github.io/webmcp/, repo
webmachinelearning/webmcp). It is not a W3C standard. Chrome ships it in
Canary from 146.0.7672.0 behind the `chrome://flags/#enable-webmcp-testing`
flag; there is no polyfill from the working group, and a page is expected
to guard with `if ('modelContext' in navigator)`. The API surface used here
is `registerTool` and `unregisterTool`; `provideContext` and `clearContext`
were removed from the draft in March 2026 but the recorder still honours
them for pages written against the older shape. A tool's `execute` returns
`{content: [{type: "text", text}]}`, the MCP tool-result shape.

The draft gives a page no way to enumerate what it registered: there is no
`listTools()` in Chrome 146, and the explainer says discovery has no
built-in mechanism yet. That fact shapes the implementation below.

## What the driver does

`BrowserDriver.webmcp_tools()` returns
`{"api": native | shim | absent, "tools": [{name, description, inputSchema, kind}]}`
and `BrowserDriver.webmcp_call(name, arguments)` returns
`{"name", "kind", "result", "truncated"?}`. Both run over the same CDP
session as snapshots and clicks, through `Runtime.evaluate`.

Because the page cannot be asked for its registrations, the driver installs
a small recorder into the page the first time either method runs:

- `Page.addScriptToEvaluateOnNewDocument` installs it into every document
  the tab loads from then on.
- One `Runtime.evaluate` installs it into the document already open.

The recorder wraps `registerTool` and `unregisterTool` when the browser has
`navigator.modelContext` (the `native` case) and polyfills the object when
it does not (the `shim` case), so a page guarded with
`'modelContext' in navigator` registers its tools even in a browser that
has not shipped the API. Registrations land in a hidden
`window.__a11y_webmcp` map that only the driver reads. A listing also
collects declarative `form[toolname]` tools, which need no recorder at all
(`kind: "form"`; their schema is derived from the named inputs).

A call runs the recorded `execute` function in the page with the arguments
as its first parameter, awaits it, and returns the result JSON-serialised.
A form tool is filled from the arguments and submitted with
`requestSubmit()`. Results are capped at 16 KiB with `truncated: true`
beyond that. A tool that throws comes back as an `unsupported` error
carrying the page's message; a name that is not registered on the page
comes back as `stale_ref`.

The recorder is on by default and can be turned off with
`BrowserDriver(webmcp_shim=False)` or `A11Y_COMPUTER_USE_WEBMCP_SHIM=0`, in
which case only form tools are visible and script tools are reported as
`absent`.

## The webmcp tool and its tiers

The MCP tool `webmcp(app, action, name=None, arguments=None)` is registered
only when the driver has the feed, like `console` and `network`. On an OS
backend the Runtime answers `unsupported`.

- `action="list"` returns JSON `{api, tools: [{ref, name, description,
  inputSchema, kind, tier}]}` and makes those refs current. Read tier.
- `action="call"` runs one tool. `name` is a `w` ref or a tool name from
  the current listing; `arguments` is a JSON object (a JSON string is
  accepted too). Click tier by default.

A call is lifted to the full tier by `safety.webmcp_sensitive`, whose rule
is:

1. The input schema has a free-text string argument (a string property, or
   array item, or alternative, with no `enum`). The model composes that
   text, which is text entry, the same tier as `type`.
2. The tool name contains a payment or submission word, matched on the name
   split at underscores, dashes, dots, and camelCase boundaries: submit,
   send, post, publish, pay, payment, purchase, buy, checkout, order, book,
   reserve, transfer, donate, subscribe, signup, register, login, signin,
   message, email, reply, comment, upload, delete, remove.
3. The schema is missing. Unknown means the conservative side.

The listing shows each tool's tier so a planner knows before calling. A
name that matches the destructive keyword list used for buttons (delete,
trash, discard, and so on) also goes through the confirmation gate.

Every call runs through the usual gate: permission check, optional
confirmation, a recheck that the tab is still the bound one, execute,
audit. The audit row records the verb, the tab, and the tool name, and
always writes `[REDACTED]` for the arguments, the way clipboard writes are
redacted, because the arguments are free-form content handed to the page.

## Limits

- Tools a page registered before the recorder was installed into the
  current document are invisible until the page loads again. In practice
  the first browser snapshot installs the recorder before the agent
  navigates anywhere, so this bites only when the driver attaches to a tab
  that already registered tools; reload the page or navigate to it again.
- Only the top document is read. Tools registered inside cross-origin
  frames are not reachable from a main-frame evaluate and are skipped.
- The recorder makes `navigator.modelContext` exist in a browser that has
  no native API. A page that changes its behaviour on that check will
  behave as if an agent-aware browser were present.
- A native browser may enforce its own permission prompt or user-gesture
  rules for `execute`; the driver does not bypass them, and a rejected call
  surfaces as the page's error.
- The listing is not verified against the browser's own view. When Chrome
  ships an enumeration API the driver will merge it in (the listing script
  already calls `listTools()` when it exists), and `api: native` will mean
  the browser's list rather than the recorder's.
- The live test runs only when opted in with `A11Y_COMPUTER_USE_CDP_ENDPOINT`
  and a Chrome binary is found; it launches its own headless Chrome on a
  port in 9951..9999 against `tests/fixtures/webmcp_tools.html` and prints
  which registration path ran. On Chrome stable that path is `driver-shim`;
  the `native` path has not been exercised in this repository.

## WindTunnel as a future lane

WindTunnel (github.com/nekuda-ai/WindTunnel, Apache 2.0) is a 49-task
benchmark over 8 sites that publishes WebMCP tools, built to compare
agents that call those tools with agents that drive the DOM or pixels. Its
published numbers put a tool-calling agent at a fraction of the per-task
cost of a computer-use agent on the same tasks. A lane that runs the
a11y-computer-use agent loop against WindTunnel with `w` refs enabled, and
the same loop with the recorder off, would isolate what the tools add on
top of the accessibility tree. It is not built; the pieces it needs (the
listing, the call path, the tiers) are what this document describes.
