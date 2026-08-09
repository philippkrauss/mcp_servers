#!/usr/bin/env python3
"""
static-site-sync - an MCP server that mirrors a local directory to a remote
web root over SSH.

The remote directory ends up matching the local one exactly: new files are
added, changed files are replaced, files that no longer exist locally are
removed, and permissions are normalised so the web server can read everything.

Design notes:

  * All configuration comes from a .env file next to this script. Nothing about
    a specific host or site is hard-coded, and the tools take no path arguments,
    so a model driving this server cannot point it somewhere else.
  * Only files with a whitelisted suffix are transferred. Dotfiles and dot
    directories are skipped.
  * The transfer is a tar archive streamed over ssh stdin. The archive is built
    entirely in memory first, so a local read error aborts before the remote
    host is touched at all.
  * The remote directory is modified in place and never replaced, which keeps
    bind mounts and inode-based references intact.
  * Nothing outside the configured remote directory is written. Server config,
    container definitions and the like remain manual work by design.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shlex
import socket
import ssl
import subprocess
import tarfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# The SDK renamed its high-level server class in 2.0: mcp.server.fastmcp.FastMCP
# became mcp.server.mcpserver.MCPServer. The decorator and run() signatures we
# rely on are the same in both, so support either rather than pinning a major.
try:
    from mcp.server.mcpserver import MCPServer as _Server  # mcp >= 2.0
except ModuleNotFoundError:
    try:
        from mcp.server.fastmcp import FastMCP as _Server  # mcp 1.2 .. 1.x
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Could not import the MCP server class from the 'mcp' package.\n"
            "Install it into this interpreter's environment:\n"
            "    python -m pip install -r requirements.txt"
        ) from exc

mcp = _Server("static-site-sync")

DEFAULT_SUFFIXES = (
    ".html,.htm,.css,.js,.mjs,.map,"
    ".svg,.png,.jpg,.jpeg,.gif,.webp,.avif,.ico,"
    ".txt,.xml,.json,.webmanifest,.ics,.pdf,"
    ".woff,.woff2,.ttf,.otf"
)

# chmod specs we are willing to interpolate into a remote shell command.
# Either symbolic (u=rwX,go=rX) or octal (755).
_CHMOD_RE = re.compile(
    r"^(?:[0-7]{3,4}|[ugoa]*[=+-][rwxXst]*(?:,[ugoa]*[=+-][rwxXst]*)*)$"
)


class ConfigError(RuntimeError):
    """The .env file is missing or unusable."""


class SyncError(RuntimeError):
    """Something went wrong; the message is meant for a human to read."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env reader.

    Deliberately does no escape processing: a Windows path such as
    C:\\srv\\site survives unquoted, which a general-purpose dotenv parser
    cannot promise. Supported syntax is KEY=VALUE, optional 'export ' prefix,
    '#' comments on their own line, and optional surrounding quotes.
    """
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


class Config:
    """Everything the server needs to know, read once at import time."""

    def __init__(self) -> None:
        env_path = Path(
            os.environ.get("STATIC_SITE_SYNC_ENV")
            or Path(__file__).resolve().parent / ".env"
        )
        if not env_path.is_file():
            raise ConfigError(
                f"No configuration file at {env_path}.\n"
                "Copy .env.example to .env and fill it in."
            )
        self.env_path = env_path
        values = _parse_env_file(env_path)

        def required(key: str) -> str:
            value = values.get(key, "").strip()
            if not value:
                raise ConfigError(f"{key} is missing or empty in {env_path}.")
            return value

        def number(key: str, default: float) -> float:
            raw = values.get(key, "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ConfigError(f"{key} in {env_path} is not a number: {raw!r}") from exc

        self.ssh_host = required("SSH_HOST")
        self.local_dir = Path(required("LOCAL_DIR"))
        self.remote_dir = required("REMOTE_DIR").rstrip("/")

        if not self.remote_dir.startswith("/"):
            raise ConfigError("REMOTE_DIR must be an absolute path.")
        if self.remote_dir.count("/") < 2:
            raise ConfigError(
                f"REMOTE_DIR {self.remote_dir!r} is too close to the filesystem root. "
                "This server deletes files that no longer exist locally, so it refuses "
                "to operate on a top-level directory."
            )

        self.staging_dir = (
            values.get("REMOTE_STAGING_DIR", "").strip().rstrip("/")
            or f"{self.remote_dir}.sync-staging"
        )
        if self.staging_dir == self.remote_dir:
            raise ConfigError("REMOTE_STAGING_DIR must differ from REMOTE_DIR.")

        self.site_url = values.get("SITE_URL", "").strip()
        self.container_name = values.get("CONTAINER_NAME", "").strip()

        self.sentinel = values.get("SENTINEL_FILE", "index.html").strip()
        self.sentinel_min_bytes = int(number("SENTINEL_MIN_BYTES", 500))

        suffixes = values.get("ALLOWED_SUFFIXES", "").strip() or DEFAULT_SUFFIXES
        self.allowed_suffixes = {
            ("." + item.strip().lstrip(".").lower())
            for item in suffixes.split(",")
            if item.strip()
        }

        self.max_file_bytes = int(number("MAX_FILE_MB", 5) * 1024 * 1024)
        self.max_total_bytes = int(number("MAX_TOTAL_MB", 25) * 1024 * 1024)
        self.ssh_timeout = int(number("SSH_TIMEOUT", 90))

        self.chmod_spec = values.get("CHMOD_SPEC", "u=rwX,go=rX").strip() or "u=rwX,go=rX"
        if not _CHMOD_RE.match(self.chmod_spec):
            raise ConfigError(
                f"CHMOD_SPEC {self.chmod_spec!r} is not a plain chmod expression. "
                "Use something like u=rwX,go=rX or 755."
            )


try:
    CFG: Config | None = Config()
    CFG_ERROR: str | None = None
except ConfigError as exc:  # keep the server startable so the error is visible
    CFG = None
    CFG_ERROR = str(exc)


def _config() -> Config:
    if CFG is None:
        raise SyncError(f"Configuration problem.\n\n{CFG_ERROR}")
    return CFG


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------


def _ssh(command: str, stdin: bytes | None = None,
         timeout: int | None = None) -> tuple[int, str, str]:
    """Run a command on the remote host.

    BatchMode makes ssh fail instead of prompting, so a key that needs a
    passphrase produces an error rather than a hang.
    """
    cfg = _config()
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        cfg.ssh_host,
        command,
    ]
    try:
        proc = subprocess.run(
            argv,
            input=stdin if stdin is not None else b"",
            capture_output=True,
            timeout=timeout or cfg.ssh_timeout,
        )
    except FileNotFoundError as exc:
        raise SyncError("ssh was not found on PATH. Is an OpenSSH client installed?") from exc
    except subprocess.TimeoutExpired as exc:
        raise SyncError(
            f"SSH call timed out after {timeout or cfg.ssh_timeout}s. "
            f"Check that 'ssh -o BatchMode=yes {cfg.ssh_host}' works from a shell - "
            "a key with a passphrase will fail here."
        ) from exc

    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def _ssh_checked(command: str, stdin: bytes | None = None,
                 timeout: int | None = None) -> str:
    code, out, err = _ssh(command, stdin=stdin, timeout=timeout)
    if code != 0:
        raise SyncError(
            f"Remote command failed with exit code {code}.\n"
            f"stderr: {err.strip() or '(empty)'}"
        )
    return out


# ---------------------------------------------------------------------------
# Local and remote inventories
# ---------------------------------------------------------------------------


def _local_files() -> dict[str, Path]:
    """Transferable files below LOCAL_DIR, keyed by their relative POSIX path."""
    cfg = _config()
    if not cfg.local_dir.is_dir():
        raise SyncError(
            f"LOCAL_DIR does not exist:\n  {cfg.local_dir}\n"
            "Check the path in the .env file, and that the directory is available "
            "(cloud-synced folders can be temporarily detached)."
        )

    found: dict[str, Path] = {}
    for path in sorted(cfg.local_dir.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(cfg.local_dir).parts
        if any(part.startswith(".") for part in parts):
            continue
        if path.suffix.lower() not in cfg.allowed_suffixes:
            continue
        found["/".join(parts)] = path
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remote_hashes() -> dict[str, str]:
    cfg = _config()
    quoted = shlex.quote(cfg.remote_dir)
    code, out, err = _ssh(
        f"cd {quoted} 2>/dev/null || exit 9; find . -type f -print0 | xargs -0 -r sha256sum"
    )
    if code == 9:
        return {}  # remote directory does not exist yet
    if code != 0:
        raise SyncError(f"Could not list the remote directory: {err.strip()}")

    hashes: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, name = line.partition("  ")
        hashes[name.removeprefix("./")] = digest
    return hashes


def _compare() -> tuple[dict[str, str], dict[str, str], list[str], list[str], list[str]]:
    """Returns (local hashes, remote hashes, added, changed, removed)."""
    local = {rel: _sha256(path) for rel, path in _local_files().items()}
    remote = _remote_hashes()
    added = sorted(set(local) - set(remote))
    changed = sorted(rel for rel in set(local) & set(remote) if local[rel] != remote[rel])
    removed = sorted(set(remote) - set(local))
    return local, remote, added, changed, removed


# ---------------------------------------------------------------------------
# Safety checks and transfer
# ---------------------------------------------------------------------------


def _preflight(files: dict[str, Path]) -> None:
    """Refuse obviously wrong input before anything leaves the machine."""
    cfg = _config()

    if not files:
        raise SyncError(
            f"No transferable file found in {cfg.local_dir}.\n"
            "Refusing to continue - this would wipe the remote directory."
        )

    if cfg.sentinel:
        if cfg.sentinel not in files:
            raise SyncError(
                f"The sentinel file {cfg.sentinel!r} is missing from the source "
                "directory. Refusing to continue."
            )
        size = files[cfg.sentinel].stat().st_size
        if size < cfg.sentinel_min_bytes:
            raise SyncError(
                f"{cfg.sentinel} is only {size} bytes "
                f"(minimum {cfg.sentinel_min_bytes}). That looks like a placeholder "
                "left behind by a cloud sync client, or a truncated file. "
                "Refusing to continue."
            )

    total = 0
    for rel, path in files.items():
        size = path.stat().st_size
        if size > cfg.max_file_bytes:
            raise SyncError(
                f"{rel} is {size / 1048576:.1f} MB, above the "
                f"{cfg.max_file_bytes / 1048576:.0f} MB per-file limit."
            )
        total += size
    if total > cfg.max_total_bytes:
        raise SyncError(
            f"The source directory totals {total / 1048576:.1f} MB, above the "
            f"{cfg.max_total_bytes / 1048576:.0f} MB limit."
        )


def _build_tar(files: dict[str, Path]) -> bytes:
    """Build the archive in memory so read errors abort before any remote change."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for rel, path in files.items():
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise SyncError(
                    f"Could not read {rel}: {exc}\n"
                    "If the file is a cloud placeholder, open the directory once so "
                    "it gets materialised."
                ) from exc
            info = tarfile.TarInfo(name=rel)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(path.stat().st_mtime)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _remote_script() -> str:
    """The shell script that performs the swap on the remote host.

    Uses only tar, find, cp and chmod - no rsync, which is not installed
    everywhere. The target directory is updated in place, never replaced.
    """
    cfg = _config()
    target = shlex.quote(cfg.remote_dir)
    staging = shlex.quote(cfg.staging_dir)
    return f"""
set -eu
TARGET={target}
STAGING={staging}
mkdir -p "$TARGET"
rm -rf "$STAGING"
mkdir -p "$STAGING"
tar -xf - -C "$STAGING"
cd "$TARGET"
find . -type f -print0 | while IFS= read -r -d '' f; do
    if [ ! -f "$STAGING/$f" ]; then rm -f "$f"; fi
done
find . -mindepth 1 -type d -empty -delete 2>/dev/null || true
cp -a "$STAGING/." "$TARGET/"
chmod -R {cfg.chmod_spec} "$TARGET"
rm -rf "$STAGING"
echo SYNC_OK
"""


