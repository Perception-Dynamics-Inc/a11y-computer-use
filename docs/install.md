# Install

> Moved here from the top-level README on 2026-10-17. The README is now a short landing page.

## Install

```bash
uvx a11y-computer-use doctor            # try it, no install
pip install a11y-computer-use           # extras: [browser] [windows] [linux] [ocr] [holo]
```

### Linux

An isolated venv cannot build the `[agent,linux]` extras. PyGObject is sdist-only and pycairo has no Linux wheel, so pip compiles both, and that compile needs `pkg-config` plus the cairo and girepository headers. Use the distro packages and a venv that can see them:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-atspi-2.0
python3 -m venv --system-site-packages .venv
.venv/bin/pip install 'a11y-computer-use[agent,linux]'
```

The other path is to install those build dependencies and then pip-install into an isolated venv. The package list is in [docs/linux-port.md](./linux-port.md).

Add it to Claude Code, Claude Desktop, Cursor, or any MCP host:

```bash
claude mcp add a11y-computer-use -- uvx a11y-computer-use mcp
```

```json
{ "mcpServers": { "a11y-computer-use": { "command": "uvx", "args": ["a11y-computer-use", "mcp"] } } }
```

For a browser tab instead of the desktop, start Chrome with `--remote-debugging-port=9222` and set `A11Y_COMPUTER_USE_DRIVER=browser`.
