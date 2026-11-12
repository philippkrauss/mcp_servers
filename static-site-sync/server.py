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
import shutil
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

        # An MCP client may launch this server with a different PATH than an
        # interactive shell. On Windows in particular, a Git installation puts
        # its own MSYS2 ssh.exe ahead of the OpenSSH one, and that build
        # resolves ~/.ssh elsewhere - which shows up as exit code 255 with no
        # error text. Pinning an absolute path here removes the ambiguity.
        self.ssh_command = values.get("SSH_COMMAND", "").strip() or "ssh"

        # "openssh" shells out to the ssh binary and inherits your existing
        # setup. "paramiko" speaks SSH from inside this process instead, which
        # is the way out when the host application launches this server in a
        # context where ssh.exe cannot start - a restriction that shows up as
        # exit code 255 with no output, even for "ssh -V".
        self.ssh_backend = (values.get("SSH_BACKEND", "").strip() or "openssh").lower()
        if self.ssh_backend not in {"openssh", "paramiko"}:
            raise ConfigError(
                f"SSH_BACKEND must be 'openssh' or 'paramiko', not {self.ssh_backend!r}."
            )

        # Only consulted by the paramiko backend, and only to override what it
        # would otherwise read from your ssh config for SSH_HOST.
        self.ssh_user = values.get("SSH_USER", "").strip()
        self.ssh_port = int(number("SSH_PORT", 0)) or None
        self.ssh_key_file = values.get("SSH_KEY_FILE", "").strip()
        self.ssh_config_file = values.get("SSH_CONFIG_FILE", "").strip()
        self.strict_host_keys = (
            values.get("SSH_STRICT_HOST_KEYS", "").strip().lower() not in {"0", "false", "no"}
        )

        # Extra arguments inserted before the destination, e.g. -F to point at a
        # config file explicitly. Useful when the launching process does not
        # share the environment that lets ssh find ~/.ssh by itself.
        raw_args = values.get("SSH_EXTRA_ARGS", "").strip()
        self.ssh_extra_args = [
            token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'"
            else token
            for token in shlex.split(raw_args, posix=False)
        ] if raw_args else []
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
    """Run a command on the remote host using the configured backend."""
    cfg = _config()
    if cfg.ssh_backend == "paramiko":
        return _ssh_paramiko(command, stdin, timeout or cfg.ssh_timeout)
    return _ssh_openssh(command, stdin, timeout)


