# Embedding computerUse

computerUse is a **library you embed in your AI platform**, not an end-user app. There are two integration shapes; both run under **your** app's identity, so your Developer ID / Authenticode signature and OS permission grants apply — computerUse ships no certificate of its own.

| | [`inprocess_python.py`](./inprocess_python.py) | [`mcp_subprocess.mjs`](./mcp_subprocess.mjs) |
|---|---|---|
| **Shape** | Import the library, in-process | Spawn `computeruse mcp` as a child, speak MCP over stdio |
| **Host language** | Python | **Any** (Node shown; Go/Rust/Swift/… identical) |
| **Identity** | runs *as* your process | child of your process; you own responsible-process attribution |
| **Best for** | Python agents | non-Python platforms, or process isolation |

## Run them

```bash
# In-process (Python). Reads Finder's a11y tree (read-only, safe).
python examples/inprocess_python.py

# MCP subprocess (Node). Any MCP-speaking language does the same.
npm i @modelcontextprotocol/sdk
node examples/mcp_subprocess.mjs
```

Both need Accessibility granted to **whatever runs them** (your app, or your terminal while developing) — that's the point: the grant attaches to the host, and computerUse inherits it.

## Your integration checklist (macOS)

1. **Sign your app** with your own Developer ID (Authenticode on Windows). We ship no cert and need none.
2. **Request the OS permissions** your app uses — Accessibility always; Screen Recording only if you use the `screenshot`/`zoom` vision fallback. `computeruse doctor` verifies them from inside your process.
3. **Hardened runtime** (needed for notarization): embed in-process, sign the bundled components with your Team ID, or set `com.apple.security.cs.disable-library-validation` — standard for any app embedding Python.
4. **Subprocess model only:** make sure TCC's "responsible process" resolves to your signed app (embed in-process, or ship the helper signed with your Team ID at a stable path). `doctor` prints the responsible app so you can check.

The payoff: your users grant permissions to *your* trusted app once, and they survive your updates — because the identity is stable and yours.