def _listing(title: str, entries: list[str], marker: str) -> str:
    if not entries:
        return ""
    lines = [f"{title} ({len(entries)}):"]
    lines += [f"  {marker} {name}" for name in entries]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def site_status() -> str:
    """Report the state of the site. Read-only; changes nothing.

    Covers the HTTP response and TLS certificate (if SITE_URL is configured),
    the container (if CONTAINER_NAME is configured), the file listing on the
    remote host, and the state of the local source directory.
    """
    try:
        cfg = _config()
    except SyncError as exc:
        return str(exc)

    report: list[str] = ["static-site-sync - status\n"]

    if cfg.site_url:
        try:
            request = urllib.request.Request(
                cfg.site_url, headers={"User-Agent": "static-site-sync"}
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read(4096)
                report.append(f"HTTP:        {response.status} ({len(body)} bytes read)")
        except urllib.error.HTTPError as exc:
            report.append(f"HTTP:        {exc.code} {exc.reason}  <-- error")
        except Exception as exc:  # noqa: BLE001 - status must never hard-fail
            report.append(f"HTTP:        unreachable ({exc})")

        parsed = urlparse(cfg.site_url)
        host, scheme = parsed.hostname, parsed.scheme
        if scheme == "https" and host:
            try:
                context = ssl.create_default_context()
                with socket.create_connection((host, 443), timeout=10) as raw:
                    with context.wrap_socket(raw, server_hostname=host) as tls:
                        cert = tls.getpeercert()
                expires = datetime.strptime(
                    cert["notAfter"], "%b %d %H:%M:%S %Y %Z"
                ).replace(tzinfo=timezone.utc)
                days = (expires - datetime.now(timezone.utc)).days
                report.append(
                    f"Certificate: valid until {expires:%Y-%m-%d} ({days} days left)"
                )
            except Exception as exc:  # noqa: BLE001
                report.append(f"Certificate: could not be checked ({exc})")

    if cfg.container_name:
        try:
            code, out, _ = _ssh(
                "docker ps --filter name="
                f"{shlex.quote(cfg.container_name)} "
                "--format '{{.Names}}: {{.Status}}'",
                timeout=30,
            )
            line = out.strip()
            report.append(
                f"Container:   {line or 'no match'}" if code == 0
                else "Container:   could not be checked"
            )
        except SyncError as exc:
            report.append(f"Container:   could not be checked ({exc})")

    try:
        listing = _ssh_checked(
            f"cd {shlex.quote(cfg.remote_dir)} 2>/dev/null "
            "&& ls -lhA --time-style=long-iso | tail -n +2 "
            "|| echo '(directory does not exist)'",
            timeout=30,
        )
        report.append(f"\nRemote {cfg.remote_dir}:\n" + (listing.rstrip() or "(empty)"))
    except SyncError as exc:
        report.append(f"\nRemote listing unavailable: {exc}")

    try:
        files = _local_files()
        size = sum(path.stat().st_size for path in files.values())
        report.append(
            f"\nLocal source: {len(files)} files, {size / 1024:.0f} KB\n  {cfg.local_dir}"
        )
    except SyncError as exc:
        report.append(f"\nLocal source: {exc}")

    return "\n".join(report)


@mcp.tool()
def site_diff() -> str:
    """Show what a sync would change, without changing anything.

    Compares SHA-256 digests of the local files against those on the remote
    host and lists what would be added, replaced and removed.
    """
    try:
        cfg = _config()
        local, remote, added, changed, removed = _compare()
    except SyncError as exc:
        return f"Comparison not possible.\n\n{exc}"

    header = (
        f"Local {cfg.local_dir} ({len(local)} files)\n"
        f"vs. remote {cfg.remote_dir} ({len(remote)} files)\n"
    )
    if not (added or changed or removed):
        return header + "\nNo differences - the remote host is up to date."

    body = (
        _listing("Added", added, "+")
        + _listing("Changed", changed, "~")
        + _listing("Would be removed", removed, "-")
    )
    footer = f"\nUnchanged: {len(set(local) & set(remote)) - len(changed)}"
    if removed:
        footer += (
            "\n\nNote: files would be deleted. site_deploy only does that when "
            "called with allow_delete=True."
        )
    return header + "\n" + body + footer


@mcp.tool()
def site_deploy(allow_delete: bool = False) -> str:
    """Mirror the local directory to the remote host. Writes to the live site.

    Order of operations: run the safety checks, build the archive in memory,
    stream it over SSH into a staging directory, drop files that no longer
    exist locally, copy into place, normalise permissions, then verify the
    result by comparing digests again.

    Args:
        allow_delete: Required to be True if files would disappear from the
            remote host. Guards against an incomplete source directory
            emptying the live site.
    """
    try:
        cfg = _config()
        local, remote, added, changed, removed = _compare()
        files = _local_files()
        _preflight(files)
    except SyncError as exc:
        return f"Aborted - nothing was changed.\n\n{exc}"

    if not (added or changed or removed):
        return "Nothing to do - the remote host already matches the local directory."

    if removed and not allow_delete:
        return (
            "Aborted - nothing was changed.\n\n"
            + _listing("These files would be deleted", removed, "-")
            + "\nIf that is intended, call site_deploy again with allow_delete=True."
        )

    try:
        archive = _build_tar(files)
    except SyncError as exc:
        return f"Aborted while reading local files - the remote host is untouched.\n\n{exc}"

    try:
        output = _ssh_checked(_remote_script(), stdin=archive)
    except SyncError as exc:
        return (
            f"The remote sync failed.\n\n{exc}\n\n"
            "The state of the site is unclear - call site_status."
        )

    if "SYNC_OK" not in output:
        return (
            "The remote script did not report success. Output:\n"
            f"{output.strip() or '(empty)'}\n\nCall site_status."
        )

    try:
        after = _remote_hashes()
    except SyncError as exc:
        return f"Sync ran, but the verification step failed: {exc}"

    mismatched = sorted(
        rel for rel in set(local) | set(after) if local.get(rel) != after.get(rel)
    )

    result = [
        "Sync complete.",
        f"  {len(added)} added, {len(changed)} changed, {len(removed)} removed",
        f"  {len(after)} files now in {cfg.remote_dir}",
        f"  permissions normalised with chmod -R {cfg.chmod_spec}",
    ]
    if mismatched:
        result.append(
            "\nVerification FAILED - these files still differ:\n  "
            + "\n  ".join(mismatched)
        )
    else:
        result.append("\nVerified: remote and local directories are identical.")

    detail = (
        _listing("Added", added, "+")
        + _listing("Changed", changed, "~")
        + _listing("Removed", removed, "-")
    )
    return "\n".join(result) + ("\n\n" + detail if detail else "")


if __name__ == "__main__":
    mcp.run()
