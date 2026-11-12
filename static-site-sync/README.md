# static-site-sync

An MCP server that mirrors a local directory to a remote web root over SSH.

After a sync the remote directory matches the local one exactly: new files are
added, changed files replaced, files that no longer exist locally removed, and
permissions normalised so the web server can read everything.

It exists so that an assistant can publish a static site without a human doing
the file transfer by hand, while the blast radius stays small enough to reason
about.

## Tools

| Tool | Effect |
|---|---|
| `site_status()` | read-only: HTTP response, TLS expiry, container state, remote file listing, local source state |
| `site_diff()` | read-only: dry run by SHA-256 comparison — what would be added, changed, removed |
| `site_deploy(allow_delete=False)` | writes: mirrors the local directory to the remote host |

## Command line

The same tools run from a terminal, for publishing without an assistant:

```sh
python server.py status                  # read-only
python server.py diff                    # read-only dry run
python server.py deploy                  # shows the diff, asks, then deploys
python server.py deploy --allow-delete   # also removes orphaned remote files
python server.py deploy --yes            # no prompt, for scripts
```

Without a subcommand the file starts as an MCP server, which is how MCP
clients launch it, so registering it stays unchanged. Both paths call the same
functions and share every safety check. `deploy` refuses before prompting when
files would be deleted and `--allow-delete` is missing. The exit code is `0`
on success or when there is nothing to do, and `1` when something was refused,
cancelled or failed.

## Scope

The server touches exactly one remote directory. It does not deploy server
configuration, container definitions or anything outside that directory —
infrastructure stays manual on purpose, because that is where the mistakes are
expensive and rare enough not to be worth automating.

Source and target come from a `.env` file. **The tools take no path arguments**,
so a model driving this server cannot redirect it. Only files with a
whitelisted suffix are transferred; dotfiles, dot directories and everything
else stay behind, which keeps notes, secrets and build config out of the web
root even when they sit in the same folder.

## Safety

`site_deploy` deletes remote files that no longer exist locally. That is the
point of a mirror, and it is also the dangerous part, so it refuses to run when:

- the source directory is missing or contains no transferable file,
- the sentinel file (`index.html` by default) is absent, or smaller than
  `SENTINEL_MIN_BYTES` — this is what catches a detached network drive or a
  cloud-sync placeholder before it empties the live site,
- a file exceeds `MAX_FILE_MB`, or the directory exceeds `MAX_TOTAL_MB`,
- files would be deleted and `allow_delete` was not set,
- `REMOTE_DIR` is not absolute, or is a top-level directory.

After a successful run the digests are compared again and any remaining
mismatch is reported.

## How the transfer works

1. Build the file list and filter it by suffix.
2. Build a tar archive **entirely in memory**. A read error aborts here, before
   a connection is opened, so a partial transfer cannot happen.
3. Stream the archive over `ssh` stdin. On the remote host: extract to a staging
   directory, delete orphans in the target, `cp -a` into place, `chmod -R`.
4. Compare digests again to verify.

No `scp` and no `rsync` — `tar`, `find`, `cp` and `chmod` are enough and are
present on any POSIX host. The target directory is modified in place and never
replaced, so bind mounts into a container stay intact.

The `chmod` step is not cosmetic. Files arriving with a restrictive umask are a
common cause of `403 Forbidden` from a web server that runs as its own user;
normalising after every sync removes that failure mode permanently.

## Requirements

- Python 3.10 or newer
- The `mcp` SDK, either 1.x or 2.x — the high-level server class moved from
  `mcp.server.fastmcp.FastMCP` to `mcp.server.mcpserver.MCPServer` in 2.0, and
  the import here handles both
- Key-based SSH access that works non-interactively
- Either an OpenSSH client on `PATH`, or `paramiko` (see below)

## Two SSH backends

`SSH_BACKEND=openssh` (the default) runs the `ssh` binary and inherits whatever
already works in your shell — config, agent, known_hosts.

`SSH_BACKEND=paramiko` speaks SSH from inside the Python process and needs no
`ssh` binary. Use it when the application launching this server does so in a
context where `ssh` cannot start: the symptom is exit code 255 with no output
at all, even from `ssh -V`, while an identical command works in a terminal. The
paramiko backend still reads `~/.ssh/config`, so a host alias in `SSH_HOST`
keeps working, and it verifies host keys against `~/.ssh/known_hosts`.

When SSH fails, `site_status` reports the exit code and whatever the backend
said. If that is exit code 255 with no output at all, the `ssh` binary never
started — switch to `SSH_BACKEND=paramiko` rather than looking for a fault in
your SSH configuration.

The server calls `ssh` with `BatchMode=yes`, so a key that needs a passphrase
produces an error instead of hanging. Use an agent, or a key without one.

## Setup

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # Windows: .venv\Scripts\python.exe
cp .env.example .env                                  # then edit it
```

Verify SSH works the way the server will use it:

```sh
ssh -o BatchMode=yes <SSH_HOST> "ls -ld <REMOTE_DIR>"
```

Register the server with your MCP client, for example:

```json
{
  "mcpServers": {
    "static-site-sync": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"]
    }
  }
}
```

The `.env` file is read from the directory containing `server.py`. Set
`STATIC_SITE_SYNC_ENV` to an absolute path to override that — useful for
running several instances against different sites from one checkout.

## Configuration

See [.env.example](.env.example). `SSH_HOST`, `LOCAL_DIR` and `REMOTE_DIR` are
required; everything else has a default.

A missing or invalid `.env` does not stop the server from starting. The tools
report the problem instead, which is easier to debug than a client that shows
no tools at all.

## Licence

None yet — all rights reserved by default. Open an issue if you want to use it.
