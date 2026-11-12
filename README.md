# mcp_servers

A collection of small, self-hosted [MCP](https://modelcontextprotocol.io)
servers. Each one lives in its own directory with its own README, dependencies
and configuration, and is meant to be run locally by an MCP client rather than
deployed anywhere.

| Server | Purpose |
|---|---|
| [static-site-sync](static-site-sync/) | Mirrors a local directory to a remote web root over SSH |
| [influx-query](influx-query/) | Read-only Flux queries against InfluxDB 2.x, with CSV export |
| [ksuite-calendar](ksuite-calendar/) | Calendars and task lists over CalDAV; reads, creates, deletes only its own entries |

## Conventions

Each server directory contains:

- `server.py` — the server itself, no arguments, configuration read from `.env`
- `.env.example` — every setting with a comment, safe to commit
- `requirements.txt`
- `README.md`

Configuration lives in `.env` files, which are git-ignored. No host names,
paths or credentials belong in the code. Anything that would need to change
when the server is pointed at something else is a setting.

Tools take as few arguments as possible. Where a server acts on a fixed
resource, that resource comes from configuration rather than from a tool
argument, so the model driving the server cannot redirect it.

Each server has one venv, created inside its own directory:

```sh
cd <server>
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```