def _ssh_openssh(command: str, stdin: bytes | None = None,
                 timeout: int | None = None) -> tuple[int, str, str]:
    """Run a command on the remote host by spawning the ssh binary.

    BatchMode makes ssh fail instead of prompting, so a key that needs a
    passphrase produces an error rather than a hang.
    """
    cfg = _config()
    argv = [
        cfg.ssh_command,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        *cfg.ssh_extra_args,
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
        raise SyncError(
            f"{cfg.ssh_command!r} was not found. Install an OpenSSH client, or set "
            "SSH_COMMAND in the .env file to its absolute path."
        ) from exc
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


def _ssh_target() -> dict[str, object]:
    """Resolve SSH_HOST through the ssh config, then apply explicit overrides.

    Keeps host aliases working under the paramiko backend, so the same entry
    in ~/.ssh/config drives both backends.
    """
    import paramiko

    cfg = _config()
    target: dict[str, object] = {
        "hostname": cfg.ssh_host,
        "port": 22,
        "username": None,
        "key_filenames": [],
    }

    config_path = Path(cfg.ssh_config_file) if cfg.ssh_config_file else (
        Path(os.path.expanduser("~")) / ".ssh" / "config"
    )
    if config_path.is_file():
        parsed = paramiko.SSHConfig()
        with config_path.open(encoding="utf-8", errors="replace") as handle:
            parsed.parse(handle)
        entry = parsed.lookup(cfg.ssh_host)
        target["hostname"] = entry.get("hostname", cfg.ssh_host)
        if entry.get("port"):
            target["port"] = int(entry["port"])
        if entry.get("user"):
            target["username"] = entry["user"]
        target["key_filenames"] = [
            os.path.expanduser(item) for item in entry.get("identityfile", []) or []
        ]

    if cfg.ssh_user:
        target["username"] = cfg.ssh_user
    if cfg.ssh_port:
        target["port"] = cfg.ssh_port
    if cfg.ssh_key_file:
        target["key_filenames"] = [os.path.expanduser(cfg.ssh_key_file)]
    if not target["username"]:
        target["username"] = os.environ.get("USERNAME") or os.environ.get("USER")

    return target


def _ssh_paramiko(command: str, stdin: bytes | None,
                  timeout: int) -> tuple[int, str, str]:
    """Run a remote command without spawning an ssh process."""
    cfg = _config()
    try:
        import paramiko
    except ModuleNotFoundError as exc:
        raise SyncError(
            "SSH_BACKEND is 'paramiko' but the paramiko package is not installed.\n"
            "    python -m pip install paramiko"
        ) from exc

    # paramiko logs through the logging module, which without configuration
    # reaches stderr and never stdout. That matters: this process speaks the
    # MCP protocol on stdout, and a stray byte there would corrupt it.
    target = _ssh_target()
    client = paramiko.SSHClient()

    known_hosts = Path(os.path.expanduser("~")) / ".ssh" / "known_hosts"
    if known_hosts.is_file():
        try:
            client.load_host_keys(str(known_hosts))
        except Exception:  # noqa: BLE001 - a malformed entry must not be fatal
            client.load_system_host_keys()
    client.set_missing_host_key_policy(
        paramiko.RejectPolicy() if cfg.strict_host_keys else paramiko.AutoAddPolicy()
    )

    try:
        client.connect(
            hostname=str(target["hostname"]),
            port=int(target["port"]),
            username=target["username"] or None,
            key_filename=list(target["key_filenames"]) or None,
            timeout=10,
            auth_timeout=15,
            banner_timeout=15,
            allow_agent=True,
            look_for_keys=True,
        )
    except paramiko.ssh_exception.SSHException as exc:
        hint = ""
        if "not found in known_hosts" in str(exc):
            hint = (
                "\nThe host key is not in ~/.ssh/known_hosts as read by this process. "
                "Connect once from a shell, or set SSH_STRICT_HOST_KEYS=false after "
                "verifying the fingerprint."
            )
        raise SyncError(f"paramiko could not connect: {exc}{hint}") from exc
    except Exception as exc:  # noqa: BLE001
        raise SyncError(
            f"paramiko could not connect to "
            f"{target['username']}@{target['hostname']}:{target['port']}: {exc}"
        ) from exc

    try:
        transport = client.get_transport()
        channel = transport.open_session(timeout=timeout)
        channel.settimeout(timeout)
        channel.exec_command(command)

        if stdin:
            channel.sendall(stdin)
        channel.shutdown_write()

        out = channel.makefile("rb").read()
        err = channel.makefile_stderr("rb").read()
        code = channel.recv_exit_status()
    except Exception as exc:  # noqa: BLE001
        raise SyncError(f"paramiko failed while running the remote command: {exc}") from exc
    finally:
        client.close()

    return code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _ssh_binary() -> str:
    """Absolute path of the ssh executable that will actually be used."""
    cfg = _config()
    resolved = shutil.which(cfg.ssh_command)
    return resolved or f"{cfg.ssh_command} (not found on PATH)"


def _ssh_checked(command: str, stdin: bytes | None = None,
                 timeout: int | None = None) -> str:
    cfg = _config()
    code, out, err = _ssh(command, stdin=stdin, timeout=timeout)
    if code != 0:
        detail = [f"Remote command failed with exit code {code}."]
        if cfg.ssh_backend == "openssh":
            detail.append(f"ssh binary: {_ssh_binary()}")
        detail += [
            f"ssh target: {cfg.ssh_host}",
            f"stderr: {err.strip() or '(empty)'}",
        ]
        if out.strip():
            detail.append(f"stdout: {out.strip()[:500]}")
        if code == 255 and cfg.ssh_backend == "openssh":
            detail.append(
                "\nExit code 255 comes from ssh itself, not from the remote command - "
                "the connection was never established. Common causes: the host alias "
                "cannot be resolved because this process does not see the same "
                "~/.ssh/config as your shell; the host key is not in known_hosts and "
                "BatchMode turns the prompt into an error; or a different ssh build is "
                "first on PATH (on Windows, Git ships its own). Compare the binary "
                "above with the one your shell uses, and set SSH_COMMAND in the .env "
                "file to an absolute path if they differ."
            )
        raise SyncError("\n".join(detail))
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

    if cfg.ssh_backend == "paramiko":
        try:
            target = _ssh_target()
            report.append(
                f"SSH backend: paramiko -> {target['username']}@{target['hostname']}"
                f":{target['port']}"
            )
        except Exception as exc:  # noqa: BLE001
            report.append(f"SSH backend: paramiko (target not resolvable: {exc})")
    else:
        report.append(f"SSH backend: openssh, binary {_ssh_binary()}")
    try:
        code, out, err = _ssh("echo reachable", timeout=30)
        if code == 0 and "reachable" in out:
            report.append(f"SSH:         connected to {cfg.ssh_host}")
        else:
            report.append(
                f"SSH:         FAILED (exit {code}) - {err.strip() or 'no error output'}"
            )
    except SyncError as exc:
        report.append(f"SSH:         FAILED - {exc}")

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


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
#
# Without arguments this file is an MCP server, which is how MCP clients start
# it. With a subcommand it runs the same tools for a human at a terminal, so a
# site can be published without an assistant in the loop. Both paths call the
# very same functions, so every safety check applies to both.


def _deploy_ok(result: str) -> bool:
    if result.startswith("Nothing to do"):
        return True
    return result.startswith("Sync complete.") and "Verification FAILED" not in result


def _confirm(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:  # no terminal attached - never assume yes
        print()
        return False
    return answer.strip().lower() in ("y", "yes", "j", "ja")


def cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="server.py",
        description="Run the static-site-sync tools from a terminal. "
                    "Without a subcommand, the MCP server starts instead.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="report the state of the site (read-only)")
    sub.add_parser("diff", help="show what a deploy would change (read-only)")
    deploy = sub.add_parser(
        "deploy",
        help="show the diff, ask, then mirror the local directory to the remote host",
    )
    deploy.add_argument(
        "--allow-delete", action="store_true",
        help="permit removing remote files that no longer exist locally",
    )
    deploy.add_argument(
        "--yes", action="store_true",
        help="skip the confirmation prompt",
    )
    args = parser.parse_args(argv)

    if args.command == "status":
        result = site_status()
        print(result)
        return 1 if "Configuration problem" in result else 0

    preview = site_diff()
    print(preview)
    if preview.startswith("Comparison not possible"):
        return 1
    if args.command == "diff" or "No differences" in preview:
        return 0

    print()
    if "Would be removed" in preview and not args.allow_delete:
        print("Aborted - nothing was changed. Files would be deleted from the "
              "remote host; run deploy again with --allow-delete if that is intended.")
        return 1
    if not args.yes and not _confirm("Deploy these changes to the live site?"):
        print("Cancelled - nothing was changed.")
        return 1

    result = site_deploy(allow_delete=args.allow_delete)
    print()
    print(result)
    return 0 if _deploy_ok(result) else 1


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        # File names may contain characters a Windows console code page
        # cannot encode; replace them rather than crash mid-report.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")
            except AttributeError:
                pass
        raise SystemExit(cli(sys.argv[1:]))
    mcp.run()
