// MCP-subprocess embed (non-Python host): spawn `computeruse mcp` and drive it
// over stdio with the Model Context Protocol. This is the integration shape for
// ANY language. Node is shown here; any language with an MCP stdio client
// follows the same shape, though only the Python client is exercised in this
// repo (tests/e2e/test_mcp_stdio.py).
//
// The child runs under YOUR app: TCC/Gatekeeper attribute to the responsible
// process (your signed app), so computerUse inherits your grants. You ship no
// computerUse certificate.
//
// Setup:
//   npm i @modelcontextprotocol/sdk
//   node examples/mcp_subprocess.mjs
//
// Needs Accessibility granted to whatever launches this (your app / terminal).

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const transport = new StdioClientTransport({
  command: "computeruse", // your app spawns this child; sign+entitle the parent
  args: ["mcp"],
});

const client = new Client({ name: "my-ai-platform", version: "0.1.0" });
await client.connect(transport);

// 1) discover the tool surface
const { tools } = await client.listTools();
console.log("tools:", tools.map((t) => t.name).join(", "));

// 2) observe: a pruned accessibility tree with element refs, no pixels
const snap = await client.callTool({
  name: "desktop_snapshot",
  arguments: { app: "com.apple.finder", scope: "window" },
});
console.log(snap.content[0].text);

// 3) act: target a ref from the tree (requires a CLICK/FULL grant for the app)
// await client.callTool({ name: "click", arguments: { ref: "e14" } });
// await client.callTool({ name: "type",  arguments: { text: "hello world" } });
//
// If desktop_snapshot returns "no interactive elements", the app is custom-drawn
// (e.g. Telegram), fall back to the `screenshot` tool + coordinate clicks.

await client.close();
