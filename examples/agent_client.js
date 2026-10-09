#!/usr/bin/env node
// Small client for `a11y-agent serve`. Node's http module only; no npm packages.
// Not run by tests or CI.
//
//   node examples/agent_client.js --base http://127.0.0.1:8765 --goal "Save the note" \
//     --model scripted:turns.json --token "$TOKEN"

const http = require("node:http");
const https = require("node:https");

function args(argv) {
  const out = {
    base: "http://127.0.0.1:8765",
    goal: "Save the note",
    model: "scripted:turns.json",
    token: "",
    display: "",
  };
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (key === "--base") out.base = value;
    else if (key === "--goal") out.goal = value;
    else if (key === "--model") out.model = value;
    else if (key === "--token") out.token = value;
    else if (key === "--display") out.display = value;
    else continue;
    i += 1;
  }
  return out;
}

function request(base, method, path, token, body) {
  const url = new URL(path, base);
  const lib = url.protocol === "https:" ? https : http;
  const payload = body === undefined ? null : Buffer.from(JSON.stringify(body));
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (payload) {
    headers["Content-Type"] = "application/json";
    headers["Content-Length"] = String(payload.length);
  }
  return new Promise((resolve, reject) => {
    const req = lib.request(url, { method, headers }, (res) => {
      const chunks = [];
      res.on("data", (chunk) => chunks.push(chunk));
      res.on("end", () => {
        resolve({
          status: res.statusCode || 0,
          body: Buffer.concat(chunks).toString("utf8"),
        });
      });
    });
    req.on("error", reject);
    if (payload) req.write(payload);
    req.end();
  });
}

function parseSse(chunk, carry) {
  const text = carry + chunk;
  const blocks = text.split("\n\n");
  const rest = blocks.pop() || "";
  const events = [];
  for (const block of blocks) {
    if (!block.trim() || block.startsWith(":")) continue;
    let data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("data: ")) data += line.slice(6);
    }
    if (data) events.push(JSON.parse(data));
  }
  return { events, rest };
}

async function main() {
  const opts = args(process.argv);
  const goal = {
    goal: opts.goal,
    model: opts.model,
  };
  if (opts.display) goal.display = opts.display;
  const created = await request(opts.base, "POST", "/runs", opts.token, goal);
  if (created.status !== 202) {
    process.stderr.write(`POST /runs failed: ${created.status} ${created.body}\n`);
    process.exitCode = 1;
    return;
  }
  const { id } = JSON.parse(created.body);
  process.stdout.write(`run ${id}\n`);

  const url = new URL(`/runs/${id}/events`, opts.base);
  const lib = url.protocol === "https:" ? https : http;
  const headers = {};
  if (opts.token) headers.Authorization = `Bearer ${opts.token}`;
  await new Promise((resolve, reject) => {
    const req = lib.request(url, { method: "GET", headers }, (res) => {
      if ((res.statusCode || 0) !== 200) {
        reject(new Error(`events status ${res.statusCode}`));
        return;
      }
      let carry = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => {
        const parsed = parseSse(chunk, carry);
        carry = parsed.rest;
        for (const event of parsed.events) {
          process.stdout.write(`${event.seq} ${event.type}\n`);
        }
      });
      res.on("end", resolve);
    });
    req.on("error", reject);
    req.end();
  });

  const result = await request(opts.base, "GET", `/runs/${id}`, opts.token);
  process.stdout.write(result.body + "\n");
  if (result.status !== 200) process.exitCode = 1;
}

main().catch((err) => {
  process.stderr.write(`${err.message}\n`);
  process.exitCode = 1;
});
